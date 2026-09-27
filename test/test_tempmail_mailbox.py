"""123nhh/tempmail 收信 provider：对着进程内的假 tempmail HTTP 服务跑真实请求。"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse

_ENGINES_DIR = str(Path(__file__).resolve().parents[1] / "gpt_free_register" / "engines")
if _ENGINES_DIR not in sys.path:
    sys.path.append(_ENGINES_DIR)

from core import tempmail_mailbox as tm  # noqa: E402
from core.base_mailbox import MailboxAccount, create_mailbox  # noqa: E402
from core.tempmail_mailbox import TempMailError, TempMailMailbox  # noqa: E402

GOOD_KEY = "tm_good"


class FakeTempmail:
    """Just enough of the tempmail /api surface, with the same JSON shapes."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mailboxes: dict[str, dict] = {}
        self.emails: dict[str, list[dict]] = {}
        self.domains = [
            {"id": 1, "domain": "mail.test", "base_domain": "mail.test", "is_active": True,
             "supports_single": True, "supports_wildcard": True},
            {"id": 2, "domain": "*.wild.test", "base_domain": "wild.test", "is_active": True,
             "supports_single": False, "supports_wildcard": True},
        ]
        self.requests: list[tuple[str, str]] = []
        self.created_bodies: list[dict] = []
        self.clock_skew = 0.0  # server clock - real clock
        self.rate_limited = False

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self.clock_skew)

    def add_mailbox(self, address: str, *, ttl_secs: float = 1800) -> dict:
        box = {
            "id": str(uuid.uuid4()),
            "address": address.split("@", 1)[0],
            "full_address": address,
            "created_at": self.now().isoformat(),
            "expires_at": (self.now() + timedelta(seconds=ttl_secs)).isoformat(),
        }
        self.mailboxes[box["id"]] = box
        self.emails[box["id"]] = []
        return box

    def deliver(self, address: str, *, subject: str, body_text: str = "", sender="noreply@tm.openai.com",
                received_at: datetime | None = None) -> str:
        with self.lock:
            box = next(b for b in self.mailboxes.values() if b["full_address"] == address)
            email_id = str(uuid.uuid4())
            self.emails[box["id"]].insert(0, {
                "id": email_id, "mailbox_id": box["id"], "sender": sender, "subject": subject,
                "body_text": body_text, "body_html": "", "raw_message": "", "size_bytes": len(body_text),
                "received_at": (received_at or self.now()).isoformat(),
            })
            return email_id

    def handle(self, method: str, path: str, query: dict, body: dict, auth: str):
        self.requests.append((method, path))
        if auth != f"Bearer {GOOD_KEY}":
            return 401, {"error": "invalid api_key"}
        if self.rate_limited:
            return 429, {"error": "rate limit exceeded", "limit": 500, "retry_after": 60}
        parts = [p for p in path.split("/") if p][1:]  # drop "api"
        with self.lock:
            if parts == ["me"]:
                return 200, {"id": "acc", "username": "tester", "is_admin": False}
            if parts == ["domains"]:
                return 200, {"domains": self.domains}
            if parts == ["mailboxes"] and method == "POST":
                self.created_bodies.append(dict(body))
                host = body.get("domain") or "mail.test"
                if body.get("mode") == "multi":
                    host = f"{body.get('subdomain') or 'gmail.mx'}.{host}"
                address = f"{body.get('address') or 'rand0m'}@{host}".lower()
                if any(b["full_address"] == address for b in self.mailboxes.values()):
                    return 409, {"error": "address already taken, try again"}
                return 201, {"mailbox": self.add_mailbox(address)}
            if parts == ["mailboxes"] and method == "GET":
                rows = sorted(self.mailboxes.values(), key=lambda b: b["created_at"], reverse=True)
                return 200, {"data": rows, "total": len(rows), "page": 1, "size": 100}
            if len(parts) >= 2 and parts[0] == "mailboxes":
                box_id = parts[1]
                if box_id not in self.mailboxes:
                    return 404, {"error": "mailbox not found"}
                if len(parts) == 2 and method == "DELETE":
                    self.mailboxes.pop(box_id)
                    self.emails.pop(box_id, None)
                    return 200, {"message": "mailbox deleted"}
                if parts[2:] == ["emails"]:
                    rows = [{k: e[k] for k in ("id", "sender", "subject", "size_bytes", "received_at")}
                            for e in self.emails[box_id]]
                    return 200, {"data": rows, "total": len(rows), "page": 1, "size": 20}
                if len(parts) == 4 and parts[2] == "emails":
                    for e in self.emails[box_id]:
                        if e["id"] == parts[3]:
                            return 200, {"email": e}
                    return 404, {"error": "email not found"}
        return 404, {"error": "not found"}


