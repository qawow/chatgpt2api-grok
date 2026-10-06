"""Regression coverage for proxy observations and evidence-based recovery gates."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from services.account_service import AccountService
from services import egress_reputation as er
from utils.network_diagnostics import proxy_identity

PROXY = "socks5h://fixture.session:fake-password@proxy.invalid:2260"


class PasswordReloginGateTest(unittest.TestCase):
    def test_oauth_account_retains_existing_behavior(self):
        self.assertTrue(AccountService._allows_password_relogin({"refresh_token": "rt"}, "refresh_accounts", []))

    def test_manual_relogin_remains_explicit(self):
        self.assertTrue(AccountService._allows_password_relogin({"session_token": "st"}, "manual_relogin", ["timed out"]))

    def test_unknown_and_transport_errors_are_not_revocation(self):
        account = {"session_token": "st", "password": "pw", "session_only": True}
        for error in (
            "read timed out", "curl: (35) OPENSSL_INTERNAL", "ProxyError SOCKS5",
            "connection reset", "session_refresh_http_403", "HTTP 429", "HTTP 503",
            "HTTP 200 <html>Just a moment</html>", "parse failed", "HTTP 401",
            "session_refresh_no_accessToken", "HTTP 407", "curl: (97) User was rejected",
        ):
            with self.subTest(error=error):
                self.assertFalse(AccountService._allows_password_relogin(account, "refresh_accounts", [error]))
        self.assertFalse(AccountService._allows_password_relogin(account, "refresh_accounts", []))

    def test_login_403_is_not_token_revocation_evidence(self):
        for error in ("authorize_failed_403", "password_verify_failed_403"):
            self.assertFalse(AccountService._token_looks_revoked({"last_token_refresh_error": error}))

    def test_confirmed_revocation_is_distinct_from_transport(self):
        account = {"session_token": "st", "password": "pw"}
        self.assertTrue(AccountService._allows_password_relogin(account, "refresh_accounts", ["session_refresh_stale_token_revoked"]))
        self.assertFalse(AccountService._allows_password_relogin(account, "refresh_accounts", ["HTTP 403 Just a moment token_revoked"]))


class EgressReputationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "observations.json"
        patched = patch.object(er, "DATA_PATH", self.path)
        patched.start()
        self.addCleanup(patched.stop)

    def test_http_failures_are_not_successful_observations(self):
        cases = [
            (403, "<html>Just a moment</html>", er.STATE_CF_BLOCKED),
            (200, "<html>Just a moment</html>", er.STATE_CF_BLOCKED),
            (401, '{"error":"unauthenticated"}', "http_auth"),
            (403, '{"error":"denied"}', "http_forbidden"),
            (429, "{}", "rate_limit"), (407, "", "proxy_auth"),
            (302, "", "redirect"), (500, "", "upstream"),
            (200, "<html>login</html>", "unexpected_html"),
        ]
        for status, body, state in cases:
            with self.subTest(status=status, state=state):
                self.assertEqual(er.classify_response(status, body), state)

    def test_identity_covers_gateway_protocol_and_whole_credentials(self):
        variants = [PROXY, PROXY.replace("proxy.invalid", "other.invalid"),
                    PROXY.replace("socks5h", "http"), PROXY.replace("session", "other"),
                    PROXY.replace("fake-password", "updated-password"), PROXY.replace("2260", "2261")]
        self.assertEqual(len({er._key(url) for url in variants}), len(variants))
        self.assertEqual(er._key(PROXY), er._key(PROXY.replace("socks5h", "socks5")))
        self.assertEqual(er._key(PROXY), er._key(PROXY.replace("fixture", "%66ixture")))
        self.assertNotIn("fake-password", json.dumps(proxy_identity(PROXY)))
        self.assertNotIn("fixture.session", json.dumps(proxy_identity(PROXY)))

    def test_socks5_input_is_probed_with_remote_dns_canonical_url(self):
        session = Mock()
        session.get.return_value = SimpleNamespace(status_code=200, text="ip=192.0.2.8\ncolo=HKG\n", headers={})
        raw = PROXY.replace("socks5h", "socks5")
        with patch.object(er, "create_cffi_session", return_value=session) as factory:
            result = er.probe_exit(raw)
        self.assertEqual(factory.call_args.kwargs["proxy"], PROXY)
        self.assertEqual(result["proxy_id"], er._key(raw))
        self.assertEqual(result["state"], er.STATE_CLEAR)

    def test_trace_uses_single_session_and_never_authenticates_accounts(self):
        session = Mock()
        session.get.return_value = SimpleNamespace(status_code=200, text="ip=192.0.2.8\ncolo=HKG\n", headers={})
        with patch.object(er, "create_cffi_session", return_value=session) as factory:
            result = er.probe_exit(PROXY, timeout=4)
        factory.assert_called_once_with(proxy=PROXY, trust_env=False, verify=True)
        session.get.assert_called_once_with("https://chatgpt.com/cdn-cgi/trace", timeout=4, allow_redirects=False)
        session.close.assert_called_once()
        self.assertEqual(result["state"], er.STATE_CLEAR)
        self.assertEqual(result["ip"], "192.0.2.8")
        self.assertEqual(result["probe_kind"], "trace_only")
        self.assertNotIn("fake-password", json.dumps(result))
        self.assertNotIn("proxy", result)

    def test_invalid_trace_and_challenge_header_are_reported(self):
        for text, headers, state in [("ip=bogus", {}, er.STATE_UNKNOWN),
                                     ("ip=192.0.2.1", {"cf-mitigated": "challenge"}, er.STATE_CF_BLOCKED)]:
            session = Mock()
            session.get.return_value = SimpleNamespace(status_code=200, text=text, headers=headers)
            with patch.object(er, "create_cffi_session", return_value=session):
                result = er.probe_exit(PROXY)
            self.assertFalse(result["ok"])
            self.assertEqual(result["state"], state)

    def test_constructor_failure_is_redacted_and_no_input_means_no_io(self):
        with patch.object(er, "create_cffi_session", side_effect=RuntimeError(f"curl: (97) User was rejected {PROXY}")):
            result = er.probe_exit(PROXY)
        self.assertEqual(result["failure_kind"], "proxy_auth")
        self.assertNotIn("fake-password", json.dumps(result))
        with patch.object(er, "create_cffi_session") as factory:
            self.assertEqual(er.probe_exit("")["failure_kind"], "configuration")
            factory.assert_not_called()

    def test_bad_latest_observation_invalidates_old_success(self):
        er.record(PROXY, er.STATE_CLEAR)
        self.assertTrue(er.is_trusted(PROXY))
        er.record(PROXY, er.STATE_TRANSPORT)
        self.assertFalse(er.is_trusted(PROXY))
        er.record(PROXY, er.STATE_CLEAR)
        self.assertTrue(er.is_trusted(PROXY))

    def test_cache_expiry_applies_to_success_and_failure(self):
        with patch.object(er.time, "time", return_value=1000):
            er.record(PROXY, er.STATE_CLEAR)
        with patch.object(er.time, "time", return_value=1000 + er.OBSERVATION_TTL_SECS):
            self.assertFalse(er.is_trusted(PROXY))
            er.record(PROXY, er.STATE_CF_BLOCKED)
        with patch.object(er.time, "time", return_value=1001 + 2 * er.OBSERVATION_TTL_SECS):
            with patch.object(er, "probe_exit", return_value={"state": er.STATE_CLEAR}) as probe:
                self.assertEqual(er.pick_proxies([PROXY], probe_missing=True), [PROXY])
            probe.assert_called_once()

    def test_candidate_picker_has_no_implicit_network_and_preserves_order(self):
        other = PROXY.replace("session", "second")
        er.record(other, er.STATE_CLEAR)
        er.record(PROXY, er.STATE_CLEAR)
        with patch.object(er, "probe_exit") as probe:
            self.assertEqual(er.pick_proxies([other, PROXY, other]), [other, PROXY])
            self.assertEqual(er.pick_proxies([PROXY], count=0), [])
            probe.assert_not_called()

    def test_bounded_history_and_owner_only_storage(self):
        for index in range(er.MAX_SAMPLES + 3):
            er.record(PROXY, er.STATE_CLEAR)
        table = json.loads(self.path.read_text())
        self.assertEqual(len(table[er._key(PROXY)]["samples"]), er.MAX_SAMPLES)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("fake-password", self.path.read_text())
        self.assertNotIn("fixture.session", self.path.read_text())

    def test_entry_cap_and_concurrent_writes(self):
        with patch.object(er, "MAX_ENTRIES", 5):
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda index: er.record(PROXY.replace("session", str(index)), er.STATE_CLEAR), range(12)))
        self.assertEqual(len(json.loads(self.path.read_text())), 5)

    def test_corrupted_cache_surfaces_error_without_overwriting(self):
        original = '{"incomplete":'
        self.path.write_text(original)
        with self.assertRaises(er.ObservationStoreError):
            er.record(PROXY, er.STATE_CLEAR)
        self.assertEqual(self.path.read_text(), original)

    def test_malformed_sample_tail_does_not_break_cache_pruning(self):
        other = PROXY.replace("session", "other")
        self.path.write_text(json.dumps({er._key(other): {
            "schema": 2, "last_seen": "invalid",
            "samples": [{"at": 950, "state": "clear"}, {"at": "broken", "state": "clear"}],
        }}))
        with patch.object(er.time, "time", return_value=1000):
            er.record(PROXY, er.STATE_CLEAR)
            self.assertTrue(er.is_trusted(other))
        self.assertEqual(len(json.loads(self.path.read_text())[er._key(other)]["samples"]), 1)

    def test_html_header_never_records_a_clear_trace(self):
        session = Mock()
        session.get.return_value = SimpleNamespace(status_code=200, text="ip=192.0.2.1", headers={"Content-Type": "text/html"})
        with patch.object(er, "create_cffi_session", return_value=session):
            result = er.probe_exit(PROXY)
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "unexpected_html")
        self.assertFalse(er.probe_exit("")["ok"])

    def test_old_username_only_cache_is_not_trusted(self):
        self.path.write_text(json.dumps({"user:fixture.session": {"clear": 99, "last_state": "clear"}}))
        self.assertFalse(er.is_trusted(PROXY))


if __name__ == "__main__":
    unittest.main()
