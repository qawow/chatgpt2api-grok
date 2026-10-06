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

    def test_socks_openssl_recreate_keeps_session_cookie(self) -> None:
        class FakeCookie:
            def __init__(self) -> None:
                self.name = "__Secure-next-auth.session-token"
                self.value = "sess"
                self.domain = ".chatgpt.com"
                self.path = "/"

        class FakeJar(list):
            def set(self, name, value, domain="", path="/"):
                self.append(type("C", (), {
                    "name": name, "value": value, "domain": domain, "path": path,
                })())

        inner_fail = MagicMock()
        inner_fail.get.side_effect = OSError(OPENSSL_INVALID)
        inner_fail.headers = {"Authorization": "Bearer x"}
        inner_fail.cookies = [FakeCookie()]
        inner_ok = MagicMock()
        inner_ok.get.return_value = "ok"
        inner_ok.headers = {}
        inner_ok.cookies = FakeJar()
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
        self.assertEqual(inner_ok.cookies[0].name, "__Secure-next-auth.session-token")
        self.assertEqual(inner_ok.cookies[0].value, "sess")

    def test_tls_recovery_only_uses_remaining_timeout(self) -> None:
        inner = MagicMock()
        inner.get.side_effect = [OSError(OPENSSL_INVALID), "ok"]
        with patch("curl_cffi.requests.Session", return_value=inner), patch("utils.curl_tls.time.monotonic", side_effect=[10.0, 11.5]):
            session = create_cffi_session(proxy="")
            self.assertEqual(session.get("https://example.invalid", timeout=3.0), "ok")
        self.assertEqual(inner.get.call_args_list[0].kwargs["timeout"], 3.0)
        self.assertEqual(inner.get.call_args_list[1].kwargs["timeout"], 1.5)

    def test_expired_tls_recovery_budget_stops_before_retry(self) -> None:
        inner = MagicMock()
        inner.get.side_effect = OSError(OPENSSL_INVALID)
        with patch("curl_cffi.requests.Session", return_value=inner), patch("utils.curl_tls.time.monotonic", side_effect=[10.0, 14.0]):
            session = create_cffi_session(proxy="")
            with self.assertRaises(TimeoutError):
                session.get("https://example.invalid", timeout=3.0)
        self.assertEqual(inner.get.call_count, 1)

    def test_invalid_library_counter_resets_after_success(self) -> None:
        inner = MagicMock()
        inner.get.side_effect = [
            OSError(OPENSSL_INVALID),
            "ok1",
            OSError(OPENSSL_INVALID),
            "ok2",
            OSError(OPENSSL_INVALID),
            "ok3",
        ]
        inner.headers = {}
        factory = MagicMock(return_value=inner)

        with patch("curl_cffi.requests.Session", factory):
            session = create_cffi_session(impersonate="chrome142", proxy="", verify=True)
            self.assertEqual(session.get("https://chatgpt.com/a"), "ok1")
            self.assertEqual(session.get("https://chatgpt.com/b"), "ok2")
            self.assertEqual(session.get("https://chatgpt.com/c"), "ok3")
            session.close()

        self.assertEqual(len(inner.get.call_args_list), 6)

    def test_stream_call_sets_low_speed_idle_guard(self) -> None:
        from curl_cffi.const import CurlOpt

        inner = MagicMock()
        inner.get.return_value = "ok"
        with patch("curl_cffi.requests.Session", return_value=inner):
            session = create_cffi_session(proxy="")
            self.assertEqual(session.get("https://example.invalid", stream=True, timeout=60), "ok")
            session.close()

        set_calls = [call.args for call in inner.curl.setopt.call_args_list if call.args]
        self.assertTrue(any(c[0] == CurlOpt.LOW_SPEED_LIMIT and c[1] == 1 for c in set_calls))
        self.assertTrue(any(c[0] == CurlOpt.LOW_SPEED_TIME and c[1] == 60.0 for c in set_calls))

    def test_stream_idle_guard_window_is_clamped(self) -> None:
        from curl_cffi.const import CurlOpt

        for timeout, expected in ((300, 180.0), (5, 30.0), (None, 180.0)):
            inner = MagicMock()
            inner.get.return_value = "ok"
            with patch("curl_cffi.requests.Session", return_value=inner):
                session = create_cffi_session(proxy="")
                session.get("https://example.invalid", stream=True, timeout=timeout)
                session.close()
            windows = [
                c[1] for c in (call.args for call in inner.curl.setopt.call_args_list if call.args)
                if c[0] == CurlOpt.LOW_SPEED_TIME
            ]
            self.assertEqual(windows, [expected], f"timeout={timeout}")

    def test_non_stream_call_skips_idle_guard(self) -> None:
        inner = MagicMock()
        inner.get.return_value = "ok"
        with patch("curl_cffi.requests.Session", return_value=inner):
            session = create_cffi_session(proxy="")
            session.get("https://example.invalid", timeout=60)
            session.close()
        inner.curl.setopt.assert_not_called()


class RequestTimeoutBudgetTests(unittest.TestCase):
    """_request_timeout locks the scalar-request budget to the task deadline."""

    def test_timeout_is_bounded_by_task_control(self) -> None:
        from types import SimpleNamespace

        from services.openai_backend_api import OpenAIBackendAPI

        backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        backend.task_control = SimpleNamespace(remaining=lambda maximum: 42.0)
        self.assertEqual(backend._request_timeout(1200), 42.0)

    def test_timeout_defaults_to_maximum_without_control(self) -> None:
        from services.openai_backend_api import OpenAIBackendAPI

        backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        backend.task_control = None
        self.assertEqual(backend._request_timeout(300), 300)