def _serve(fake: FakeTempmail) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence
            pass

        def date_time_string(self, timestamp=None):
            return formatdate(time.time() + fake.clock_skew, usegmt=True)

        def _dispatch(self, method: str):
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else {}
            status, payload = fake.handle(method, url.path, parse_qs(url.query), body,
                                          self.headers.get("Authorization") or "")
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


class TempmailTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTempmail()
        self.server = _serve(self.fake)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        tm._COOLDOWN_UNTIL.clear()
        tm._DOMAINS_CACHE.clear()
        self.addCleanup(tm._COOLDOWN_UNTIL.clear)
        self.addCleanup(tm._DOMAINS_CACHE.clear)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def mailbox(self, **kwargs) -> TempMailMailbox:
        opts = {"base_url": self.base_url, "api_key": GOOD_KEY, "domain": "mail.test", "mode": "single"}
        opts.update(kwargs)
        box = TempMailMailbox(**opts)
        self.addCleanup(box.close)
        return box

    def deliver_later(self, delay: float, address: str, **kwargs) -> None:
        timer = threading.Timer(delay, lambda: self.fake.deliver(address, **kwargs))
        timer.start()
        self.addCleanup(timer.cancel)

    def detail_requests(self) -> int:
        return sum(1 for _, path in self.fake.requests if re.search(r"/emails/[^/]+$", path))


class CreateMailboxTests(TempmailTestCase):
    def test_creates_server_side_mailbox_with_domain_and_mode(self):
        box = self.mailbox()
        account = box.get_email()
        self.assertTrue(account.email.endswith("@mail.test"))
        self.assertEqual(account.account_id, account.extra["tempmail_mailbox_id"])
        self.assertIn(account.account_id, self.fake.mailboxes)
        body = self.fake.created_bodies[-1]
        self.assertEqual((body["domain"], body["mode"]), ("mail.test", "single"))
        self.assertEqual(account.extra["provider_resource"]["provider_name"], "tempmail")

    def test_fresh_mailbox_baseline_costs_no_request(self):
        box = self.mailbox()
        account = box.get_email()
        before = len(self.fake.requests)
        self.assertEqual(box.get_current_ids(account), set())
        self.assertEqual(len(self.fake.requests), before)

    def test_address_collision_retries_with_new_local_part(self):
        self.fake.add_mailbox("taken@mail.test")
        box = self.mailbox()
        with mock.patch.object(box, "_make_local_part", side_effect=["taken", "fresh"]):
            account = box.get_email()
        self.assertEqual(account.email, "fresh@mail.test")

    def test_bad_key_fails_fast(self):
        box = self.mailbox(api_key="tm_wrong")
        with self.assertRaises(TempMailError) as ctx:
            box.get_email()
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(len(self.fake.requests), 1)

    def test_base_url_with_api_suffix_is_accepted(self):
        box = self.mailbox(base_url=self.base_url + "/api/")
        self.assertTrue(box.get_email().email.endswith("@mail.test"))

    def test_missing_config_raises(self):
        with self.assertRaises(RuntimeError):
            TempMailMailbox(base_url="", api_key="")


