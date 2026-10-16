"""Proxy routing regressions with loopback fixtures; no external services."""
from __future__ import annotations

import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

import requests

from services import content_filter, grok_backend_api, openai_backend_api, proxy_service
from services.protocol import conversation
from utils import egress_locale
from utils.curl_tls import create_cffi_session
from utils.ssrf import UnsafeUrlError, fetch_following_redirects


def store(legacy="", runtime=None, provider=None):
    return proxy_service.ProxySettingsStore(SimpleNamespace(
        get_proxy_settings=lambda: legacy,
        get_proxy_runtime_settings=lambda: runtime or {},
    ), clearance_provider_factory=provider)


class CurlProxyEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.origin = self.server(b"origin")
        self.proxy = self.server(b"configured proxy")
        self.target = f"http://127.0.0.1:{self.origin.server_port}/fixture"

    def server(self, content):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *args):
                pass

        fixture = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=fixture.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(fixture.server_close)
        self.addCleanup(fixture.shutdown)
        return fixture

    def test_direct_setting_ignores_dead_environment_proxies(self):
        poisoned = {name: "http://127.0.0.1:1" for name in (
            "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY",
        )}
        poisoned.update(no_proxy="", NO_PROXY="")
        with mock.patch.dict(os.environ, poisoned):
            session = create_cffi_session(**store().build_session_kwargs(upstream=True))
            try:
                self.assertEqual(session.get(self.target, timeout=2).content, b"origin")
            finally:
                session.close()

    def test_explicit_proxy_ignores_environment_no_proxy(self):
        proxy_url = f"http://127.0.0.1:{self.proxy.server_port}"
        with mock.patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}):
            session = create_cffi_session(**store(legacy=proxy_url).build_session_kwargs(upstream=True))
            try:
                self.assertEqual(session.get(self.target, timeout=2).content, b"configured proxy")
            finally:
                session.close()


class BoundAccountProxyTests(unittest.TestCase):
    def test_failed_shared_bound_proxy_never_creates_a_direct_backend(self):
        created = []
        bound_proxy = "socks5h://bound.invalid:1080"

        class Backend:
            def __init__(self, access_token, *, force_proxy=None):
                created.append((access_token, force_proxy))

            def close(self):
                pass

        with (
            mock.patch.object(conversation, "account_service") as accounts,
            mock.patch.object(conversation, "proxy_settings", store(legacy="http://global.invalid:8080")),
            mock.patch.object(conversation, "OpenAIBackendAPI", Backend),
            mock.patch.object(conversation, "stream_image_outputs", side_effect=OSError("connection refused")),
            mock.patch.object(conversation.time, "sleep"),
        ):
            accounts.get_available_access_token.side_effect = ["fixture-a", "fixture-b", RuntimeError("no available image quota")]
            accounts.get_account.return_value = {"proxy": bound_proxy}
            with self.assertRaises(conversation.ImageGenerationError):
                conversation._generate_single_image(conversation.ConversationRequest(prompt="fixture", model="gpt-image-2"), 1, 1)
        self.assertEqual(created, [("fixture-a", bound_proxy)])


