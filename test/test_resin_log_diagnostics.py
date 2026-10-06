"""Local regressions for Resin connectivity diagnostics and credential-safe logs."""
import copy
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import api.system as system
from services import proxy_service
from services.account_service import AccountService
from services.gpt_register_service import GptRegisterService
from services.log_service import LogService
from services.storage.json_storage import JSONStorageBackend
from test.test_proxy_service import FakeConfig, make_runtime
from utils.log import Logger
from utils.log_safety import redact_text, sanitize_log_value
from utils.network_diagnostics import error_details

PROXY = "socks5h://fixture.session:fake-secret@proxy.invalid:2260"


class ProxyDiagnosticTests(unittest.TestCase):
    def test_http_failure_codes_and_html_never_report_success(self):
        for status, text, headers, kind in [
            (401, "{}", {}, "http_auth"), (403, "{}", {}, "http_forbidden"),
            (407, "", {}, "proxy_auth"), (429, "{}", {"Retry-After": "42"}, "rate_limit"),
            (503, "oops", {}, "upstream"), (302, "", {}, "redirect"),
            (200, "<html>Just a moment</html>", {}, "challenge"),
            (200, "<html>sign in</html>", {}, "unexpected_html"),
            (200, "", {}, "unexpected_response"),
            (200, "{}", {}, "unexpected_response"),
            (200, "not json", {}, "unexpected_response"),
            (200, '{"csrfToken":12}', {}, "unexpected_response"),
            (200, '{"csrfToken":""}', {}, "unexpected_response"),
            (200, "<body>Login</body>", {"Content-Type": "text/html"}, "unexpected_html"),
        ]:
            with self.subTest(status=status, kind=kind):
                session = Mock()
                session.get.return_value = SimpleNamespace(status_code=status, text=text, headers=headers)
                with patch.object(proxy_service, "create_cffi_session", return_value=session):
                    result = proxy_service.test_proxy(PROXY)
                self.assertFalse(result["ok"])
                self.assertTrue(result["reachable"])
                self.assertEqual(result["failure_kind"], kind)
                self.assertEqual(result["status"], status)
                self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
                self.assertNotIn("fake-secret", json.dumps(result))
                session.close.assert_called_once()
                if status == 429:
                    self.assertEqual(result["retry_after_seconds"], 42)

    def test_exact_candidate_is_tested_even_with_runtime_override(self):
        runtime = make_runtime(enabled=True, egress_mode="single_proxy", proxy_url="http://runtime.invalid:8080", skip_ssl_verify=True)
        store = proxy_service.ProxySettingsStore(FakeConfig(runtime=runtime))
        session = Mock()
        session.get.return_value = SimpleNamespace(status_code=200, text='{"csrfToken":"fixture"}', headers={})
        with patch.object(proxy_service, "proxy_settings", store), patch.object(proxy_service, "create_cffi_session", return_value=session) as factory:
            result = proxy_service.test_proxy(PROXY, timeout=3)
        self.assertEqual(factory.call_args.kwargs["proxy"], PROXY)
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertFalse(factory.call_args.kwargs["verify"])
        self.assertEqual(session.get.call_args.kwargs["timeout"], 3)
        self.assertTrue(result["ok"])
        self.assertEqual(result["gateway"], "proxy.invalid:2260")
        self.assertEqual(result["proxy_source"], "input")

    def test_constructor_errors_are_returned_and_close_errors_do_not_mask_result(self):
        with patch.object(proxy_service, "create_cffi_session", side_effect=RuntimeError(f"curl: (97) User was rejected {PROXY}")):
            result = proxy_service.test_proxy(PROXY)
        self.assertEqual(result["failure_kind"], "proxy_auth")
        self.assertEqual(result["curl_code"], 97)
        self.assertNotIn("fake-secret", json.dumps(result))
        session = Mock()
        session.get.side_effect = RuntimeError("curl: (28) timed out")
        session.close.side_effect = RuntimeError("close failed")
        with patch.object(proxy_service, "create_cffi_session", return_value=session):
            result = proxy_service.test_proxy(PROXY)
        self.assertEqual(result["failure_kind"], "timeout")

    def test_invalid_ports_are_configuration_errors_without_network(self):
        with patch.object(proxy_service, "create_cffi_session") as factory:
            for proxy in ("http://host:0", "http://host:70000", "http://host:bad", "http://:80"):
                result = proxy_service.test_proxy(proxy)
                self.assertFalse(result["ok"])
                self.assertEqual(result["failure_kind"], "configuration")
            factory.assert_not_called()

    def test_failure_classification_covers_transport_and_http_formats(self):
        for text, expected in [
            ("curl: (6) Could not resolve host", "dns"),
            ("curl: (35) TLS connect error", "tls"),
            ("curl: (7) Failed to connect", "transport"),
            ("curl: (97) SOCKS5 connection failed", "proxy_connect"),
            ("curl: (97) User was rejected by the SOCKS5 server", "proxy_auth"),
            ("session_refresh_http_403", "http_forbidden"),
            ("HTTP/2 429", "rate_limit"), ("status_code=503", "upstream"),
            ("HTTP 401 token_revoked", "token_revoked"),
            ("HTTP 401", "http_auth"), ("unknown", "unknown"),
        ]:
            self.assertEqual(error_details(text)["failure_kind"], expected, text)