class WaitForCodeTests(TempmailTestCase):
    def test_code_from_subject_needs_no_detail_request(self):
        box = self.mailbox()
        account = box.get_email()
        self.deliver_later(0.3, account.email, subject="Your ChatGPT code is 482913")
        code = box.wait_for_code(account, timeout=5, otp_sent_at=time.time())
        self.assertEqual(code, "482913")
        self.assertEqual(self.detail_requests(), 0)

    def test_code_from_body_when_subject_has_none(self):
        box = self.mailbox()
        account = box.get_email()
        self.fake.deliver(account.email, subject="Verify your email",
                          body_text="Enter this temporary verification code to continue:\n\n615243\n")
        self.assertEqual(box.wait_for_code(account, timeout=5), "615243")
        self.assertEqual(self.detail_requests(), 1)

    def test_second_otp_never_returns_first_code(self):
        box = self.mailbox()
        account = box.get_email()
        self.fake.deliver(account.email, subject="Your ChatGPT code is 111111")
        self.assertEqual(box.wait_for_code(account, timeout=5), "111111")
        self.deliver_later(0.3, account.email, subject="Your ChatGPT code is 222222")
        self.assertEqual(box.wait_for_code(account, timeout=5), "222222")

    def test_before_ids_are_skipped(self):
        box = self.mailbox()
        account = box.get_email()
        old = self.fake.deliver(account.email, subject="Your ChatGPT code is 333333")
        with self.assertRaises(TimeoutError):
            box.wait_for_code(account, timeout=0.5, before_ids={old})

    def test_min_ts_is_compared_on_the_server_clock(self):
        # Server clock 2 minutes behind: a mail received right now carries a
        # received_at that looks older than otp_sent_at - 15s on our clock.
        self.fake.clock_skew = -120
        box = self.mailbox()
        account = box.get_email()
        sent_at = time.time()
        self.deliver_later(0.3, account.email, subject="Your ChatGPT code is 444444")
        self.assertEqual(box.wait_for_code(account, timeout=5, otp_sent_at=sent_at), "444444")

    def test_mail_older_than_otp_send_is_ignored(self):
        box = self.mailbox()
        account = box.get_email()
        self.fake.deliver(account.email, subject="Your ChatGPT code is 555555",
                          received_at=self.fake.now() - timedelta(minutes=5))
        with self.assertRaises(TimeoutError):
            box.wait_for_code(account, timeout=0.5, otp_sent_at=time.time())

    def test_bad_key_during_wait_raises_instead_of_timing_out(self):
        box = self.mailbox()
        account = box.get_email()
        box.api_key = "tm_revoked"
        box.close()  # drop the session carrying the old header
        started = time.time()
        with self.assertRaises(TempMailError):
            box.wait_for_code(account, timeout=10)
        self.assertLess(time.time() - started, 3)

    def test_mailbox_reaped_mid_wait_is_recreated_under_same_address(self):
        box = self.mailbox()
        account = box.get_email()
        self.fake.mailboxes.pop(account.account_id)  # server TTL cleaner

        def deliver():
            deadline = time.time() + 3
            while time.time() < deadline:
                if any(b["full_address"] == account.email for b in self.fake.mailboxes.values()):
                    self.fake.deliver(account.email, subject="Your ChatGPT code is 666666")
                    return
                time.sleep(0.05)

        threading.Thread(target=deliver, daemon=True).start()
        self.assertEqual(box.wait_for_code(account, timeout=5), "666666")

    def test_rate_limit_silences_every_request_for_the_window(self):
        box = self.mailbox()
        account = box.get_email()
        self.fake.rate_limited = True
        with self.assertRaises(TempMailError) as ctx:
            box._list_emails(account.account_id)
        self.assertEqual(ctx.exception.status, 429)
        self.assertGreater(box.cooldown_remaining(), 55)
        sent = len(self.fake.requests)
        other = self.mailbox()  # another registration thread, same key
        with self.assertRaises(TempMailError):
            other._list_emails(account.account_id)
        self.assertEqual(len(self.fake.requests), sent)


LABEL_RE = re.compile(r"^[a-z][a-z0-9]{3,7}$")


