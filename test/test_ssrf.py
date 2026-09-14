from __future__ import annotations

import os
import socket
import unittest
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "chatgpt2api")

from utils.ssrf import (
    UnsafeUrlError,
    assert_safe_url,
    fetch_following_redirects,
    host_is_private,
)

# Deterministic DNS so the tests never depend on live resolution.
_PUBLIC_IP = "93.184.216.34"
_PRIVATE_IP = "10.1.2.3"
_PUBLIC_HOSTS = {"example.com", "cdn.example.com"}
_PRIVATE_HOSTS = {
    "localhost",
    "127.0.0.1.nip.io",
    "no-such-internal-host.invalid",
    "internal.corp.local",
}


def _fake_getaddrinfo(host, *args, **kwargs):
    if host in _PRIVATE_HOSTS:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PRIVATE_IP, 0))]
    if host in _PUBLIC_HOSTS:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 0))]
    raise socket.gaierror("no DNS in tests")


@mock.patch("utils.ssrf.socket.getaddrinfo", side_effect=_fake_getaddrinfo)
class HostClassificationTests(unittest.TestCase):
    def test_private_ip_literals_are_rejected(self, _getaddrinfo) -> None:
        for host in ("127.0.0.1", "10.0.0.1", "192.168.1.5", "172.16.0.1", "169.254.169.254",
                     "::1", "[::]", "0.0.0.0", "::ffff:127.0.0.1"):
            with self.subTest(host=host):
                self.assertTrue(host_is_private(host.strip("[]")), f"{host} should be private")

    def test_public_ip_literals_are_allowed(self, _getaddrinfo) -> None:
        for host in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
            with self.subTest(host=host):
                self.assertFalse(host_is_private(host))

    def test_local_names_resolve_to_private(self, _getaddrinfo) -> None:
        for host in ("localhost", "127.0.0.1.nip.io"):
            with self.subTest(host=host):
                self.assertTrue(host_is_private(host))

    def test_unresolvable_host_is_fail_closed(self, _getaddrinfo) -> None:
        # A host we cannot verify as public must be refused: a proxied request
        # with remote DNS may still resolve it to an internal address.
        self.assertTrue(host_is_private("no-such-internal-host.invalid"))

    def test_literal_only_mode_skips_resolution(self, _getaddrinfo) -> None:
        self.assertFalse(host_is_private("no-such-internal-host.invalid", resolve=False))


@mock.patch("utils.ssrf.socket.getaddrinfo", side_effect=_fake_getaddrinfo)
class AssertSafeUrlTests(unittest.TestCase):
    def test_rejects_private_targets(self, _getaddrinfo) -> None:
        for url in (
            "http://127.0.0.1:8080/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "http://10.1.2.3/x.png",
            "http://localhost:9000/",
            "http://internal.corp.local/secret",
        ):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeUrlError):
                    assert_safe_url(url)

    def test_rejects_non_http_schemes(self, _getaddrinfo) -> None:
        for url in ("file:///etc/passwd", "gopher://127.0.0.1:6379/_INFO", "ftp://example.com/a.png"):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeUrlError):
                    assert_safe_url(url)

    def test_rejects_empty_and_hostless(self, _getaddrinfo) -> None:
        for url in ("", "http:///path", ":///x"):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeUrlError):
                    assert_safe_url(url)

    def test_allows_public_urls(self, _getaddrinfo) -> None:
        assert_safe_url("https://example.com/a.png")
        assert_safe_url("http://93.184.216.34/index.html")


class FakeResponse:
    def __init__(self, status_code: int, location: str | None = None):
        self.status_code = status_code
        self.headers = {"location": location} if location is not None else {}
        self.closed = False

    def close(self) -> None:
        self.closed = True


@mock.patch("utils.ssrf.socket.getaddrinfo", side_effect=_fake_getaddrinfo)
class RedirectWalkTests(unittest.TestCase):
    def _make_fetch(self, chain: dict[str, tuple[int, str | None]]):
        calls: list[str] = []

        def fetch(url: str):
            calls.append(url)
            status, location = chain[url]
            return FakeResponse(status, location)

        return fetch, calls

    def test_returns_terminal_response(self, _getaddrinfo) -> None:
        fetch, calls = self._make_fetch({
            "http://example.com/a.png": (200, None),
        })
        response = fetch_following_redirects(fetch, "http://example.com/a.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["http://example.com/a.png"])

    def test_follows_relative_and_absolute_redirects(self, _getaddrinfo) -> None:
        fetch, calls = self._make_fetch({
            "http://example.com/a.png": (302, "/final.png"),
            "http://example.com/final.png": (301, "https://cdn.example.com/final.png"),
            "https://cdn.example.com/final.png": (200, None),
        })
        response = fetch_following_redirects(fetch, "http://example.com/a.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [
            "http://example.com/a.png",
            "http://example.com/final.png",
            "https://cdn.example.com/final.png",
        ])

    def test_rejects_redirect_into_private_network(self, _getaddrinfo) -> None:
        fetch, _ = self._make_fetch({
            "http://example.com/a.png": (302, "http://169.254.169.254/latest/meta-data/"),
        })
        with self.assertRaises(UnsafeUrlError):
            fetch_following_redirects(fetch, "http://example.com/a.png")

    def test_detects_redirect_loop(self, _getaddrinfo) -> None:
        fetch, _ = self._make_fetch({
            "http://example.com/a": (302, "/b"),
            "http://example.com/b": (302, "/a"),
        })
        with self.assertRaises(UnsafeUrlError):
            fetch_following_redirects(fetch, "http://example.com/a")

    def test_caps_redirect_chain(self, _getaddrinfo) -> None:
        chain = {f"http://example.com/{i}": (302, f"/{i + 1}") for i in range(20)}
        fetch, _ = self._make_fetch(chain)
        with self.assertRaises(UnsafeUrlError):
            fetch_following_redirects(fetch, "http://example.com/0", max_redirects=3)


if __name__ == "__main__":
    unittest.main()
