from __future__ import annotations

import threading
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlparse

from curl_cffi.requests import Cookies
from fastapi.testclient import TestClient

from api import app as app_module
from services import openai_backend_api, proxy_service
from utils.curl_tls import create_cffi_session
from utils.egress_locale import default_locale


def response(status=200, *, challenge=False, content=b"fixture"):
    result = mock.Mock(status_code=status, content=content)
    result.headers = {"cf-mitigated": "challenge"} if challenge else {}
    return result


def settings(*, enabled=True, warm=False, mode="flaresolverr", codes=None):
    runtime = {
        "enabled": enabled, "egress_mode": "single_proxy", "proxy_url": "http://api.invalid:8080",
        "resource_proxy_url": "http://cdn.invalid:8080", "reset_session_status_codes": codes or [403],
        "clearance": {"enabled": True, "mode": mode, "warm_up_on_start": warm,
                      "flaresolverr_url": "http://solver.invalid", "timeout_sec": 10, "refresh_interval": 300,
                      "cf_clearance": "manual-cookie", "user_agent": "Manual UA"},
    }
    provider = mock.Mock()
    provider.get_clearance.side_effect = lambda target, proxy_url, timeout_sec: proxy_service.ClearanceBundle(
        target_host=urlparse(target).hostname, proxy_url=proxy_url,
        cookies={"cf_clearance": "solved-cookie"}, user_agent="Solved UA",
    )
    store = proxy_service.ProxySettingsStore(SimpleNamespace(
        get_proxy_settings=lambda: "", get_proxy_runtime_settings=lambda: runtime,
    ), clearance_provider_factory=lambda _: provider)
    return store, provider