class MultiLevelDomainTests(TempmailTestCase):
    def created(self) -> dict:
        return self.fake.created_bodies[-1]

    def test_multi_mode_generates_a_short_random_subdomain(self):
        account = self.mailbox(mode="multi").get_email()
        body = self.created()
        self.assertEqual((body["mode"], body["domain"]), ("multi", "mail.test"))
        labels = body["subdomain"].split(".")
        self.assertEqual(len(labels), 2)
        self.assertTrue(all(LABEL_RE.match(label) for label in labels), labels)
        self.assertEqual(account.email.split("@", 1)[1], f"{body['subdomain']}.mail.test")

    def test_every_account_gets_its_own_host(self):
        box = self.mailbox(mode="multi")
        hosts = {box.get_email().email.split("@", 1)[1] for _ in range(5)}
        self.assertEqual(len(hosts), 5)

    def test_depth_zero_leaves_the_subdomain_to_tempmail(self):
        self.mailbox(mode="multi", subdomain_depth=0).get_email()
        self.assertNotIn("subdomain", self.created())

    def test_wildcard_pool_entry_forces_multi(self):
        account = self.mailbox(domain="*.wild.test", mode="single", subdomain_depth=3).get_email()
        body = self.created()
        self.assertEqual((body["mode"], body["domain"]), ("multi", "wild.test"))
        self.assertEqual(len(body["subdomain"].split(".")), 3)
        self.assertTrue(account.email.endswith(".wild.test"))

    def test_fixed_subdomain_pool_entry(self):
        account = self.mailbox(domain="a.b.mail.test", mode="single").get_email()
        body = self.created()
        self.assertEqual((body["mode"], body["domain"], body["subdomain"]), ("multi", "mail.test", "a.b"))
        self.assertTrue(account.email.endswith("@a.b.mail.test"))

    def test_wildcard_under_a_fixed_prefix_keeps_the_prefix(self):
        # depth 0 can't keep a prefix (tempmail only generates right under the
        # base), so the default depth is used.
        self.mailbox(domain="*.x.mail.test", subdomain_depth=0).get_email()
        labels = self.created()["subdomain"].split(".")
        self.assertEqual(labels[-1], "x")
        self.assertEqual(len(labels), tm.TempMailMailbox.DEFAULT_SUBDOMAIN_DEPTH + 1)

    def test_single_mode_on_a_base_domain_is_unchanged(self):
        self.mailbox(domain="mail.test", mode="single").get_email()
        self.assertEqual(self.created(), {"domain": "mail.test", "mode": "single", "address": mock.ANY})

    def test_multi_without_domain_lets_tempmail_pick_the_base(self):
        self.mailbox(domain="", mode="multi").get_email()
        body = self.created()
        self.assertEqual(body["mode"], "multi")
        self.assertNotIn("domain", body)
        self.assertEqual(len(body["subdomain"].split(".")), 2)

    def test_foreign_domain_fails_before_creating_anything(self):
        with self.assertRaises(TempMailError) as ctx:
            self.mailbox(domain="*.cf-only.example").get_email()
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.fake.created_bodies, [])

    def test_domain_list_is_cached_across_registrations(self):
        for _ in range(3):
            self.mailbox(domain="*.wild.test").get_email()
        self.assertEqual(sum(1 for _, path in self.fake.requests if path == "/api/domains"), 1)

    def test_newly_added_domain_is_found_without_waiting_for_the_cache(self):
        self.mailbox(domain="mail.test").get_email()  # warms the cache
        self.fake.domains.append({"id": 3, "domain": "new.test", "base_domain": "new.test", "is_active": True,
                                  "supports_single": True, "supports_wildcard": True})
        self.assertTrue(self.mailbox(domain="new.test").get_email().email.endswith("@new.test"))