class ClearanceRouteTests(unittest.TestCase):
    def setUp(self):
        self.primary = "http://primary.invalid:8080"
        self.fallback = "http://fallback.invalid:8080"
        self.provider = mock.Mock()
        self.provider.get_clearance.side_effect = lambda target, proxy_url, timeout_sec: proxy_service.ClearanceBundle(
            target_host="chatgpt.com", proxy_url=proxy_url, cookies={"cf_clearance": "fixture-cookie"},
        )
        self.settings = store(legacy=self.fallback, runtime={
            "enabled": True, "egress_mode": "single_proxy", "proxy_url": self.primary,
            "clearance": {"enabled": True, "mode": "flaresolverr", "flaresolverr_url": "http://solver.invalid", "refresh_interval": 60},
        }, provider=lambda _: self.provider)

    def test_force_route_cache_never_uses_primary_cookie(self):
        self.settings.refresh_clearance()
        self.assertIn("Cookie", self.settings.build_headers())
        for route in (self.fallback, ""):
            self.assertNotIn("Cookie", self.settings.build_headers(force_proxy=route))
        self.settings.refresh_clearance(force_proxy=self.fallback)
        self.assertIn("Cookie", self.settings.build_headers(force_proxy=self.fallback))
        self.settings.invalidate_clearance(force_proxy=self.fallback)
        self.assertNotIn("Cookie", self.settings.build_headers(force_proxy=self.fallback))
        self.assertIn("Cookie", self.settings.build_headers())

    def test_expired_cookie_is_not_reported_or_returned_after_refresh_failure(self):
        with mock.patch.object(proxy_service.time, "time", return_value=1000):
            self.settings.refresh_clearance()
        self.provider.get_clearance.return_value = None
        self.provider.get_clearance.side_effect = None
        with mock.patch.object(proxy_service.time, "time", return_value=2000):
            self.assertIsNone(self.settings.refresh_clearance(force=True))
            self.assertFalse(self.settings.get_runtime_status()["has_clearance_bundle"])

    def test_backend_locale_and_headers_follow_actual_fallback_route(self):
        self.settings.refresh_clearance()
        session = mock.Mock(headers={})
        with (
            mock.patch.object(openai_backend_api, "proxy_settings", self.settings),
            mock.patch.object(openai_backend_api.account_service, "get_account", return_value={}),
            mock.patch.object(openai_backend_api.account_service, "update_account", return_value={}),
            mock.patch.object(proxy_service, "is_egress_unusable", side_effect=lambda url: url == self.primary),
            mock.patch.object(openai_backend_api, "create_cffi_session", return_value=session) as factory,
            mock.patch.object(openai_backend_api, "resolve_egress_locale", return_value=egress_locale.default_locale()) as locale,
        ):
            backend = openai_backend_api.OpenAIBackendAPI("fixture-token")
            try:
                self.assertEqual(factory.call_args.kwargs["proxy"], self.fallback)
                locale.assert_called_once_with(self.fallback)
                self.assertNotIn("Cookie", backend._headers("/backend-api/me"))
            finally:
                backend.close()


