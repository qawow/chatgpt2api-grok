"""Isolate curl_cffi (BoringSSL) from Debian OpenSSL on Linux/WSL2/Docker.

``OPENSSL_internal:invalid library`` is a local library-init failure, not a
remote handshake problem. On SOCKS it shows up as ``curl: (35)`` when
impersonate uses HTTP/2. The same proxy works with HTTP/1.1.
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


def _proxy_scheme(url: object) -> str:
    text = str(url or "").strip()
    if not text:
        return "direct"
    if "://" not in text:
        return "unknown"
    return text.split("://", 1)[0]


def is_socks_proxy(url: object) -> bool:
    scheme = _proxy_scheme(url)
    return scheme in {"socks", "socks5", "socks5h"}


def is_openssl_invalid_library(message: str) -> bool:
    text = str(message or "").lower()
    return "openssl_internal" in text and "invalid library" in text


def _http11_constant():
    from curl_cffi import CurlHttpVersion

    return CurlHttpVersion.V1_1


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
    return [resolve_session_impersonate(value)]


def create_cffi_session(**session_kwargs: Any):
    """curl_cffi Session that retries OPENSSL_internal:invalid library via HTTP/1.1."""
    from curl_cffi.requests import Session

    sanitize_curl_ssl_env()
    kwargs = dict(session_kwargs)
    chain = impersonate_fallback_chain(kwargs.get("impersonate"))
    if chain[0]:
        kwargs["impersonate"] = chain[0]
    else:
        kwargs.pop("impersonate", None)
    # Impersonate forces HTTP/2 on Session(); that trips OPENSSL_internal on
    # SOCKS. Force HTTP/1.1 on each request instead of the constructor.
    prefer_http11 = is_socks_proxy(kwargs.get("proxy"))
    return _TlsLibraryFallbackSession(Session, chain, kwargs, prefer_http11=prefer_http11)


class _TlsLibraryFallbackSession:
    def __init__(
        self,
        factory,
        chain: list[str],
        session_kwargs: dict[str, Any],
        *,
        prefer_http11: bool = False,
    ) -> None:
        self._factory = factory
        self._chain = list(chain)
        self._kwargs = dict(session_kwargs)
        self._idx = 0
        self._http11 = bool(prefer_http11)
        self._openssl_retries = 0
        self._inner = self._factory(**self._kwargs)

    def _recreate_inner(self) -> None:
        headers = {}
        try:
            headers = dict(self._inner.headers)
        except Exception:
            headers = {}
        try:
            self._inner.close()
        except Exception:
            pass
        self._inner = self._factory(**self._kwargs)
        if headers:
            try:
                self._inner.headers.update(headers)
            except Exception:
                pass

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        last: BaseException | None = None
        while True:
            call_kwargs = dict(kwargs)
            if self._http11:
                call_kwargs.setdefault("http_version", _http11_constant())
            try:
                return getattr(self._inner, name)(*args, **call_kwargs)
            except Exception as exc:
                last = exc
                if not is_openssl_invalid_library(str(exc)):
                    raise
                self._http11 = True
                if self._openssl_retries >= 2:
                    raise
                self._openssl_retries += 1
                if self._openssl_retries > 1 or is_socks_proxy(self._kwargs.get("proxy")):
                    self._recreate_inner()
                continue
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