class BindExistingTests(TempmailTestCase):
    def test_live_mailbox_is_reused(self):
        existing = self.fake.add_mailbox("old@mail.test")
        account = self.mailbox().bind_existing("old@mail.test")
        self.assertEqual(account.account_id, existing["id"])

    def test_expiring_mailbox_is_recreated(self):
        existing = self.fake.add_mailbox("old@mail.test", ttl_secs=30)
        account = self.mailbox().bind_existing("OLD@mail.test")
        self.assertEqual(account.email, "old@mail.test")
        self.assertNotEqual(account.account_id, existing["id"])
        self.assertNotIn(existing["id"], self.fake.mailboxes)

    def test_reaped_multi_level_address_is_recreated_with_subdomain(self):
        account = self.mailbox().bind_existing("abc@inbox.gmail.wild.test")
        self.assertEqual(account.email, "abc@inbox.gmail.wild.test")
        body = self.fake.created_bodies[-1]
        self.assertEqual((body["mode"], body["domain"], body["subdomain"]), ("multi", "wild.test", "inbox.gmail"))

    def test_foreign_domain_is_rejected(self):
        with self.assertRaises(TempMailError):
            self.mailbox().bind_existing("abc@cf-only.example")

    def test_plain_account_resolves_mailbox_id_by_address(self):
        existing = self.fake.add_mailbox("plain@mail.test")
        box = self.mailbox()
        account = MailboxAccount(email="plain@mail.test", account_id="plain@mail.test", extra={})
        self.fake.deliver("plain@mail.test", subject="Your ChatGPT code is 777777")
        self.assertEqual(box.wait_for_code(account, timeout=5), "777777")
        self.assertIn(existing["id"], {p.split("/")[3] for _, p in self.fake.requests if "/emails" in p})


class FactoryTests(TempmailTestCase):
    def test_registry_builds_tempmail_from_extra(self):
        box = create_mailbox("tempmail", extra={
            "tempmail_base_url": self.base_url, "tempmail_api_key": GOOD_KEY, "tempmail_mode": "multi",
        })
        self.addCleanup(box.close)
        self.assertIsInstance(box, TempMailMailbox)
        self.assertEqual(box.mode, "multi")
        self.assertIsNone(box.proxy)

    def test_registry_reports_missing_config(self):
        with mock.patch.dict("os.environ", {"TEMPMAIL_BASE_URL": "", "TEMPMAIL_API_KEY": ""}):
            with self.assertRaises(RuntimeError):
                create_mailbox("tempmail", extra={})


class ServiceSettingsTests(unittest.TestCase):
    def setUp(self):
        from services import gpt_register_service as svc

        self.svc = svc

    def test_provider_and_fields_normalize(self):
        s = self.svc.normalize_settings({
            "mail_provider": "TempMail", "tempmail_base_url": "mail.example.com/api/",
            "tempmail_mode": "bogus", "tempmail_api_key": " tm_x ",
        })
        self.assertEqual(s["mail_provider"], "tempmail")
        self.assertEqual(s["tempmail_base_url"], "https://mail.example.com")
        self.assertEqual(s["tempmail_mode"], "")
        self.assertEqual(s["tempmail_api_key"], "tm_x")
        self.assertEqual(self.svc.normalize_settings({"mail_provider": "outlook"})["mail_provider"],
                         "cloudflare_d1_api")
        self.assertEqual(self.svc.normalize_settings(None)["tempmail_mode"], "single")

    def test_api_key_is_masked_and_kept_on_blank_patch(self):
        stored = self.svc.normalize_settings({"tempmail_api_key": "tm_secret"})
        public = self.svc.public_settings(stored)
        self.assertEqual(public["tempmail_api_key"], "")
        self.assertTrue(public["has_tempmail_api_key"])
        patch = {"tempmail_api_key": "", "count": 2}
        merged = self.svc.keep_stored_secrets({**stored, **patch}, patch, stored)
        self.assertEqual(merged["tempmail_api_key"], "tm_secret")

    def test_domain_pool_key_follows_provider(self):
        self.assertEqual(self.svc.mail_domain_setting_keys({"mail_provider": "tempmail"}),
                         ("tempmail_domains", "tempmail_domain"))
        self.assertEqual(self.svc.mail_domain_setting_keys({"mail_provider": "cloudflare_d1_api"}),
                         ("cfd1_domains", "cfd1_domain"))

    def test_job_fails_fast_without_tempmail_config(self):
        settings = self.svc.normalize_settings({"mail_provider": "tempmail"})
        with self.assertRaises(RuntimeError) as ctx:
            self.svc.GptRegisterService.__new__(self.svc.GptRegisterService)._validate_engines(settings)
        self.assertIn("tempmail_base_url", str(ctx.exception))

    def test_api_model_declares_every_setting(self):
        from api.gpt_register import GptRegisterSettingsUpdate

        missing = set(self.svc.DEFAULT_SETTINGS) - set(GptRegisterSettingsUpdate.model_fields)
        self.assertEqual(missing, set())