class RedactionTests(unittest.TestCase):
    def test_nested_credentials_and_text_are_masked_idempotently(self):
        original = {
            "proxy_url": PROXY,
            "nested": [{"password": "fixture-password", "access_token": "fixture-token"}],
            "headers": {"Authorization": "Bearer bearer-secret", "Cookie": "sid=cookie-secret"},
            "request": 'Authorization: Bearer bearer-secret password="fixture-password"',
            "urls": ["https://images.invalid/generated/photo.png"],
            "status": 429, "curl_code": 97,
        }
        snapshot = copy.deepcopy(original)
        clean = sanitize_log_value(original)
        self.assertEqual(original, snapshot)
        self.assertEqual(clean, sanitize_log_value(clean))
        self.assertEqual(clean["urls"], original["urls"])
        for secret in ("fake-secret", "fixture.session", "fixture-password", "fixture-token", "bearer-secret", "cookie-secret"):
            self.assertNotIn(secret, json.dumps(clean))

    def test_multiline_credential_urls_and_otp(self):
        raw = f"{PROXY}\npassword=fixture-password\nOTP: 123456\nAuthorization: Bearer bearer-secret"
        clean = redact_text(raw)
        self.assertEqual(clean, redact_text(clean))
        for secret in ("fake-secret", "fixture-password", "123456", "bearer-secret"):
            self.assertNotIn(secret, clean)
        self.assertLessEqual(len(redact_text("x" * 100, limit=20)), 20)
        self.assertNotIn("fixture-password", redact_text("生成密码[1/3]: fixture-password"))
        quoted = redact_text('password="first part; second part" access_token=\'another secret\'')
        self.assertNotIn("part", quoted)
        self.assertNotIn("secret", quoted)
        self.assertEqual(quoted, redact_text(quoted))
        raw_json = '{"Cookie": "first=cookie-one; second=cookie-two", "token":"opaque-secret"}'
        raw_cookie = "__Secure-next-auth.session-token.0=chunk-secret; Path=/"
        for raw in (raw_json, raw_cookie):
            for secret in ("cookie-one", "cookie-two", "opaque-secret", "chunk-secret"):
                self.assertNotIn(secret, redact_text(raw))

    def test_oauth_callback_urls_and_pkce_secrets_are_masked(self):
        callback = "GET http://localhost:1455/auth/callback?code=FIXTURE-CODE&state=FIXTURE-STATE"
        clean = redact_text(callback)
        self.assertNotIn("FIXTURE-CODE", clean)
        self.assertNotIn("FIXTURE-STATE", clean)
        for raw in ('{"code_verifier": "opaque-verifier", "totp_secret": "JBSWY3DP"}',
                    "code_verifier=opaque-verifier totp_secret=JBSWY3DP mfa_secret=AAAA"):
            clean = redact_text(raw)
            self.assertEqual(clean, redact_text(clean))
            for secret in ("opaque-verifier", "JBSWY3DP", "AAAA"):
                self.assertNotIn(secret, clean)
        self.assertNotIn("real-secret", redact_text("opaque token=real-secret"))

    def test_console_output_uses_central_redaction(self):
        logger = Logger("diagnostic-regression")
        with patch.object(logger, "_enabled", return_value=True), patch.object(logger._logger, "warning") as output:
            logger.warning({"error": f"curl: (97) User was rejected {PROXY}", "password": "fixture-password"})
        self.assertNotIn("fake-secret", output.call_args.args[0])
        self.assertNotIn("fixture-password", output.call_args.args[0])

    def test_registration_progress_is_sanitized_before_stdout_and_history(self):
        service = GptRegisterService.__new__(GptRegisterService)
        service._lock = threading.RLock()
        service._jobs = {"job": {"logs": [], "status": "running"}}
        service._save_jobs = Mock()
        with redirect_stdout(io.StringIO()) as stdout:
            service._append_log("job", f"curl: (97) User was rejected {PROXY} password=fixture-password", level="error")
        row = service._jobs["job"]["logs"][0]
        self.assertEqual(row["failure_kind"], "proxy_auth")
        for text in (stdout.getvalue(), json.dumps(row)):
            self.assertNotIn("fake-secret", text)
            self.assertNotIn("fixture-password", text)
        service._jobs["job"]["logs"].append({"message": PROXY})
        self.assertNotIn("fake-secret", json.dumps(service.get_job("job")))
        self.assertNotIn("fake-secret", json.dumps(service.list_jobs()))
    def test_anonymous_references_survive_every_log_boundary(self):
        alias = "token:0123456789"
        logger = Logger("alias-regression")
        self.assertEqual(redact_text(alias), alias)
        data = {"token": alias, "account_ref": alias, "account_refs": [alias]}
        self.assertEqual(sanitize_log_value(data), data)
        self.assertEqual(json.loads(logger._message(data)), data)
        self.assertEqual(redact_text("opaque token=real-secret"), "opaque token=[REDACTED]")

    def test_registration_disk_and_public_history_leave_runtime_settings_intact(self):
        from services import gpt_register_service as register

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs_path = root / "jobs.json"
            settings = {"proxy": PROXY, "count": 1, "password": "runtime-password"}
            service = GptRegisterService.__new__(GptRegisterService)
            service._lock = threading.RLock()
            service._cancel_flags = {}
            service._jobs = {}
            service.config_store = Mock()
            service.config_store.get.return_value = settings
            with patch.object(register, "GPT_REGISTER_JOBS_FILE", jobs_path), patch.object(register, "DATA_DIR", root), patch.object(register.threading, "Thread") as worker:
                public = service.start_job()
                job_id = public["job_id"]
                runtime = worker.call_args.kwargs["args"][1]
                self.assertEqual(runtime["proxy"], PROXY)
                self.assertEqual(settings["password"], "runtime-password")
                self.assertNotIn("fake-secret", json.dumps(public))
                service._jobs[job_id]["items"] = [{"error": f"HTTP 403 {PROXY}", "logs_tail": ["password=old-secret"]}]
                service._save_jobs()
                cancel = service.cancel_job(job_id)
                for value in (jobs_path.read_text(), json.dumps(cancel), json.dumps(service.list_jobs()), json.dumps(service.get_job(job_id))):
                    self.assertNotIn("fake-secret", value)
                    self.assertNotIn("old-secret", value)
                self.assertIn("fake-secret", service._jobs[job_id]["settings"]["proxy"])
                service._append_log = Mock()
                with patch("services.log_service.log_service.add"):
                    service._emit_completion_log(job_id, status="failed", success=0, failed=1, added=0, completed=1, total=1, duration=0.1, items=service._jobs[job_id]["items"], error=f"HTTP 403 {PROXY}", settings=runtime)
                completed_path = root / "gpt_register_logs" / f"{job_id}.json"
                for path in (jobs_path, completed_path):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertNotIn("fake-secret", path.read_text())
                    self.assertNotIn("old-secret", path.read_text())


class AccountLogNoiseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = Mock()
        patched = patch("services.account_service.log_service", self.log)
        patched.start()
        self.addCleanup(patched.stop)
        self.service = AccountService(JSONStorageBackend(Path(tmp.name) / "accounts.json"))
        self.service.add_account_items([{"access_token": "fixture-token", "quota": 10}])
        self.log.reset_mock()

    def test_identical_periodic_updates_do_not_emit_events(self):
        self.service.update_account("fixture-token", {"quota": 10}, source="remote_probe")
        self.service.update_account("fixture-token", {"last_probed_at": "2026-10-06T03:00:00Z", "restore_at": "next"}, source="remote_probe")
        self.log.add.assert_not_called()

    def test_quota_and_manual_status_updates_still_log(self):
        self.service.update_account("fixture-token", {"quota": 9}, source="remote_probe")
        detail = self.log.add.call_args.args[2]
        self.assertEqual(detail["previous_quota"], 10)
        self.assertEqual(detail["quota"], 9)
        self.assertNotIn("fixture-token", json.dumps(detail))
        self.log.reset_mock()
        self.service.update_account("fixture-token", {}, allow_status_override=True)
        self.log.add.assert_called_once()

    def test_gate_changes_are_logged_but_observation_timestamps_are_quiet(self):
        gate = {"name": "image_gen", "limit": 5, "observed_at": 1}
        self.service.update_account("fixture-token", {"image_gate": gate}, source="remote_probe")
        self.assertIn("image_gate", self.log.add.call_args.args[2]["changed_fields"])
        self.log.reset_mock()
        self.service.update_account("fixture-token", {"image_gate": {**gate, "observed_at": 2}}, source="remote_probe")
        self.log.add.assert_not_called()
        self.service.update_account("fixture-token", {"image_gate": None}, source="remote_probe")
        self.assertIn("image_gate", self.log.add.call_args.args[2]["changed_fields"])

    def test_deletion_records_anonymous_affected_reference(self):
        self.service.delete_accounts(["fixture-token"])
        detail = self.log.add.call_args.args[2]
        self.assertEqual(detail["removed"], 1)
        self.assertEqual(len(detail["account_refs"]), 1)
        self.assertNotIn("fixture-token", json.dumps(detail))


class LogPaginationApiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = LogService(Path(tmp.name) / "logs.jsonl")
        for index in range(205):
            self.log.add("call", f"item-{index}")
        for target, replacement in [("log_service", self.log), ("require_admin", lambda _: {"role": "admin"})]:
            patched = patch.object(system, target, replacement)
            patched.start()
            self.addCleanup(patched.stop)
        app = FastAPI()
        app.include_router(system.create_router("test"))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_default_response_stays_compatible_and_cursor_reads_remainder(self):
        response = self.client.get("/api/logs")
        self.assertEqual(response.status_code, 200)
        first = response.json()
        self.assertEqual(len(first["items"]), 200)
        self.assertTrue(first["has_more"])
        second = self.client.get("/api/logs", params={"cursor": first["next_cursor"]}).json()
        self.assertEqual(len(second["items"]), 5)
        self.assertFalse(second["has_more"])
        self.assertEqual(len({row["id"] for row in first["items"] + second["items"]}), 205)

    def test_expired_cursor_and_bad_parameters_have_distinct_statuses(self):
        page = self.client.get("/api/logs", params={"limit": 2}).json()
        self.log.delete([page["items"][0]["id"]])
        stale = self.client.get("/api/logs", params={"cursor": page["next_cursor"]})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["code"], "log_cursor_expired")
        for params, status in [({"cursor": "bad"}, 400), ({"limit": 0}, 422), ({"limit": 1001}, 422), ({"start_date": "bad"}, 400)]:
            self.assertEqual(self.client.get("/api/logs", params=params).status_code, status)

    def test_admin_guard_is_retained(self):
        with patch.object(system, "require_admin", side_effect=HTTPException(status_code=403, detail="admin required")):
            self.assertEqual(self.client.get("/api/logs").status_code, 403)


if __name__ == "__main__":
    unittest.main()
