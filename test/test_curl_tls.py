from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from utils.curl_tls import (
    create_cffi_session,
    impersonate_fallback_chain,
    is_openssl_invalid_library,
    is_socks_proxy,
    resolve_session_impersonate,
    sanitize_curl_ssl_env,
)


OPENSSL_INVALID = (
    "Failed to perform, curl: (35) TLS connect error: error:00000000:"
    "invalid library (0):OPENSSL_internal:invalid library (0)"
)


class CurlTlsHelperTests(unittest.TestCase):
    def test_legacy_impersonate_is_upgraded(self) -> None:
        self.assertEqual(resolve_session_impersonate("chrome110"), "chrome142")
        self.assertEqual(resolve_session_impersonate("chrome"), "chrome142")
        self.assertEqual(resolve_session_impersonate("chrome136"), "chrome136")
        self.assertEqual(impersonate_fallback_chain("chrome110"), ["chrome142"])

    def test_detects_socks_proxy(self) -> None:
        self.assertTrue(is_socks_proxy("socks5h://127.0.0.1:1080"))
        self.assertFalse(is_socks_proxy("http://127.0.0.1:8080"))
        self.assertFalse(is_socks_proxy(""))

    def test_detects_invalid_library(self) -> None:
        self.assertTrue(is_openssl_invalid_library(OPENSSL_INVALID))
        self.assertFalse(is_openssl_invalid_library("curl: (35) TLS connect error"))

    def test_sanitize_drops_system_openssl_conf(self) -> None:
        old = os.environ.get("OPENSSL_CONF")
        os.environ["OPENSSL_CONF"] = "/usr/lib/ssl/openssl.cnf"
        try:
            removed = sanitize_curl_ssl_env()
            self.assertIn("OPENSSL_CONF", removed)
            self.assertNotIn("OPENSSL_CONF", os.environ)
        finally:
            if old is None:
                os.environ.pop("OPENSSL_CONF", None)
            else:
                os.environ["OPENSSL_CONF"] = old

    def test_socks_session_starts_on_http11(self) -> None:
        factory = MagicMock()
        inner = MagicMock()
        inner.get.return_value = "ok"
        inner.headers = {}
        factory.return_value = inner

        with patch("curl_cffi.requests.Session", factory):
            session = create_cffi_session(
                impersonate="chrome142",
                proxy="socks5h://example.invalid:1080",
                verify=True,
            )
            result = session.get("https://chatgpt.com")
            session.close()

        self.assertEqual(result, "ok")
        self.assertEqual(factory.call_count, 1)
        self.assertNotIn("http_version", factory.call_args.kwargs)
        self.assertIn("http_version", inner.get.call_args.kwargs)
        self.assertEqual(factory.call_args.kwargs.get("impersonate"), "chrome142")

    def test_invalid_library_retries_with_http11(self) -> None:
        inner = MagicMock()
        inner.get.side_effect = [OSError(OPENSSL_INVALID), "ok"]
        inner.headers = {"User-Agent": "ua"}
        factory = MagicMock(return_value=inner)

        with patch("curl_cffi.requests.Session", factory):
            session = create_cffi_session(impersonate="chrome142", proxy="", verify=True)
            result = session.get("https://chatgpt.com")
            session.close()

        self.assertEqual(result, "ok")
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(len(inner.get.call_args_list), 2)
        self.assertNotIn("http_version", inner.get.call_args_list[0].kwargs)
        self.assertIn("http_version", inner.get.call_args_list[1].kwargs)

    def test_invalid_library_does_not_mark_proxy(self) -> None:
        inner = MagicMock()
        inner.get.side_effect = OSError(OPENSSL_INVALID)
        inner.headers = {}
        factory = MagicMock(return_value=inner)

        with patch("curl_cffi.requests.Session", factory):
            with patch("services.proxy_service.mark_egress_unusable") as mark:
                session = create_cffi_session(
                    impersonate="chrome142",
                    proxy="socks5h://example.invalid:1080",
                    verify=True,
                )
                with self.assertRaises(OSError):
                    session.get("https://chatgpt.com")
                session.close()
                mark.assert_not_called()
        self.assertGreaterEqual(factory.call_count, 2)

    def test_socks_openssl_recreates_session(self) -> None:
        inner_fail = MagicMock()
        inner_fail.get.side_effect = OSError(OPENSSL_INVALID)
        inner_fail.headers = {"Authorization": "Bearer x"}
        inner_ok = MagicMock()
        inner_ok.get.return_value = "ok"
        inner_ok.headers = {}
        factory = MagicMock(side_effect=[inner_fail, inner_ok])

        with patch("curl_cffi.requests.Session", factory):
            session = create_cffi_session(
                impersonate="chrome142",
                proxy="socks5h://example.invalid:1080",
                verify=True,
            )
            result = session.get("https://chatgpt.com")
            session.close()

        self.assertEqual(result, "ok")
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(inner_ok.headers.get("Authorization"), "Bearer x")