class ProbeTests(TempmailTestCase):
    def test_probe_lists_active_domains(self):
        from services.gpt_register_service import probe_tempmail

        out = probe_tempmail(self.base_url + "/api", GOOD_KEY)
        self.assertEqual(out["username"], "tester")
        self.assertEqual(out["domains"][0], {"domain": "mail.test", "single": True, "multi": True})
        self.assertEqual(out["domains"][1], {"domain": "wild.test", "single": False, "multi": True})

    def test_probe_bad_key(self):
        from services.gpt_register_service import probe_tempmail

        with self.assertRaisesRegex(ValueError, "API Key"):
            probe_tempmail(self.base_url, "tm_wrong")


class CodexUpgradeRoutingTests(unittest.TestCase):
    def test_accounts_route_to_the_provider_that_minted_them(self):
        from gpt_free_register.codex_upgrade import _mail_provider_for

        cfg = {"mail_provider": "tempmail", "cfd1_domains": "cf.example\nold.example",
               "tempmail_base_url": "https://m", "tempmail_api_key": "k"}
        with mock.patch.dict("os.environ", {"CFD1_DOMAIN": "", "CF_MAIL_DOMAIN": "",
                                            "CLOUDFLARE_EMAIL_DOMAIN": "", "MAIL_DOMAIN": ""}):
            self.assertEqual(_mail_provider_for(cfg, "a@old.example"), "cloudflare_d1_api")
            self.assertEqual(_mail_provider_for(cfg, "a@mail.test"), "tempmail")
            cfg["mail_provider"] = "cloudflare_d1_api"
            self.assertEqual(_mail_provider_for(cfg, "a@mail.test"), "tempmail")
            self.assertEqual(_mail_provider_for(cfg, "a@cf.example"), "cloudflare_d1_api")

    def test_bind_uses_server_side_lookup_when_available(self):
        from gpt_free_register.codex_upgrade import _bind_existing_mailbox_account

        bound = SimpleNamespace(email="x@mail.test", account_id="id-1", extra={})
        mailbox = SimpleNamespace(bind_existing=mock.Mock(return_value=bound))
        out = _bind_existing_mailbox_account(mailbox, "X@mail.test")
        mailbox.bind_existing.assert_called_once_with("x@mail.test")
        self.assertTrue(out.extra["fixed_email"])


if __name__ == "__main__":
    unittest.main()


