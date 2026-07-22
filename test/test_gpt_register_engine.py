"""Unit tests for GPT free registrar engine hardening."""
from __future__ import annotations

import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from gpt_free_register.engines.core.base_mailbox import CloudflareD1Mailbox
from platforms.chatgpt.protocol_mailbox import _MailboxEmailService
from platforms.chatgpt.register import RegistrationEngine, _SentinelTokenGenerator


class PasswordGenTest(unittest.TestCase):
    def test_password_has_classes(self):
        eng = RegistrationEngine(
            email_service=SimpleNamespace(service_type=SimpleNamespace(value="x"))
        )
        pwd = eng._generate_password(16)
        self.assertGreaterEqual(len(pwd), 12)
        self.assertTrue(any(c.islower() for c in pwd))
        self.assertTrue(any(c.isupper() for c in pwd))
        self.assertTrue(any(c.isdigit() for c in pwd))
        self.assertTrue(any(c in ",._!@#" for c in pwd))


class SentinelGeneratorTest(unittest.TestCase):
    def test_requirements_token_prefix(self):
        gen = _SentinelTokenGenerator("did-1", "Mozilla/5.0 Chrome/142.0.0.0")
        token = gen.generate_requirements_token()
        self.assertTrue(token.startswith("gAAAAAC"))

    def test_decrypt_turnstile_delegates(self):
        gen = _SentinelTokenGenerator("did-1", "ua")
        with mock.patch(
            "platforms.chatgpt.sentinel_vm.solve_turnstile_dx",
            return_value="t-value",
        ) as solve:
            out = gen.decrypt_turnstile("ZHgtZGF0YQ==", "p-token")
        self.assertEqual(out, "t-value")
        solve.assert_called_once()


class Cfd1MailTimeFilterTest(unittest.TestCase):
    def test_mail_received_epoch_parses_date(self):
        raw = (
            "From: noreply@openai.com\r\n"
            "Date: Wed, 01 Jan 2020 12:00:00 +0000\r\n"
            "Subject: code\r\n"
            "\r\n"
            "Your code is 123456\r\n"
        )
        ts = CloudflareD1Mailbox._mail_received_epoch(raw)
        self.assertIsNotNone(ts)
        self.assertGreater(ts, 0)

    def test_wait_for_code_skips_old_and_baseline(self):
        mb = object.__new__(CloudflareD1Mailbox)
        old_raw = (
            "From: a\r\nDate: Wed, 01 Jan 2020 00:00:00 +0000\r\n\r\n"
            "old code 111111"
        )
        new_raw = (
            "From: a\r\nDate: Wed, 01 Jan 2030 00:00:00 +0000\r\n\r\n"
            "Your OpenAI code is 654321"
        )

        def list_mails(email, limit=30):
            return [
                {"id": "old1", "raw": old_raw},
                {"id": "new1", "raw": new_raw},
            ]

        mb._list_mails = list_mails  # type: ignore
        code = CloudflareD1Mailbox.wait_for_code(
            mb,
            SimpleNamespace(email="u@example.com"),
            timeout=2,
            before_ids=set(),
            otp_sent_at=time.mktime(time.strptime("2025-01-01", "%Y-%m-%d")),
            poll_interval=0.01,
        )
        self.assertEqual(code, "654321")

    def test_wait_for_code_respects_before_ids(self):
        mb = object.__new__(CloudflareD1Mailbox)
        raw = (
            "From: a\r\nDate: Wed, 01 Jan 2030 00:00:00 +0000\r\n\r\n"
            "code 222222"
        )
        mb._list_mails = lambda email, limit=30: [{"id": "seen1", "raw": raw}]  # type: ignore
        with self.assertRaises(TimeoutError):
            CloudflareD1Mailbox.wait_for_code(
                mb,
                SimpleNamespace(email="u@example.com"),
                timeout=0.2,
                before_ids={"seen1"},
                poll_interval=0.05,
            )