class GrokProxyBudgetTests(unittest.TestCase):
    def test_ssl_setting_and_remaining_budget_follow_runtime(self):
        clock = {"now": 100.0}
        calls = []
        settings = store(legacy="http://legacy.invalid:8080", runtime={
            "enabled": True, "egress_mode": "single_proxy", "proxy_url": "http://primary.invalid:8080", "skip_ssl_verify": True,
        })
        response = object()

        def post(url, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                clock["now"] += 4
                raise requests.ConnectionError("connection refused")
            return response

        with (
            mock.patch.object(proxy_service, "proxy_settings", settings),
            mock.patch.object(proxy_service, "mark_egress_unusable"),
            mock.patch.object(grok_backend_api, "_http", return_value=SimpleNamespace(post=post)),
            mock.patch.object(grok_backend_api.time, "monotonic", side_effect=lambda: clock["now"]),
        ):
            self.assertIs(grok_backend_api._request("post", "https://fixture.invalid", timeout=5), response)
        self.assertEqual([call["timeout"] for call in calls], [5, 1])
        self.assertTrue(all(call["verify"] is False for call in calls))

    def test_budget_exhaustion_does_not_retry_another_egress(self):
        clock = {"now": 0.0}
        http = mock.Mock()

        def fail(*args, **kwargs):
            clock["now"] = 6.0
            raise requests.ConnectionError("connection refused")

        http.get.side_effect = fail
        with (
            mock.patch.object(proxy_service, "proxy_settings", store(legacy="http://fixture.invalid:8080")),
            mock.patch.object(proxy_service, "mark_egress_unusable"),
            mock.patch.object(grok_backend_api, "_http", return_value=http),
            mock.patch.object(grok_backend_api.time, "monotonic", side_effect=lambda: clock["now"]),
        ):
            with self.assertRaises(requests.Timeout):
                grok_backend_api._request("get", "https://fixture.invalid", timeout=5)
        http.get.assert_called_once()


class ResourceFetchRecoveryTests(unittest.TestCase):
    def test_failed_proxy_is_remembered_for_later_downloads(self):
        proxy_service.reset_unusable_egress()
        self.addCleanup(proxy_service.reset_unusable_egress)
        gateway = "http://dead.invalid:8080"
        settings = store(legacy=gateway)
        bad, good = mock.Mock(), mock.Mock()
        bad.get.side_effect = OSError("connection refused")
        good.get.return_value = mock.Mock(status_code=200, content=b"fixture", headers={})
        with (
            mock.patch.object(type(proxy_service.config), "egress_blacklist_failure_threshold", new_callable=mock.PropertyMock, return_value=1),
            mock.patch.object(proxy_service, "create_cffi_session", side_effect=[bad, good, good]) as factory,
        ):
            self.assertEqual(settings.get_with_egress_fallback("https://fixture.invalid").content, b"fixture")
            self.assertTrue(proxy_service.is_egress_unusable(gateway))
            self.assertEqual(settings.get_with_egress_fallback("https://fixture.invalid").content, b"fixture")
        self.assertEqual([call.kwargs["proxy"] for call in factory.call_args_list], [gateway, "", ""])

    def test_redirect_hops_use_remaining_budget(self):
        clock = {"now": 0.0}
        timeouts = []
        redirected = mock.Mock(status_code=302, headers={"location": "https://fixture.invalid/second"})
        final = mock.Mock(status_code=200, content=b"fixture", headers={})

        def get(url, **kwargs):
            timeouts.append(kwargs["timeout"])
            if len(timeouts) == 1:
                clock["now"] += 4
            return redirected if len(timeouts) == 1 else final

        session = mock.Mock()
        session.get.side_effect = get
        with (
            mock.patch.object(proxy_service, "assert_safe_url"),
            mock.patch("utils.ssrf.assert_safe_url"),
            mock.patch.object(proxy_service.time, "monotonic", side_effect=lambda: clock["now"]),
            mock.patch.object(proxy_service, "create_cffi_session", return_value=session),
        ):
            self.assertEqual(store().get_with_egress_fallback("https://fixture.invalid/first", timeout=5, ssrf_guard=True).content, b"fixture")
        self.assertEqual(timeouts, [5, 1])
        redirected.close.assert_called_once()
        session.close.assert_called_once()

    def test_rejected_redirect_closes_streamed_response(self):
        response = mock.Mock(status_code=302, headers={"location": "http://127.0.0.1/admin"})
        with self.assertRaises(UnsafeUrlError):
            fetch_following_redirects(lambda url: response, "https://fixture.invalid")
        response.close.assert_called_once()


class ProxySessionLifecycleTests(unittest.TestCase):
    def test_review_failure_closes_session(self):
        session = mock.Mock()
        session.post.side_effect = OSError("connection refused")
        review = SimpleNamespace(sensitive_words=[], ai_review={
            "enabled": True, "base_url": "https://review.invalid", "model": "fixture", "api_key": "fixture", "fail_open": True,
        })
        with (
            mock.patch.object(content_filter, "config", review),
            mock.patch.object(content_filter, "proxy_settings", store()),
            mock.patch.object(content_filter, "create_cffi_session", return_value=session),
        ):
            content_filter.check_request("fixture")
        session.close.assert_called_once()

    def test_locale_probe_keeps_explicit_direct_route_and_closes_session(self):
        session = mock.Mock()
        session.get.side_effect = OSError("fixture probe unavailable")
        with mock.patch("utils.curl_tls.create_cffi_session", return_value=session) as factory:
            with self.assertRaises(OSError):
                egress_locale._fetch_locale("https://locale.invalid", None)
        self.assertEqual(factory.call_args.kwargs["proxy"], "")
        session.close.assert_called_once()