class AdminEndpointTests(unittest.TestCase):
    """POST /api/gpt-register/tempmail/test —— 未保存的表单值也能先测连通性。"""

    def _client(self, *, stored=None, probe=None):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import api.gpt_register as reg_module

        fake_config = mock.Mock()
        fake_config.get.return_value = dict(stored or {})
        patchers = [
            mock.patch.object(reg_module, "require_admin", lambda _authorization: {"role": "admin"}),
            mock.patch.object(reg_module, "gpt_register_config", fake_config),
        ]
        if probe is not None:
            patchers.append(mock.patch.object(reg_module, "probe_tempmail", probe))
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(reg_module.create_router())
        return TestClient(app), fake_config

    def test_form_values_win_over_stored(self):
        seen = {}

        def probe(base_url, api_key):
            seen.update(base_url=base_url, api_key=api_key)
            return {"ok": True, "domains": []}

        client, _ = self._client(stored={"tempmail_base_url": "https://stored", "tempmail_api_key": "tm_stored"},
                                 probe=probe)
        resp = client.post("/api/gpt-register/tempmail/test",
                           json={"tempmail_base_url": "https://form", "tempmail_api_key": "tm_form"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(seen, {"base_url": "https://form", "api_key": "tm_form"})

    def test_blank_key_falls_back_to_stored(self):
        seen = {}

        def probe(base_url, api_key):
            seen.update(base_url=base_url, api_key=api_key)
            return {"ok": True, "domains": []}

        client, _ = self._client(stored={"tempmail_base_url": "https://stored", "tempmail_api_key": "tm_stored"},
                                 probe=probe)
        client.post("/api/gpt-register/tempmail/test", json={"tempmail_base_url": "", "tempmail_api_key": ""})
        self.assertEqual(seen, {"base_url": "https://stored", "api_key": "tm_stored"})

    def test_probe_error_is_a_400(self):
        def probe(_base_url, _api_key):
            raise ValueError("连不上 tempmail")

        client, _ = self._client(probe=probe)
        resp = client.post("/api/gpt-register/tempmail/test", json={"tempmail_base_url": "https://x"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("连不上", resp.json()["detail"]["error"])

    def test_saved_settings_never_return_the_key(self):
        client, fake_config = self._client()
        fake_config.update.return_value = {"tempmail_api_key": "tm_secret"}
        resp = client.post("/api/gpt-register/settings", json={"tempmail_api_key": "tm_secret"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["settings"]["tempmail_api_key"], "")
        self.assertTrue(resp.json()["settings"]["has_tempmail_api_key"])


class DomainPoolPickTests(unittest.TestCase):
    def test_domain_pool_pick_survives_normalization(self):
        from services import gpt_register_service as svc

        # The per-registration pick key must be a declared setting, or
        # normalize_settings drops it and the pool silently does nothing.
        picked = svc.normalize_settings({"mail_provider": "tempmail", "tempmail_domain": "@Mail.Example.com"})
        self.assertEqual(picked["tempmail_domain"], "mail.example.com")
        self.assertIn("tempmail_domain", svc.DEFAULT_SETTINGS)


class RunnerWiringTests(unittest.TestCase):
    def test_job_settings_reach_the_factory(self):
        from gpt_free_register.runner import _create_mailbox, mailbox_extra

        cfg = {"mail_provider": "tempmail", "tempmail_base_url": "https://mail.example.com",
               "tempmail_api_key": "tm_k", "tempmail_domain": "b.example", "tempmail_mode": "single",
               "cfd1_domain": "cf.example"}
        extra = mailbox_extra(cfg, "tempmail")
        self.assertNotIn("cfd1_domain", extra)
        box = _create_mailbox("tempmail", extra, None)
        self.addCleanup(box.close)
        self.assertIsInstance(box, TempMailMailbox)
        self.assertEqual((box.base_url, box.domain, box.mode), ("https://mail.example.com", "b.example", "single"))
        self.assertIsNone(box.proxy)
        self.assertNotIn("tempmail_api_key", mailbox_extra(cfg, "cloudflare_d1_api"))

    def test_subdomain_depth_zero_reaches_the_factory(self):
        from gpt_free_register.runner import _create_mailbox, mailbox_extra
        from services.gpt_register_service import normalize_settings

        settings = normalize_settings({"mail_provider": "tempmail", "tempmail_base_url": "https://m",
                                       "tempmail_api_key": "k", "tempmail_subdomain_depth": 0})
        extra = mailbox_extra(settings, "tempmail")
        self.assertEqual(extra["tempmail_subdomain_depth"], "0")
        box = _create_mailbox("tempmail", extra, None)
        self.addCleanup(box.close)
        self.assertEqual(box.subdomain_depth, 0)

    def test_subdomain_depth_is_clamped(self):
        from services.gpt_register_service import normalize_settings

        self.assertEqual(normalize_settings({"tempmail_subdomain_depth": 9})["tempmail_subdomain_depth"], 5)
        self.assertEqual(normalize_settings({"tempmail_subdomain_depth": -1})["tempmail_subdomain_depth"], 0)
        self.assertEqual(normalize_settings({})["tempmail_subdomain_depth"], 2)
