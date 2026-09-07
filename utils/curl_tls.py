"""Isolate curl_cffi (BoringSSL) from Debian OpenSSL on Linux/WSL2/Docker.

``OPENSSL_internal:invalid library`` is a local library-init failure, not a
remote handshake problem. It shows up as ``curl: (35)`` and the image path
wraps that as ``upstream image connection failed``.
"""
from __future__ import annotations

import os
from typing import Any

DEFAULT_IMPERSONATE = "chrome142"

_LEGACY_IMPERSONATES = {
    "chrome",
    "chrome99",
    "chrome100",
    "chrome101",
    "chrome104",
    "chrome107",
    "chrome110",
    "chrome116",
    "chrome119",
}


def is_openssl_invalid_library(message: str) -> bool:
    text = str(message or "").lower()
    return "openssl_internal" in text and "invalid library" in text


def sanitize_curl_ssl_env() -> list[str]:
    """Drop system OpenSSL config that BoringSSL cannot load.

    Returns the environment keys that were removed.
    """
    removed: list[str] = []
    conf = os.environ.get("OPENSSL_CONF")
    if conf is None:
        return removed
    path = str(conf).strip()
    if (
        not path
        or path.endswith("openssl.cnf")
        or path.startswith("/etc/")
        or path.startswith("/usr/lib/ssl")
        or "/ssl/" in path.replace("\\", "/")
    ):
        os.environ.pop("OPENSSL_CONF", None)
        removed.append("OPENSSL_CONF")
    return removed


def resolve_session_impersonate(value: str | None) -> str:
    raw = str(value or "").strip() or DEFAULT_IMPERSONATE
    if raw.lower() in _LEGACY_IMPERSONATES:
        return DEFAULT_IMPERSONATE
    return raw


def impersonate_fallback_chain(value: str | None) -> list[str]:
    start = resolve_session_impersonate(value)
    # Chrome impersonates share one BoringSSL handle. chrome142 and chrome146
    # both fail OPENSSL_internal on the same SOCKS, so the next step is a
    # session with no fingerprint.
    return [start, ""]


def create_cffi_session(**session_kwargs: Any):
    """curl_cffi Session that retries OPENSSL_internal:invalid library.

    Recreates the handle without impersonate after sanitizing OPENSSL_CONF.
    Cookies/headers from a failed TLS connect are not useful.
    """
    from curl_cffi.requests import Session

    sanitize_curl_ssl_env()
    kwargs = dict(session_kwargs)
    chain = impersonate_fallback_chain(kwargs.get("impersonate"))
    if chain[0]:
        kwargs["impersonate"] = chain[0]
    else:
        kwargs.pop("impersonate", None)
    return _TlsLibraryFallbackSession(Session, chain, kwargs)


class _TlsLibraryFallbackSession:
    def __init__(self, factory, chain: list[str], session_kwargs: dict[str, Any]) -> None:
        self._factory = factory
        self._chain = list(chain)
        self._kwargs = dict(session_kwargs)
        self._idx = 0
        self._inner = self._factory(**self._kwargs)

    def _rebind(self) -> None:
        headers = {}
        cookies = None
        try:
            headers = dict(self._inner.headers)
        except Exception:
            headers = {}
        try:
            cookies = getattr(self._inner, "cookies", None)
        except Exception:
            cookies = None
        try:
            self._inner.close()
        except Exception:
            pass
        sanitize_curl_ssl_env()
        value = self._chain[self._idx]
        if value:
            self._kwargs["impersonate"] = value
        else:
            self._kwargs.pop("impersonate", None)
        self._inner = self._factory(**self._kwargs)
        if headers:
            try:
                self._inner.headers.update(headers)
            except Exception:
                pass
        if cookies is not None:
            try:
                self._inner.cookies.update(cookies)
            except Exception:
                try:
                    self._inner.cookies = cookies
                except Exception:
                    pass

    def _mark_dead_proxy(self, reason: str) -> None:
        proxy = str(self._kwargs.get("proxy") or "")
        if not proxy:
            return
        try:
            from services.proxy_service import mark_egress_unusable

            mark_egress_unusable(proxy, reason)
        except Exception:
            pass

    def _next_fallback_idx(self) -> int | None:
        none_idx = next((i for i, item in enumerate(self._chain) if not item), None)
        if none_idx is not None and self._idx != none_idx:
            return none_idx
        nxt = self._idx + 1
        if nxt < len(self._chain):
            return nxt
        return None

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        last: BaseException | None = None
        while True:
            try:
                return getattr(self._inner, name)(*args, **kwargs)
            except Exception as exc:
                last = exc
                invalid_lib = is_openssl_invalid_library(str(exc))
                nxt = self._next_fallback_idx() if invalid_lib else None
                if nxt is None:
                    if invalid_lib:
                        self._mark_dead_proxy(str(exc))
                    raise
                self._idx = nxt
                try:
                    self._rebind()
                except Exception as rebind_exc:
                    last = rebind_exc
                    if is_openssl_invalid_library(str(rebind_exc)):
                        self._mark_dead_proxy(str(rebind_exc))
                    raise last
        raise last or RuntimeError("curl TLS library fallback exhausted")

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("get", *args, **kwargs)

    def post(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("post", *args, **kwargs)

    def put(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("put", *args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("delete", *args, **kwargs)

    def head(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("head", *args, **kwargs)

    def patch(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("patch", *args, **kwargs)

    def request(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("request", *args, **kwargs)

    def close(self) -> None:
        try:
            self._inner.close()
        except Exception:
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
