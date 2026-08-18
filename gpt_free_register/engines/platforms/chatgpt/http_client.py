"""OpenAI 专用 HTTP 客户端"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

from curl_cffi import requests as cffi_requests
from curl_cffi.requests import Session

from core.http_client import HTTPClient, HTTPClientError, RequestConfig
from .browser_profile import (
    apply_profile_to_session,
    browser_profile,
    default_request_headers,
    random_browser_profile,
    resolve_impersonate,
)
from .constants import ERROR_MESSAGES

logger = logging.getLogger(__name__)


class OpenAIHTTPClient(HTTPClient):
    """OpenAI 专用 HTTP 客户端：TLS impersonate + headers + Sentinel 共用同一画像。"""

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        config: Optional[RequestConfig] = None,
        *,
        platform: Optional[str] = None,
        profile: Optional[Dict[str, Any]] = None,
        randomize: bool = False,
        profile_seed: Optional[str] = None,
    ):
        # One account => one coherent profile. randomize=True for independent accounts.
        if profile is not None:
            browser = dict(profile)
        elif randomize:
            browser = random_browser_profile(
                seed=profile_seed,
                platform=platform or os.environ.get("OPENAI_BROWSER_PLATFORM"),
            )
        else:
            browser = browser_profile(
                platform=platform or os.environ.get("OPENAI_BROWSER_PLATFORM"),
            )

        cfg = config or RequestConfig()
        if config is None:
            cfg.timeout = 30
            cfg.max_retries = 3
        # TLS impersonate must match profile chrome family.
        cfg.impersonate = resolve_impersonate(
            browser.get("impersonate") or getattr(cfg, "impersonate", None)
        )
        browser["impersonate"] = cfg.impersonate
        browser["chrome_major"] = browser.get("chrome_major") or str(
            "".join(ch for ch in cfg.impersonate if ch.isdigit()) or "142"
        )
        super().__init__(proxy_url, cfg)

        self.browser = browser
        self.default_headers = default_request_headers(profile=self.browser, for_api=True)

    def _apply_default_session_headers(self, session: Session) -> None:
        apply_profile_to_session(session, self.browser)
        # Keep Accept loose on session; per-request code often sets application/json.
        session.headers.setdefault("Accept-Language", self.browser["accept_language"])

    @property
    def user_agent(self) -> str:
        return str(self.browser.get("user_agent") or self.default_headers.get("User-Agent") or "")

    def check_ip_location(self) -> Tuple[bool, Optional[str]]:
        """Return (ok, location).

        ok=False only when the egress IP is in a blocked region. If the trace
        probe itself fails (proxy hiccup / network jitter), we return ok=True
        with location=None so a transient cloudflare.com timeout does not abort
        the whole registration — the real OpenAI requests use the same proxy
        and will surface their own errors if it is truly down.
        """
        blocked = {
            x.strip().upper()
            for x in str(os.environ.get("OPENAI_BLOCK_REGIONS", "CN") or "CN").split(",")
            if x.strip()
        }
        last_err = ""
        for attempt in range(1, 4):
            try:
                response = self.get("https://cloudflare.com/cdn-cgi/trace", timeout=10)
                trace_text = response.text or ""
                loc_match = re.search(r"loc=([A-Z]+)", trace_text)
                loc = loc_match.group(1) if loc_match else None
                if loc and loc in blocked:
                    return False, loc
                return True, loc
            except Exception as e:
                last_err = str(e)
                if attempt < 3:
                    import time as _time

                    _time.sleep(0.8 * attempt)
        logger.warning(f"检查 IP 地理位置失败（放行继续）: {last_err}")
        return True, None

    def send_openai_request(
        self,
        endpoint: str,
        method: str = "POST",
        data: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        request_headers = self.default_headers.copy()
        if headers:
            request_headers.update(headers)
        if json_data is not None and "Content-Type" not in request_headers:
            request_headers["Content-Type"] = "application/json"
        elif data is not None and "Content-Type" not in request_headers:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            response = self.request(
                method,
                endpoint,
                data=data,
                json=json_data,
                headers=request_headers,
                **kwargs,
            )
            response.raise_for_status()
            try:
                return response.json()
            except json.JSONDecodeError:
                return {"raw_response": response.text}
        except cffi_requests.RequestsError as e:
            raise HTTPClientError(f"OpenAI 请求失败: {endpoint} - {e}")

    def check_sentinel(self, did: str, proxies: Optional[Dict] = None) -> Optional[str]:
        from .constants import OPENAI_API_ENDPOINTS, SENTINEL_FRAME_URL

        try:
            sen_req_body = f'{{"p":"","id":"{did}","flow":"authorize_continue"}}'
            response = self.post(
                OPENAI_API_ENDPOINTS["sentinel"],
                headers={
                    "origin": "https://sentinel.openai.com",
                    "referer": SENTINEL_FRAME_URL,
                    "content-type": "text/plain;charset=UTF-8",
                },
                data=sen_req_body,
            )
            if response.status_code == 200:
                return response.json().get("token")
            logger.warning(f"Sentinel 检查失败: {response.status_code}")
            return None
        except Exception as e:
            logger.error(f"Sentinel 检查异常: {e}")
            return None


def create_http_client(
    proxy_url: Optional[str] = None,
    config: Optional[RequestConfig] = None,
) -> HTTPClient:
    return HTTPClient(proxy_url, config)


def create_openai_client(
    proxy_url: Optional[str] = None,
    config: Optional[RequestConfig] = None,
) -> OpenAIHTTPClient:
    return OpenAIHTTPClient(proxy_url, config)
