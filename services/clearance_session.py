"""One bounded Cloudflare recovery on the same account and egress."""
from __future__ import annotations

import time
from http.cookiejar import CookieJar
from typing import Callable
from urllib.parse import urlparse

from utils.network_diagnostics import response_diagnostics


class ClearanceSession:
    def __init__(self, session, store, *, proxy_url: str, account, resource: bool,
                 on_user_agent: Callable[[str], None] | None = None):
        self._session = session
        self._store = store
        self._proxy_url = proxy_url
        self._account = account
        self._resource = resource
        self._on_user_agent = on_user_agent

    def __getattr__(self, name):
        return getattr(self._session, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._session.close()

    def _call(self, name: str, *args, **kwargs):
        url = str((args[1] if len(args) > 1 else kwargs.get("url")) if name == "request"
                  else (args[0] if args else kwargs.get("url")) or "")
        account = self._account() if callable(self._account) else self._account
        profile = self._store.get_profile(account=account, resource=self._resource,
                                          upstream=True, force_proxy=self._proxy_url)
        if not profile.clearance_enabled or urlparse(url).scheme not in {"http", "https"}:
            return getattr(self._session, name)(*args, **kwargs)
        timeout = kwargs.get("timeout", 60)
        budget = float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else 60.0
        deadline = time.monotonic() + budget
        call_kwargs = dict(kwargs)
        bundle = self._store.clearance_bundle(url, account=account, resource=self._resource,
                                               force_proxy=self._proxy_url)
        call_kwargs["headers"] = self._headers(url, account, kwargs.get("headers"), bundle)
        response = getattr(self._session, name)(*args, **call_kwargs)
        status = getattr(response, "status_code", 0)
        if status not in profile.reset_session_status_codes or not _is_challenge(response, bool(kwargs.get("stream"))):
            return response
        # Never replay files or a consumed iterator; JSON and byte uploads are replayable.
        data = kwargs.get("data")
        if kwargs.get("files") or kwargs.get("multipart") or (
            data is not None and not isinstance(data, (bytes, str, dict, list, tuple))
        ):
            return response
        remaining = deadline - time.monotonic()
        if profile.clearance_mode == "flaresolverr":
            if remaining < 1:
                return response
            fresh = self._store.refresh_clearance(
                target_url=url, account=account, resource=self._resource,
                force_proxy=self._proxy_url, force=True, timeout=remaining,
                on_challenge=True, rejected_bundle=bundle,
            )
            if fresh is None or fresh is bundle:
                return response
        else:
            # Manual mode cannot obtain a new cookie; reconnect once with its saved bundle.
            fresh = bundle
            if fresh is None:
                return response
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return response
        response.close()
        # The TLS wrapper preserves authorization/device cookies across pool resets.
        self._session.reset()
        call_kwargs["headers"] = self._headers(url, account, kwargs.get("headers"), fresh)
        call_kwargs["timeout"] = remaining
        return getattr(self._session, name)(*args, **call_kwargs)

    def _headers(self, url, account, headers, bundle):
        merged = dict(getattr(self._session, "headers", {}) or {})
        merged.update(dict(headers or {}))
        if bundle is not None:
            jar = getattr(getattr(self._session, "cookies", None), "jar", None)
            host = urlparse(url).hostname or ""
            if isinstance(jar, CookieJar):
                for cookie in list(jar):
                    domain = cookie.domain.lstrip(".")
                    if cookie.name in bundle.cookies and (host == domain or host.endswith("." + domain)):
                        jar.clear(cookie.domain, cookie.path, cookie.name)
            # Replace stale clearance cookies, retaining unrelated account cookies.
            key = next((key for key in merged if key.lower() == "cookie"), "Cookie")
            cookies = dict(part.strip().split("=", 1) for part in str(merged.get(key) or "").split(";") if "=" in part)
            cookies.update(bundle.cookies)
            if cookies:
                merged[key] = "; ".join(f"{name}={value}" for name, value in cookies.items())
            if bundle.user_agent:
                ua_key = next((key for key in merged if key.lower() == "user-agent"), "User-Agent")
                merged[ua_key] = bundle.user_agent
                # Old client hints must not contradict the solver's browser UA.
                for key in list(merged):
                    if key.lower().startswith("sec-ch-ua"):
                        merged[key] = None
                if self._on_user_agent is not None:
                    self._on_user_agent(bundle.user_agent)
        return merged

    def get(self, *args, **kwargs):
        return self._call("get", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._call("post", *args, **kwargs)

    def put(self, *args, **kwargs):
        return self._call("put", *args, **kwargs)

    def patch(self, *args, **kwargs):
        return self._call("patch", *args, **kwargs)

    def delete(self, *args, **kwargs):
        return self._call("delete", *args, **kwargs)

    def head(self, *args, **kwargs):
        return self._call("head", *args, **kwargs)

    def request(self, *args, **kwargs):
        return self._call("request", *args, **kwargs)


def _is_challenge(response, stream: bool) -> bool:
    if response.status_code in {401, 407, 429}:
        return False
    headers = {str(key).lower(): str(value) for key, value in (getattr(response, "headers", {}) or {}).items()}
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if stream and "text/html" in headers.get("content-type", "").lower():
        # A failed HTML stream is not an image/SSE result. Buffer only an error excerpt.
        chunks = []
        size = 0
        try:
            for chunk in response.iter_content(chunk_size=4096):
                chunks.append(chunk[:16384 - size])
                size += len(chunks[-1])
                if size >= 16384:
                    break
        finally:
            response.close()
        response.content = b"".join(chunks)
    body = getattr(response, "content", b"")
    text = body[:16384].decode("utf-8", errors="replace") if isinstance(body, bytes) else ""
    if "text/html" not in headers.get("content-type", "").lower() and not text.lstrip().lower().startswith(("<html", "<!doctype html")):
        return False
    return response_diagnostics(response.status_code, text, headers)["failure_kind"] == "challenge"