class ProtocolMailboxOtpArgsTest(unittest.TestCase):
    def test_forwards_otp_sent_at_and_before_ids(self):
        class FakeMailbox:
            def __init__(self):
                self.calls = []
                self.id_calls = 0

            def get_current_ids(self, account):
                self.id_calls += 1
                return {"pre1"}

            def wait_for_code(self, account, **kwargs):
                self.calls.append(kwargs)
                return "999999"

        mailbox = FakeMailbox()
        acct = SimpleNamespace(email="a@b.c", account_id="a@b.c")
        svc = _MailboxEmailService(mailbox=mailbox, mailbox_account=acct, provider="cfd1")
        svc.create_email()
        code = svc.get_verification_code(timeout=30, pattern=r"(\d{6})", otp_sent_at=123.0)
        self.assertEqual(code, "999999")
        self.assertEqual(len(mailbox.calls), 1)
        kwargs = mailbox.calls[0]
        self.assertEqual(kwargs.get("otp_sent_at"), 123.0)
        self.assertIn("pre1", kwargs.get("before_ids") or set())
        # baseline must be snapshotted at create_email only (not re-polled later)
        self.assertEqual(mailbox.id_calls, 1)

    def test_empty_baseline_not_refreshed_after_create(self):
        class FakeMailbox:
            def __init__(self):
                self.calls = []
                self.phase = 0

            def get_current_ids(self, account):
                # first call (create): empty; later calls would include OTP
                self.phase += 1
                return set() if self.phase == 1 else {"otp1"}

            def wait_for_code(self, account, **kwargs):
                self.calls.append(kwargs)
                return "123456"

        mailbox = FakeMailbox()
        acct = SimpleNamespace(email="n@e.w", account_id="n@e.w")
        svc = _MailboxEmailService(mailbox=mailbox, mailbox_account=acct, provider="cfd1")
        svc.create_email()
        svc.get_verification_code(timeout=10, otp_sent_at=1.0)
        before = mailbox.calls[0].get("before_ids") or set()
        self.assertEqual(before, set())


class HttpRetryConfigTest(unittest.TestCase):
    def test_default_impersonate_chrome142(self):
        from core.http_client import RequestConfig

        self.assertEqual(RequestConfig().impersonate, "chrome142")

    def test_env_impersonate_override(self):
        from core.http_client import HTTPClient

        old = os.environ.get("HTTP_IMPERSONATE")
        os.environ["HTTP_IMPERSONATE"] = "chrome131"
        try:
            client = HTTPClient()
            self.assertEqual(client.config.impersonate, "chrome131")
        finally:
            if old is None:
                os.environ.pop("HTTP_IMPERSONATE", None)
            else:
                os.environ["HTTP_IMPERSONATE"] = old


