from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from utils.curl_tls import (
    create_cffi_session,
    impersonate_fallback_chain,
    is_openssl_invalid_library,
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
        chain = impersonate_fallback_chain("chrome110")
        self.assertEqual(chain[0], "chrome142")
        self.assertEqual(chain[-1], "")

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

    def test_session_retries_invalid_library_with_next_impersonate(self) -> None:
        first = MagicMock()
        first.get.side_effect = OSError(OPENSSL_INVALID)
        first.headers = {"User-Agent": "ua"}
        second = MagicMock()
        second.get.return_value = "ok"
        second.headers = {}
        factory = MagicMock(side_effect=[first, second])

        with patch("curl_cffi.requests.Session", factory):
            session = create_cffi_session(impersonate="chrome142", proxy="", verify=True)
            result = session.get("https://chatgpt.com")
            session.close()

        self.assertEqual(result, "ok")
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(factory.call_args_list[0].kwargs.get("impersonate"), "chrome142")
        self.assertNotIn("impersonate", factory.call_args_list[1].kwargs)

    def test_invalid_library_exhaust_marks_proxy(self) -> None:
        first = MagicMock()
        first.get.side_effect = OSError(OPENSSL_INVALID)
        first.headers = {}
        second = MagicMock()
        second.get.side_effect = OSError(OPENSSL_INVALID)
        second.headers = {}
        factory = MagicMock(side_effect=[first, second])

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
                mark.assert_called_once()
