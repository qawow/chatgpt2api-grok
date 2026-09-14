"""Shared SSRF guard for every fetch of a user-supplied URL.

Centralizes what used to be an ad-hoc check in ``api/image_inputs`` (and what
was missing entirely in ``utils/helper``) so all remote-reference fetches follow
one rule set:

* scheme allowlist — ``http``/``https`` only (no ``file://``, ``gopher://`` …);
* private / loopback / link-local / reserved / multicast / unspecified address
  rejection, covering IPv4, IPv6 and IPv4-mapped IPv6 (``::ffff:127.0.0.1``),
  including hostnames that *resolve* to such addresses (``127.0.0.1.nip.io``,
  obfuscated numeric hosts like ``2130706433`` / ``0x7f.1``);
* fail-closed DNS — a host we cannot verify as public is refused. A proxied
  fetch (``socks5h://`` = remote DNS) would otherwise resolve an internal name
  through the proxy even though the local resolver has no answer;
* per-hop validation of redirect chains (see :func:`fetch_following_redirects`),
  because ``allow_redirects=True`` lets a public URL 302 into ``169.254.169.254``.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit

# Redirect hops we are willing to walk when validating each Location.
MAX_SAFE_REDIRECTS = 5

_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})


class UnsafeUrlError(ValueError):
    """Raised when a URL (or a redirect target) is not safe to fetch."""


def _address_is_risky(ip: ipaddress._BaseAddress) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def host_is_private(host: str, *, resolve: bool = True) -> bool:
    """True if ``host`` is — or resolves to — a non-public address.

    IP literals (including IPv4-mapped IPv6) are checked directly. Hostnames are
    resolved; on resolver failure this returns ``True`` (fail-closed) unless
    ``resolve`` is False, so callers that only want literal checks stay literal.
    """
    candidate = str(host or "").strip().strip("[]")
    if not candidate:
        return True
    try:
        return _address_is_risky(ipaddress.ip_address(candidate))
    except ValueError:
        pass  # Not an IP literal — treat as a hostname.
    if not resolve:
        return False
    try:
        addr_info = socket.getaddrinfo(candidate, None)
    except (socket.gaierror, OSError):
        # We cannot prove this host is public. A proxied request with remote
        # DNS may still resolve it to something internal, so refuse it.
        return True
    for _family, _type, _proto, _canon, sockaddr in addr_info:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError):
            continue
        if _address_is_risky(ip):
            return True
    return False


def assert_safe_url(url: str) -> None:
    """Raise :class:`UnsafeUrlError` unless ``url`` is a public http(s) URL."""
    raw = str(url or "").strip()
    if not raw:
        raise UnsafeUrlError("url is empty")
    parsed = urlsplit(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise UnsafeUrlError(f"unsupported url scheme: {scheme or '<none>'}")
    host = parsed.hostname or ""
    if not host:
        raise UnsafeUrlError("url has no host")
    if host_is_private(host):
        raise UnsafeUrlError(f"host resolves to a private or local network address: {host}")


def fetch_following_redirects(
    fetch,
    url: str,
    *,
    max_redirects: int = MAX_SAFE_REDIRECTS,
) -> Any:
    """Call ``fetch(url, allow_redirects=False)`` and walk the redirect chain.

    ``fetch`` must accept ``allow_redirects=False`` and return a response object
    exposing ``status_code`` / ``headers`` / ``close()``. Every hop — including
    the initial URL, which the caller validates separately — is re-validated so
    a public URL cannot bounce into an internal one. Relative ``Location``
    values are resolved against the current URL.
    """
    response = fetch(url)
    current_url = url
    seen = {url}
    for _ in range(max(0, max_redirects)):
        status = getattr(response, "status_code", None)
        if status not in _REDIRECT_STATUS_CODES:
            return response
        headers = getattr(response, "headers", None)
        location = headers.get("location") if headers is not None else None
        if not location:
            return response
        next_url = urljoin(current_url, str(location).strip())
        if not next_url:
            return response
        assert_safe_url(next_url)
        if next_url in seen:
            raise UnsafeUrlError("redirect loop detected")
        seen.add(next_url)
        try:
            response.close()
        except Exception:
            pass
        response = fetch(next_url)
        current_url = next_url
    raise UnsafeUrlError(f"exceeded {max_redirects} redirects")