class ClearanceRecoveryTests(unittest.TestCase):
    def test_real_local_http_challenge_replays_same_json_with_solver_identity(self):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                calls.append(({key.lower(): value for key, value in self.headers.items()}, json.loads(body)))
                solved = "cf_clearance=solved-cookie" in self.headers.get("Cookie", "")
                self.send_response(200 if solved else 403)
                if not solved:
                    self.send_header("cf-mitigated", "challenge")
                self.send_header("Content-Length", "7")
                self.end_headers()
                self.wfile.write(b"fixture")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        store, provider = settings()
        session = store.create_session(force_proxy="", headers={"Authorization": "Bearer fixture", "Sec-Ch-Ua": "old"})
        try:
            result = session.post(f"http://127.0.0.1:{server.server_port}/fixture", json={"fixture": 1}, timeout=5)
            self.assertEqual(result.status_code, 200)
        finally:
            session.close()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(calls[1][0]["user-agent"], "Solved UA")
        self.assertEqual(calls[1][0]["authorization"], "Bearer fixture")
        self.assertNotIn("sec-ch-ua", {key.lower() for key in calls[1][0]})
        provider.get_clearance.assert_called_once()

    def test_challenge_refreshes_once_with_matching_cookie_ua_and_same_account(self):
        store, provider = settings()
        blocked, ok = response(403, challenge=True), response()
        raw = mock.Mock(headers={"Authorization": "Bearer fixture", "User-Agent": "Old UA", "Sec-Ch-Ua": "old"})
        raw.post.side_effect = [blocked, ok]
        account = {"email": "a@example.test", "proxy": "http://bound.invalid:8080"}
        session = store.wrap_session(raw, proxy_url=account["proxy"], account=account)
        self.assertIs(session.post("https://chatgpt.com/backend-api/conversation", json={"prompt": "fixture"}, timeout=10), ok)
        self.assertEqual(raw.post.call_count, 2)
        blocked.close.assert_called_once()
        raw.reset.assert_called_once()
        self.assertEqual(provider.get_clearance.call_args.kwargs["proxy_url"], account["proxy"])
        sent = raw.post.call_args.kwargs
        self.assertEqual(sent["headers"]["User-Agent"], "Solved UA")
        self.assertEqual(sent["headers"]["Cookie"], "cf_clearance=solved-cookie")
        self.assertEqual(sent["headers"]["Authorization"], "Bearer fixture")
        self.assertIsNone(sent["headers"]["Sec-Ch-Ua"])
        self.assertGreater(sent["timeout"], 0)
        self.assertLess(sent["timeout"], 10)

    def test_second_challenge_is_returned_without_unbounded_retry(self):
        store, provider = settings()
        raw = mock.Mock(headers={})
        raw.get.side_effect = [response(403, challenge=True), response(403, challenge=True)]
        result = store.wrap_session(raw, proxy_url="").get("https://chatgpt.com/", timeout=5)
        self.assertEqual(result.status_code, 403)
        self.assertEqual(raw.get.call_count, 2)
        provider.get_clearance.assert_called_once()

    def test_plain_auth_quota_and_disabled_statuses_do_not_refresh(self):
        for status, headers, content in (
            (401, {}, b'{"error":"invalid_token"}'), (403, {}, b'{"error":"quota exceeded"}'),
            (429, {}, b'{"error":"rate limited"}'), (503, {"cf-mitigated": "challenge"}, b""),
            (403, {"content-type": "application/json"}, b'{"error":"just a moment"}'),
        ):
            with self.subTest(status=status):
                store, provider = settings()
                blocked = response(status, content=content)
                blocked.headers = headers
                raw = mock.Mock(headers={})
                raw.get.return_value = blocked
                self.assertIs(store.wrap_session(raw, proxy_url="").get("https://chatgpt.com/"), blocked)
                provider.get_clearance.assert_not_called()
                raw.reset.assert_not_called()

    def test_disabled_clearance_does_not_change_session(self):
        store, provider = settings(enabled=False)
        raw = mock.Mock()
        self.assertIs(store.wrap_session(raw, proxy_url=""), raw)
        provider.get_clearance.assert_not_called()

    def test_custom_status_codes_trigger_only_identified_challenges(self):
        store, provider = settings(codes=[503])
        raw = mock.Mock(headers={})
        raw.get.side_effect = [response(503, challenge=True), response()]
        self.assertEqual(store.wrap_session(raw, proxy_url="").get("https://chatgpt.com/").status_code, 200)
        provider.get_clearance.assert_called_once()

    def test_non_replayable_upload_is_not_sent_twice(self):
        import io

        store, provider = settings()
        raw = mock.Mock(headers={})
        raw.post.return_value = blocked = response(403, challenge=True)
        self.assertIs(store.wrap_session(raw, proxy_url="").post("https://chatgpt.com/", data=io.BytesIO(b"fixture")), blocked)
        raw.post.assert_called_once()
        provider.get_clearance.assert_not_called()

    def test_failed_solver_keeps_original_error_without_retry(self):
        store, provider = settings()
        provider.get_clearance.return_value = None
        provider.get_clearance.side_effect = None
        raw = mock.Mock(headers={})
        raw.get.return_value = blocked = response(403, challenge=True)
        self.assertIs(store.wrap_session(raw, proxy_url="").get("https://chatgpt.com/"), blocked)
        raw.get.assert_called_once()
        raw.reset.assert_not_called()

    def test_expired_request_budget_does_not_start_solver(self):
        store, provider = settings()
        clock = {"now": 0.0}
        blocked = response(403, challenge=True)

        def get(*args, **kwargs):
            clock["now"] = 6
            return blocked

        raw = mock.Mock(headers={})
        raw.get.side_effect = get
        with mock.patch("services.clearance_session.time.monotonic", side_effect=lambda: clock["now"]):
            self.assertIs(store.wrap_session(raw, proxy_url="").get("https://chatgpt.com/", timeout=5), blocked)
        provider.get_clearance.assert_not_called()

    def test_streamed_html_challenge_is_closed_before_successful_sse_retry(self):
        store, provider = settings()
        raw = mock.Mock(headers={})
        blocked = response(403)
        blocked.headers = {"content-type": "text/html"}
        blocked.iter_content.return_value = iter([b"<html><title>Just a moment</title></html>"])
        raw.post.side_effect = [blocked, ok := response()]
        result = store.wrap_session(raw, proxy_url="").post("https://chatgpt.com/", json={}, stream=True)
        self.assertIs(result, ok)
        self.assertTrue(raw.post.call_args.kwargs["stream"])
        self.assertTrue(blocked.close.called)
        provider.get_clearance.assert_called_once()

    def test_manual_cookie_does_not_leak_to_unrelated_cdn(self):
        store, provider = settings(mode="manual")
        raw = mock.Mock(headers={"User-Agent": "Old UA"})
        raw.get.return_value = response()
        store.wrap_session(raw, proxy_url="", resource=True).get("https://cdn.example.test/file.png")
        self.assertNotIn("Cookie", raw.get.call_args.kwargs["headers"])
        provider.get_clearance.assert_not_called()

    def test_single_flight_refreshes_concurrent_challenges_once(self):
        store, provider = settings()
        workers = 6
        barrier = threading.Barrier(workers)

        def run(_):
            calls = 0

            def get(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    barrier.wait(timeout=5)
                    return response(403, challenge=True)
                return response()

            raw = mock.Mock(headers={})
            raw.get.side_effect = get
            return store.wrap_session(raw, proxy_url="http://same.invalid:8080", account={"email": "same@example.test"}).get("https://chatgpt.com/", timeout=10).status_code

        with ThreadPoolExecutor(max_workers=workers) as executor:
            self.assertEqual(list(executor.map(run, range(workers))), [200] * workers)
        provider.get_clearance.assert_called_once()


class ClearanceWarmupTests(unittest.TestCase):
    def test_opt_in_warms_account_keys_that_survive_added_device_id(self):
        store, provider = settings(warm=True)
        accounts = [{"access_token": "fixture-a", "email": "a@example.test", "proxy": "http://a.invalid:8080"},
                    {"access_token": "fixture-b", "status": "禁用"}]
        thread = store.start_clearance_warmup(threading.Event(), lambda: accounts)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(provider.get_clearance.call_count, 2)
        self.assertIsNotNone(store.clearance_bundle("https://chatgpt.com/", account=dict(accounts[0], **{"oai-device-id": "new-device"}), force_proxy=accounts[0]["proxy"]))

    def test_disabled_warmup_and_stopped_worker_make_no_requests(self):
        store, provider = settings(warm=False)
        self.assertIsNone(store.start_clearance_warmup(threading.Event()))
        store, provider = settings(warm=True)
        stop = threading.Event()
        stop.set()
        thread = store.start_clearance_warmup(stop)
        thread.join(timeout=5)
        provider.get_clearance.assert_not_called()

    def test_app_lifespan_starts_and_joins_clearance_worker(self):
        thread = mock.Mock()
        names = ("start_limited_account_watcher", "start_grok_account_watcher", "start_account_replenish_watcher",
                 "start_image_cleanup_scheduler", "start_log_retention_watcher")
        patchers = [mock.patch.object(app_module, name, return_value=mock.Mock()) for name in names]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        with (
            mock.patch.object(app_module.proxy_settings, "start_clearance_warmup", return_value=thread) as warm,
            mock.patch.object(app_module.backup_service, "start"), mock.patch.object(app_module.backup_service, "stop"),
            mock.patch.object(app_module.config, "cleanup_old_images"), mock.patch.object(app_module, "Thread"),
        ):
            with TestClient(app_module.create_app()):
                stop = warm.call_args.args[0]
                self.assertFalse(stop.is_set())
            self.assertTrue(stop.is_set())
        thread.join.assert_called_once_with(timeout=1)


class ClearanceIdentityTests(unittest.TestCase):
    def test_real_cookie_jar_survives_connection_reset(self):
        first = mock.Mock(headers={"Authorization": "Bearer fixture"}, cookies=Cookies())
        first.cookies.set("__Secure-next-auth.session-token", "fixture-session", domain=".chatgpt.com", secure=True)
        second = mock.Mock(headers={}, cookies=Cookies())
        with mock.patch("curl_cffi.requests.Session", side_effect=[first, second]):
            session = create_cffi_session(proxy="")
            session.reset()
            jar = list(session.cookies.jar)
            self.assertEqual(jar[0].value, "fixture-session")
            self.assertTrue(jar[0].secure)
            self.assertEqual(session.headers["Authorization"], "Bearer fixture")
            session.close()

    def test_cdn_uses_resource_proxy_and_its_own_clearance(self):
        store, provider = settings()
        primary, resource = mock.Mock(headers={}), mock.Mock(headers={})
        resource.get.side_effect = [response(403, challenge=True), response(content=b"image-fixture")]
        account = {"access_token": "fixture", "email": "a@example.test"}
        with (
            mock.patch.object(openai_backend_api, "proxy_settings", store),
            mock.patch.object(openai_backend_api.account_service, "get_account", return_value=account),
            mock.patch.object(openai_backend_api.account_service, "update_account", return_value=account),
            mock.patch.object(openai_backend_api, "resolve_egress_locale", return_value=default_locale()),
            mock.patch.object(openai_backend_api, "create_cffi_session", side_effect=[primary, resource]) as factory,
        ):
            backend = openai_backend_api.OpenAIBackendAPI("fixture", force_proxy="http://api.invalid:8080")
            try:
                images = backend.download_image_bytes(["https://files.oaiusercontent.com/fixture.png"])
            finally:
                backend.close()
        self.assertEqual(images, [b"image-fixture"])
        self.assertEqual(factory.call_args_list[1].kwargs["proxy"], "http://cdn.invalid:8080")
        self.assertEqual(provider.get_clearance.call_args.kwargs["proxy_url"], "http://cdn.invalid:8080")
        self.assertNotIn("Authorization", resource.get.call_args.kwargs["headers"])
        primary.get.assert_not_called()
