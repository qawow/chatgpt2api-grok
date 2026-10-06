"""Transport diagnostics only; HTTP reachability is not account usability."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

_HTTP_STATUS = re.compile(
    r"(?:^|[^a-z0-9])(?:http(?:/\d(?:\.\d)?)?[\s_:=]*(?:error[\s_:=]*)?|"
    r"status(?:_code)?[\"']?[\s_:=]+)([1-5]\d{2})(?!\d)", re.I,
)
_CURL_CODE = re.compile(r"\bcurl\s*:\s*\((\d+)\)", re.I)
_CHALLENGE_MARKERS = ("cf-chl-", "cf_chl_", "/cdn-cgi/challenge-platform/", "just a moment", "cf-mitigated")


def proxy_identity(proxy: object) -> dict[str, str]:
    """Identify a gateway AND its credentials without exposing either credential.

    Resin session names live in userinfo. The complete username is opaque here:
    splitting on a dot would corrupt ordinary dotted usernames and nested labels.
    A credential change must also invalidate a previous connectivity observation.
    """
    text = str(proxy or "").strip()
    if not text:
        return {"proxy_id": "direct", "gateway": "", "scheme": "direct"}
    try:
        parsed = urlsplit(text if "://" in text else "http://" + text)
        scheme = parsed.scheme.lower()
        if scheme in {"socks", "socks5"}:
            scheme = "socks5h"
        if scheme not in {"http", "https", "socks5h"} or not parsed.hostname:
            raise ValueError("invalid proxy URL")
        if any(char.isspace() for char in text) or parsed.port == 0:
            raise ValueError("invalid proxy URL")
        port = parsed.port or {"http": 80, "https": 443, "socks5h": 1080}[scheme]
        host = parsed.hostname.lower()
        gateway = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        material = json.dumps([
            scheme, host, port, unquote(parsed.username or ""),
            unquote(parsed.password or ""), parsed.path, parsed.query,
        ], ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError):
        scheme, gateway, material = "invalid", "", text
    return {
        "proxy_id": "proxy:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24],
        "gateway": gateway,
        "scheme": scheme,
    }


def response_diagnostics(status: int, body: str = "", headers: Mapping[str, Any] | None = None) -> dict[str, Any]:
    text = str(body or "")[:65536].lower()
    header_map = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    html = "<html" in text or "<!doctype html" in text or "text/html" in header_map.get("content-type", "").lower()
    challenge = header_map.get("cf-mitigated", "").lower() == "challenge" or any(
        marker in text for marker in _CHALLENGE_MARKERS
    )
    if status == 407:
        kind = "proxy_auth"
    elif challenge:
        kind = "challenge"
    elif status == 429:
        kind = "rate_limit"
    elif status == 401:
        kind = "http_auth"
    elif status == 403:
        kind = "http_forbidden"
    elif status >= 500:
        kind = "upstream"
    elif 300 <= status < 400:
        kind = "redirect"
    elif 200 <= status < 300:
        kind = "unexpected_html" if html else "none"
    else:
        kind = "http_error" if status else "unknown"
    result: dict[str, Any] = {
        "ok": kind == "none", "reachable": 100 <= status <= 599,
        "http_status": status, "failure_kind": kind,
    }
    retry = header_map.get("retry-after", "").strip()
    if retry.isascii() and retry.isdecimal() and len(retry) <= 9:
        result["retry_after_seconds"] = min(int(retry), 604800)
    return result


def error_details(error: object) -> dict[str, Any]:
    """Extract bounded, non-secret labels; never infer revocation from a 403."""
    text = str(error or "").lower()
    status_match, curl_match = _HTTP_STATUS.search(text), _CURL_CODE.search(text)
    status = int(status_match.group(1)) if status_match else 0
    curl_code = int(curl_match.group(1)) if curl_match else 0
    if status == 407 or any(s in text for s in ("user was rejected", "proxy authentication", "socks5 authentication")):
        kind = "proxy_auth"
    elif any(s in text for s in _CHALLENGE_MARKERS):
        kind = "challenge"
    elif status == 429:
        kind = "rate_limit"
    elif status == 403:
        kind = "http_forbidden"
    elif status >= 500:
        kind = "upstream"
    elif curl_code == 28 or "timed out" in text or "timeout" in text:
        kind = "timeout"
    elif curl_code in {5, 6} or "could not resolve" in text or "name or service not known" in text:
        kind = "dns"
    elif curl_code == 97 or "socks5 connection" in text or "socks handshake" in text:
        kind = "proxy_connect"
    elif curl_code in {35, 60} or any(s in text for s in ("openssl_internal", "invalid library", "tls", "sslerror", "certificate verify failed")):
        kind = "tls"
    elif curl_code in {7, 52, 55, 56} or any(s in text for s in ("connection", "proxyerror", "proxy error", "failed to connect")):
        kind = "transport"
    elif "token_revoked" in text or "invalidated oauth" in text or "app_session_terminated" in text:
        kind = "token_revoked"
    elif status == 401 or "token invalidated" in text:
        kind = "http_auth"
    else:
        kind = "unknown"
    result: dict[str, Any] = {"failure_kind": kind}
    if status:
        result["http_status"] = status
    if curl_code:
        result["curl_code"] = curl_code
    return result