class BrowserProfileConsistencyTest(unittest.TestCase):
    def test_openai_client_headers_match_profile(self):
        from platforms.chatgpt.http_client import OpenAIHTTPClient
        from platforms.chatgpt.browser_profile import browser_profile

        client = OpenAIHTTPClient(proxy_url=None)
        profile = browser_profile(impersonate=client.config.impersonate)
        self.assertEqual(client.user_agent, profile["user_agent"])
        self.assertIn("Macintosh", client.user_agent)
        self.assertEqual(client.default_headers.get("sec-ch-ua-platform"), '"macOS"')
        # session must carry the same UA after creation
        self.assertEqual(client.session.headers.get("User-Agent"), client.user_agent)
        self.assertEqual(client.session.headers.get("sec-ch-ua-platform"), '"macOS"')

    def test_sentinel_vm_platform_follows_ua(self):
        from platforms.chatgpt.sentinel_vm import _FakeWindow

        mac = _FakeWindow(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/142.0.0.0")
        self.assertEqual(mac.navigator.platform, "MacIntel")
        win = _FakeWindow(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/142.0.0.0")
        self.assertEqual(win.navigator.platform, "Win32")

    def test_random_profiles_are_independent(self):
        from platforms.chatgpt.browser_profile import random_browser_profile
        from platforms.chatgpt.http_client import OpenAIHTTPClient

        a = random_browser_profile(seed="seed-a")
        b = random_browser_profile(seed="seed-b")
        self.assertNotEqual(a["profile_id"], b["profile_id"])
        # same seed => stable
        a2 = random_browser_profile(seed="seed-a")
        self.assertEqual(a["user_agent"], a2["user_agent"])
        self.assertEqual(a["screen_width"], a2["screen_width"])
        # client honors provided profile
        c1 = OpenAIHTTPClient(proxy_url=None, profile=a)
        c2 = OpenAIHTTPClient(proxy_url=None, profile=b)
        self.assertEqual(c1.user_agent, a["user_agent"])
        self.assertEqual(c2.user_agent, b["user_agent"])
        self.assertEqual(c1.session.headers.get("User-Agent"), a["user_agent"])
        # internal coherence: platform string appears consistently
        for c in (c1, c2):
            plat = c.browser["platform"]
            if plat == "mac":
                self.assertIn("Macintosh", c.user_agent)
                self.assertEqual(c.browser["navigator_platform"], "MacIntel")
                self.assertEqual(c.default_headers.get("sec-ch-ua-platform"), '"macOS"')
            else:
                self.assertIn("Windows", c.user_agent)
                self.assertEqual(c.browser["navigator_platform"], "Win32")
                self.assertEqual(c.default_headers.get("sec-ch-ua-platform"), '"Windows"')

    def test_oauth_post_form_uses_unified_impersonate(self):
        from platforms.chatgpt import oauth as oauth_mod
        from unittest import mock

        captured = {}

        def fake_post(url, data=None, headers=None, timeout=30, proxies=None, impersonate=None):
            captured["impersonate"] = impersonate
            captured["headers"] = dict(headers or {})
            class R:
                status_code = 200
                text = "{}"
                def json(self):
                    return {"access_token": "x"}
            return R()

        with mock.patch.object(oauth_mod.cffi_requests, "post", side_effect=fake_post):
            out = oauth_mod._post_form("https://example.com/token", {"a": "b"})
        self.assertEqual(out.get("access_token"), "x")
        self.assertEqual(captured.get("impersonate"), "chrome142")
        self.assertIn("Macintosh", captured["headers"].get("User-Agent", ""))

    def test_client_hints_match_chrome_major(self):
        from platforms.chatgpt.browser_profile import (
            browser_profile,
            random_browser_profile,
            sec_ch_ua,
            default_request_headers,
        )
        from platforms.chatgpt.http_client import OpenAIHTTPClient

        p = browser_profile(impersonate="chrome142")
        self.assertIn("142", p["sec_ch_ua"])
        self.assertEqual(p["sec_ch_ua"], sec_ch_ua("142"))
        self.assertIn("142.0.0.0", p["sec_ch_ua_full_version_list"])
        self.assertEqual(p["sec_ch_ua_arch"], '"x86"')  # mac default
        self.assertEqual(p["sec_ch_ua_bitness"], '"64"')
        self.assertTrue(p["sec_ch_ua_platform_version"].startswith('"'))

        r = random_browser_profile(seed="hints-seed", platform="mac", impersonate="chrome136")
        self.assertIn("136", r["sec_ch_ua"])
        self.assertIn(r["chrome_full"], r["sec_ch_ua_full_version_list"])
        headers = default_request_headers(profile=r)
        for key in (
            "sec-ch-ua",
            "sec-ch-ua-mobile",
            "sec-ch-ua-platform",
            "sec-ch-ua-full-version-list",
            "sec-ch-ua-arch",
            "sec-ch-ua-bitness",
            "sec-ch-ua-model",
            "sec-ch-ua-platform-version",
        ):
            self.assertIn(key, headers)
            self.assertTrue(str(headers[key]).strip())

        client = OpenAIHTTPClient(proxy_url=None, profile=r)
        self.assertEqual(client.default_headers.get("sec-ch-ua"), r["sec_ch_ua"])
        self.assertEqual(
            client.session.headers.get("sec-ch-ua-full-version-list"),
            r["sec_ch_ua_full_version_list"],
        )

    def test_windows_platform_env_switch(self):
        import os
        from unittest import mock
        from platforms.chatgpt.browser_profile import random_browser_profile, browser_profile
        from platforms.chatgpt.http_client import OpenAIHTTPClient

        with mock.patch.dict(os.environ, {"OPENAI_BROWSER_PLATFORM": "windows"}):
            p = random_browser_profile(seed="win-seed")
            self.assertEqual(p["platform"], "windows")
            self.assertIn("Windows", p["user_agent"])
            self.assertEqual(p["navigator_platform"], "Win32")
            self.assertEqual(p["sec_ch_ua_platform"], '"Windows"')
            self.assertEqual(p["sec_ch_ua_arch"], '"x86_64"')
            c = OpenAIHTTPClient(proxy_url=None, profile=p)
            self.assertEqual(c.default_headers.get("sec-ch-ua-platform"), '"Windows"')
            self.assertIn("Windows", c.user_agent)

        # explicit platform arg wins over default
        w = browser_profile(platform="windows", impersonate="chrome142")
        self.assertEqual(w["platform"], "windows")
        self.assertEqual(w["sec_ch_ua_platform"], '"Windows"')

    def test_resolve_screen_hint_default_login_or_signup(self):
        import os
        from unittest import mock
        from platforms.chatgpt.register import RegistrationEngine

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_SCREEN_HINT", None)
            self.assertEqual(RegistrationEngine._resolve_screen_hint(), "login_or_signup")
        with mock.patch.dict(os.environ, {"OPENAI_SCREEN_HINT": "signup"}):
            self.assertEqual(RegistrationEngine._resolve_screen_hint(), "signup")
        with mock.patch.dict(os.environ, {"OPENAI_SCREEN_HINT": "bogus"}):
            self.assertEqual(RegistrationEngine._resolve_screen_hint(), "login_or_signup")

    def test_sentinel_payload_so_field(self):
        from platforms.chatgpt.register import SentinelPayload
        p = SentinelPayload(p="p", c="c", flow="oauth_create_account", t="t", so="so-val")
        self.assertEqual(p.so, "so-val")
        p2 = SentinelPayload(p="p", c="c", flow="authorize_continue")
        self.assertEqual(p2.so, "")

    def test_random_delay_respects_disable_env(self):
        import os
        from unittest import mock
        from platforms.chatgpt import register as regmod

        with mock.patch.dict(os.environ, {"OPENAI_REGISTER_NO_DELAY": "1"}):
            with mock.patch.object(regmod.time, "sleep") as sleep:
                regmod._random_delay(0.5, 1.0)
                sleep.assert_not_called()
        with mock.patch.dict(os.environ, {"OPENAI_REGISTER_NO_DELAY": "0"}, clear=False):
            with mock.patch.object(regmod.time, "sleep") as sleep:
                regmod._random_delay(0.1, 0.1)
                sleep.assert_called_once()

    def test_login_challenge_fast_fail_short_probe(self):
        from unittest import mock
        from platforms.chatgpt.register import RegistrationEngine

        eng = RegistrationEngine(
            email_service=mock.Mock(
                service_type=type("ST", (), {"value": "cloudflare_d1_api"})(),
                create_email=mock.Mock(return_value={"email": "a@b.com", "service_id": "1"}),
                get_verification_code=mock.Mock(side_effect=TimeoutError("等待验证码超时 (12s)")),
            ),
            proxy_url=None,
            callback_logger=lambda m: None,
        )
        eng.email = "a@b.com"
        eng.email_info = {"service_id": "1"}
        eng._otp_sent_at = 1.0
        eng._otp_login_challenge = True
        eng.session = mock.Mock()
        eng.session.get = mock.Mock(return_value=mock.Mock(status_code=200))

        with mock.patch.dict(os.environ, {
            "OPENAI_OTP_LOGIN_CHALLENGE_FAST_FAIL": "1",
            "OPENAI_OTP_LOGIN_CHALLENGE_PROBE_SECS": "12",
            "OPENAI_REGISTER_NO_DELAY": "1",
        }):
            # Make mailbox slices return immediately so test is fast.
            eng.email_service.get_verification_code = mock.Mock(
                side_effect=TimeoutError("等待验证码超时 (1s)")
            )
            # Avoid real sleeping inside wait loop by patching time.
            t0 = [1000.0]

            def fake_time():
                # advance ~6s per call so probe ends quickly
                t0[0] += 6.0
                return t0[0]

            with mock.patch("platforms.chatgpt.register.time.time", side_effect=fake_time):
                with mock.patch("platforms.chatgpt.register.time.sleep", return_value=None):
                    code = eng._get_verification_code()
        self.assertIsNone(code)
        # Should not have spun for many resends under fast-fail.
        self.assertLessEqual(eng.email_service.get_verification_code.call_count, 6)


    def test_skip_codex_env_default(self):
        import os
        from unittest import mock
        from platforms.chatgpt import register as regmod

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_SKIP_CODEX", None)
            self.assertTrue(regmod._env_truthy("OPENAI_SKIP_CODEX", "1"))
        with mock.patch.dict(os.environ, {"OPENAI_SKIP_CODEX": "0"}):
            self.assertFalse(regmod._env_truthy("OPENAI_SKIP_CODEX", "1"))
        with mock.patch.dict(os.environ, {"OPENAI_SKIP_CODEX": "1"}):
            self.assertTrue(regmod._env_truthy("OPENAI_SKIP_CODEX", "0"))


if __name__ == "__main__":
    unittest.main()
