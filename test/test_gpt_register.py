"""GPT free batch register settings + job helpers."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.gpt_register_service import (
    GptRegisterConfig,
    GptRegisterService,
    _circuit_break_threshold,
    _extract_json_object,
    _is_network_register_error,
    _mask_proxy_url,
    normalize_settings,
    parse_proxy_pool,
    pick_proxy,
    public_settings,
)


class NormalizeSettingsTest(unittest.TestCase):
    def test_defaults_and_clamps(self):
        s = normalize_settings({"count": 999, "concurrency": 0, "executor": "weird"})
        self.assertEqual(s["count"], 50)
        self.assertEqual(s["concurrency"], 1)
        self.assertEqual(s["executor"], "protocol")
        self.assertTrue(s["push_enabled"])

    def test_removed_register_paths_are_clamped(self):
        s = normalize_settings({
            "executor": "headless",
            "mail_provider": "outlook_token",
            "captcha": "yescaptcha_api",
        })
        self.assertEqual(s["executor"], "protocol")
        self.assertEqual(s["mail_provider"], "cloudflare_d1_api")
        self.assertEqual(s["captcha"], "")

    def test_public_hides_auth_key(self):
        raw = normalize_settings({"chatgpt2api_auth_key": "secret-key"})
        pub = public_settings(raw)
        self.assertEqual(pub["chatgpt2api_auth_key"], "")
        self.assertTrue(pub["has_chatgpt2api_auth_key"])

    def test_skip_codex_default_true(self):
        s = normalize_settings(None)
        self.assertTrue(s["skip_codex"])
        s2 = normalize_settings({"skip_codex": False})
        self.assertFalse(s2["skip_codex"])

    def test_stable_register_defaults(self):
        s = normalize_settings(None)
        self.assertEqual(s["concurrency"], 1)
        self.assertEqual(s["interval_secs"], 3)
        self.assertFalse(s["register_no_delay"])
        self.assertTrue(s["auto_replenish_enabled"])
        self.assertEqual(s["auto_replenish_min_available"], 2)
        self.assertEqual(s["auto_replenish_batch"], 1)

    def test_auto_replenish_clamps(self):
        s = normalize_settings(
            {
                "auto_replenish_min_available": 99,
                "auto_replenish_batch": 0,
                "auto_replenish_interval_secs": 5,
            }
        )
        self.assertEqual(s["auto_replenish_min_available"], 20)
        self.assertEqual(s["auto_replenish_batch"], 1)
        self.assertEqual(s["auto_replenish_interval_secs"], 30)

    def test_api_model_accepts_latency_fields(self):
        """Regression: undeclared fields were dropped by Pydantic → UI could not uncheck skip_codex."""
        from api.gpt_register import GptRegisterSettingsUpdate

        body = GptRegisterSettingsUpdate(
            skip_codex=False,
            register_no_delay=True,
            so_collect_ms="1000",
            count=2,
            auto_replenish_enabled=False,
            auto_replenish_min_available=3,
        )
        patch = body.model_dump(exclude_none=True)
        self.assertIn("skip_codex", patch)
        self.assertFalse(patch["skip_codex"])
        self.assertTrue(patch["register_no_delay"])
        self.assertEqual(patch["so_collect_ms"], "1000")
        self.assertEqual(patch["count"], 2)
        self.assertFalse(patch["auto_replenish_enabled"])
        self.assertEqual(patch["auto_replenish_min_available"], 3)


class ConfigStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "gpt_reg.json"
        self.cfg = GptRegisterConfig(path=self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_update_keeps_auth_key_when_empty(self):
        self.cfg.update({"chatgpt2api_auth_key": "k1", "count": 3})
        updated = self.cfg.update({"chatgpt2api_auth_key": "", "count": 5})
        self.assertEqual(updated["chatgpt2api_auth_key"], "k1")
        self.assertEqual(updated["count"], 5)

    def test_update_persists_skip_codex_false(self):
        updated = self.cfg.update({"skip_codex": False, "register_no_delay": True})
        self.assertFalse(updated["skip_codex"])
        self.assertTrue(updated["register_no_delay"])
        # reload from disk
        reloaded = GptRegisterConfig(path=self.path).get()
        self.assertFalse(reloaded["skip_codex"])
        self.assertTrue(reloaded["register_no_delay"])


class ExtractJsonTest(unittest.TestCase):
    def test_extract_trailing_object(self):
        text = 'noise\n{"email":"a@b.c","token":"tok"}\n'
        data = _extract_json_object(text)
        assert data is not None
        self.assertEqual(data["email"], "a@b.c")


class ServiceValidateTest(unittest.TestCase):
    def test_validate_missing_dir(self):
        svc = GptRegisterService(config_store=GptRegisterConfig(path=Path("/tmp/nope-gpt-reg.json")))
        # normalize falls back to builtin when path is missing; validation still
        # rejects an explicitly broken engines_dir after normalize is bypassed.
        broken = {
            **normalize_settings(None),
            "engines_dir": "/tmp/does-not-exist-gpt-reg-engines",
        }
        with self.assertRaises(RuntimeError):
            svc._validate_engines(broken)

    def test_missing_path_falls_back_to_builtin(self):
        s = normalize_settings({"engines_dir": "/app/gpt_free_register/engines"})
        self.assertTrue(Path(s["engines_dir"]).is_dir())
        self.assertTrue((Path(s["engines_dir"]) / "platforms" / "chatgpt" / "plugin.py").is_file())


class ServiceRegisterOnceTest(unittest.TestCase):
    def test_register_once_parses_cli_output(self):
        svc = GptRegisterService(config_store=GptRegisterConfig(path=Path("/tmp/nope-gpt-reg2.json")))
        settings = normalize_settings(
            {
                "engines_dir": "/tmp",
                "run_mode": "subprocess",
                "push_enabled": False,
                "timeout_secs": 30,
            }
        )
        fake = mock.Mock()
        fake.stdout = '{"email":"u@x.com","token":"at-1","extra":{"access_token":"at-1"}}'
        fake.stderr = ""
        fake.returncode = 0
        with mock.patch("services.gpt_register_service.subprocess.run", return_value=fake):
            with mock.patch.object(svc, "_resolve_python", return_value="python3"):
                out = svc._register_once(settings)
        self.assertTrue(out["ok"])
        self.assertEqual(out["email"], "u@x.com")
        self.assertTrue(out["has_token"])

    def test_builtin_engines_default(self):
        s = normalize_settings({"engines_dir": "/root/any-register-engines"})
        self.assertIn("gpt_free_register", s["engines_dir"].replace("\\", "/"))
        self.assertEqual(s["run_mode"], "inprocess")

    def test_inprocess_uses_runner(self):
        svc = GptRegisterService(config_store=GptRegisterConfig(path=Path("/tmp/nope-gpt-reg3.json")))
        settings = normalize_settings({"run_mode": "inprocess", "push_enabled": False})
        with mock.patch(
            "gpt_free_register.runner.register_chatgpt_once",
            return_value={
                "email": "a@b.c",
                "token": "tok",
                "extra": {"access_token": "tok"},
                "status": "registered",
            },
        ):
            out = svc._register_once_inprocess(settings)
        self.assertTrue(out["ok"])
        self.assertEqual(out["email"], "a@b.c")
        self.assertEqual(out["mode"], "inprocess")

    def test_inprocess_local_push_imports(self):
        svc = GptRegisterService(config_store=GptRegisterConfig(path=Path("/tmp/nope-gpt-reg4.json")))
        settings = normalize_settings(
            {"run_mode": "inprocess", "push_enabled": True, "push_mode": "local", "dry_run": False}
        )
        with mock.patch(
            "gpt_free_register.runner.register_chatgpt_once",
            return_value={
                "email": "b@c.d",
                "token": "tok-2",
                "extra": {"access_token": "tok-2"},
                "status": "registered",
            },
        ):
            with mock.patch.object(svc, "_import_local", return_value=1) as imp:
                out = svc._register_once_inprocess(settings)
        self.assertTrue(out["ok"])
        self.assertEqual(out["added"], 1)
        imp.assert_called_once()



class JobCompletionLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.cfg_path = self.data / "gpt_reg.json"
        self.jobs_path = self.data / "jobs.json"
        self.logs_dir = self.data / "gpt_register_logs"

    def test_run_job_writes_summary_and_completion_file(self):
        import services.gpt_register_service as mod

        with mock.patch.object(mod, "DATA_DIR", self.data):
            with mock.patch.object(mod, "GPT_REGISTER_JOBS_FILE", self.jobs_path):
                svc = GptRegisterService(config_store=GptRegisterConfig(path=self.cfg_path))
                settings = normalize_settings(
                    {
                        "count": 2,
                        "concurrency": 1,
                        "interval_secs": 0,
                        "run_mode": "inprocess",
                        "push_enabled": False,
                    }
                )
                # engines validate against real builtin path from normalize
                outcomes = [
                    {
                        "ok": True,
                        "email": "ok@example.com",
                        "has_token": True,
                        "added": 0,
                        "push": {"ok": True},
                        "error": None,
                        "logs": ["step: signup", "step: otp ok"],
                        "mode": "inprocess",
                    },
                    {
                        "ok": False,
                        "email": "bad@example.com",
                        "has_token": False,
                        "added": 0,
                        "error": "otp timeout",
                        "logs": ["step: signup", "error: otp timeout"],
                        "mode": "inprocess",
                    },
                ]
                with mock.patch.object(svc, "_register_once", side_effect=outcomes):
                    job = svc.start_job(settings)
                    # wait for daemon thread
                    import time

                    for _ in range(100):
                        cur = svc.get_job(job["job_id"])
                        if cur and cur.get("status") in {"done", "failed", "cancelled"}:
                            break
                        time.sleep(0.05)
                    cur = svc.get_job(job["job_id"])
                self.assertIsNotNone(cur)
                assert cur is not None
                self.assertEqual(cur["status"], "done")
                self.assertEqual(cur["success"], 1)
                self.assertEqual(cur["failed"], 1)
                self.assertIn("summary", cur)
                self.assertEqual(cur["summary"]["success"], 1)
                self.assertAlmostEqual(float(cur["summary"]["success_rate"]), 50.0)
                self.assertTrue(any("任务结束" in (x.get("message") or "") for x in cur.get("logs") or []))
                self.assertTrue(
                    any("otp timeout" in (x.get("message") or "") for x in cur.get("logs") or [])
                )
                # engine step logs forwarded
                self.assertTrue(
                    any("step: otp ok" in (x.get("message") or "") for x in cur.get("logs") or [])
                )
                log_file = self.data / "gpt_register_logs" / f"{job['job_id']}.json"
                self.assertTrue(log_file.is_file(), f"missing {log_file}")
                import json

                record = json.loads(log_file.read_text(encoding="utf-8"))
                self.assertEqual(record["job_id"], job["job_id"])
                self.assertEqual(record["summary"]["failed"], 1)
                self.assertEqual(len(record["items"]), 2)
                # secrets not present
                self.assertEqual(record["settings"].get("chatgpt2api_auth_key"), "")


class RunnerBootstrapTest(unittest.TestCase):
    def test_bootstrap_creates_provider_tables(self):
        from gpt_free_register import runner as reg_runner

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name)
        db_url = f"sqlite:///{data_dir / 'register_engines.db'}"
        with mock.patch.object(reg_runner, "_data_dir", return_value=data_dir):
            reg_runner._BOOTED = False
            old = os.environ.get("REGISTER_ENGINES_DATABASE_URL")
            os.environ["REGISTER_ENGINES_DATABASE_URL"] = db_url
            try:
                # Rebind the already-imported engine instead of deleting
                # core.db from sys.modules (SQLModel MetaData cannot re-register
                # the same tables in one process).
                reg_runner._bootstrap(Path(reg_runner.default_engines_dir()))
                from sqlmodel import Session, select
                import core.db as engines_db

                with Session(engines_db.engine) as session:
                    rows = session.exec(select(engines_db.ProviderDefinitionModel)).all()
                self.assertGreaterEqual(len(rows), 1)
                self.assertTrue(any(r.provider_key == "cloudflare_d1_api" for r in rows))
                self.assertTrue((data_dir / "register_engines.db").exists())
            finally:
                if old is None:
                    os.environ.pop("REGISTER_ENGINES_DATABASE_URL", None)
                else:
                    os.environ["REGISTER_ENGINES_DATABASE_URL"] = old

    def test_socks_proxy_requires_pysocks(self):
        from gpt_free_register.runner import _ensure_runtime_deps

        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "socks":
                raise ModuleNotFoundError("no socks")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaises(RuntimeError) as ctx:
                _ensure_runtime_deps("socks5h://127.0.0.1:1080")
            self.assertIn("PySocks", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class ImportLocalTest(unittest.TestCase):
    def test_import_local_marks_session_only_and_refreshes(self):
        svc = GptRegisterService(config_store=GptRegisterConfig(path=Path("/tmp/nope-gpt-reg-import.json")))
        settings = normalize_settings({"plan_type": "free", "bind_register_proxy": False})
        account = {
            "email": "s@x.com",
            "password": "",
            "token": "access-only",
            "user_id": "u1",
            "extra": {
                "access_token": "access-only",
                # no refresh_token / id_token → session-only register path
            },
        }
        fake_svc = mock.Mock()
        fake_svc.add_account_items.return_value = {"added": 1, "skipped": 0, "items": []}
        fake_svc.list_accounts.return_value = []
        fake_svc.fetch_remote_info.return_value = {
            "access_token": "access-only",
            "quota": 0,
            "status": "限流",
            "type": "free",
        }
        with mock.patch("services.account_service.account_service", fake_svc):
            added = svc._import_local(account, settings)
        self.assertEqual(added, 1)
        payload = fake_svc.add_account_items.call_args[0][0][0]
        self.assertTrue(payload["session_only"])
        self.assertTrue(payload["fragile"])
        self.assertEqual(payload["source_type"], "register")
        self.assertEqual(int(payload.get("quota") or 0), 30)

    def test_import_local_persists_oai_device_id(self):
        svc = GptRegisterService(config_store=GptRegisterConfig(path=Path("/tmp/nope-gpt-reg-import-did.json")))
        settings = normalize_settings({"plan_type": "free", "bind_register_proxy": False})
        account = {
            "email": "d@x.com",
            "token": "access-did",
            "user_id": "u2",
            "extra": {
                "access_token": "access-did",
                "session_token": "sess",
                "oai-device-id": "did-from-register",
                "profile": {
                    "impersonate": "chrome142",
                    "user_agent": "Mozilla/5.0 isolation-test",
                    "sec_ch_ua": '"Chromium";v="142"',
                },
            },
        }
        fake_svc = mock.Mock()
        fake_svc.add_account_items.return_value = {"added": 1, "skipped": 0, "items": []}
        fake_svc.list_accounts.return_value = []
        fake_svc.fetch_remote_info.return_value = {
            "access_token": "access-did",
            "quota": 25,
            "status": "正常",
            "type": "free",
        }
        with mock.patch("services.account_service.account_service", fake_svc):
            added = svc._import_local(account, settings)
        self.assertEqual(added, 1)
        payload = fake_svc.add_account_items.call_args[0][0][0]
        self.assertEqual(payload["oai-device-id"], "did-from-register")
        self.assertEqual(payload["fp"]["oai-device-id"], "did-from-register")
        self.assertEqual(payload["fp"]["user-agent"], "Mozilla/5.0 isolation-test")
        self.assertEqual(payload["fp"]["sec-ch-ua"], '"Chromium";v="142"')
        # fetch_remote_info runs in a daemon thread; wait briefly
        import time as _time
        for _ in range(50):
            if fake_svc.fetch_remote_info.called:
                break
            _time.sleep(0.02)
        fake_svc.fetch_remote_info.assert_called_once()
        self.assertEqual(fake_svc.fetch_remote_info.call_args[0][0], "access-did")

    def test_import_local_codex_tokens_not_session_only(self):
        svc = GptRegisterService(config_store=GptRegisterConfig(path=Path("/tmp/nope-gpt-reg-import2.json")))
        settings = normalize_settings({"plan_type": "free", "bind_register_proxy": False})
        account = {
            "email": "c@x.com",
            "token": "at-codex",
            "extra": {
                "access_token": "at-codex",
                "refresh_token": "rt-codex",
                "id_token": "id-codex",
            },
        }
        fake_svc = mock.Mock()
        fake_svc.add_account_items.return_value = {"added": 1}
        fake_svc.list_accounts.return_value = [
            {"email": "c@x.com", "access_token": "at-codex-rotated"}
        ]
        fake_svc.fetch_remote_info.return_value = {"access_token": "at-codex-rotated", "quota": 2}
        with mock.patch("services.account_service.account_service", fake_svc):
            added = svc._import_local(account, settings)
        self.assertEqual(added, 1)
        payload = fake_svc.add_account_items.call_args[0][0][0]
        self.assertFalse(payload["session_only"])
        self.assertEqual(payload["source_type"], "codex")
        self.assertEqual(int(payload.get("quota") or 0), 30)
        import time as _time
        for _ in range(50):
            if fake_svc.fetch_remote_info.called:
                break
            _time.sleep(0.02)
        fake_svc.fetch_remote_info.assert_called_once()
        self.assertEqual(fake_svc.fetch_remote_info.call_args[0][0], "at-codex-rotated")


class CircuitBreakerTest(unittest.TestCase):
    def test_network_error_classifier(self):
        self.assertTrue(_is_network_register_error("curl: (35) TLS connect error"))
        self.assertTrue(_is_network_register_error("开始 OAuth 流程失败"))
        self.assertTrue(_is_network_register_error("authorize 409 invalid_state"))
        self.assertTrue(_is_network_register_error("oai_did_missing"))
        self.assertTrue(_is_network_register_error("curl: (52) Empty reply from server"))
        self.assertFalse(_is_network_register_error("注册密码失败"))
        self.assertFalse(_is_network_register_error("验证验证码失败"))
        self.assertFalse(_is_network_register_error("wrong_email_otp_code"))

    def test_threshold_env(self):
        with mock.patch.dict(os.environ, {"GPT_REGISTER_CIRCUIT_BREAK": "5"}):
            self.assertEqual(_circuit_break_threshold({}), 5)
        self.assertEqual(_circuit_break_threshold({"circuit_break": "0"}), 0)


class ProxyPoolTest(unittest.TestCase):
    def test_parse_and_round_robin(self):
        pool = parse_proxy_pool(
            "socks5h://a:1\n# skip\nsocks5h://b:2, socks5h://a:1\n"
        )
        self.assertEqual(pool, ["socks5h://a:1", "socks5h://b:2"])
        self.assertEqual(pick_proxy(pool, 1), "socks5h://a:1")
        self.assertEqual(pick_proxy(pool, 2), "socks5h://b:2")
        self.assertEqual(pick_proxy(pool, 3), "socks5h://a:1")
        self.assertEqual(pick_proxy([], 1), "")
        self.assertEqual(
            _mask_proxy_url("socks5h://user:secret@host:1080"),
            "socks5h://user:***@host:1080",
        )

    def test_concurrency_should_not_exceed_pool(self):
        pool = parse_proxy_pool("socks5h://a:1\nsocks5h://b:2")
        self.assertEqual(len(pool), 2)
        self.assertEqual(min(5, len(pool)), 2)


class ReplenishPoolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self.tmp.name) / "gpt_reg.json"
        self.svc = GptRegisterService(config_store=GptRegisterConfig(path=self.cfg_path))
        self.svc.config_store.update(
            {
                "auto_replenish_enabled": True,
                "auto_replenish_min_available": 2,
                "auto_replenish_batch": 1,
                "push_enabled": True,
                "dry_run": False,
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_skips_when_disabled(self):
        self.svc.config_store.update({"auto_replenish_enabled": False})
        with mock.patch.object(self.svc, "start_job") as start:
            out = self.svc.maybe_replenish_pool()
        self.assertEqual(out["action"], "skip")
        self.assertEqual(out["reason"], "disabled")
        start.assert_not_called()

    def test_skips_when_stocked(self):
        with mock.patch("services.account_service.account_service") as acc:
            acc.count_image_available_accounts.return_value = 2
            with mock.patch.object(self.svc, "start_job") as start:
                out = self.svc.maybe_replenish_pool()
        self.assertEqual(out["action"], "skip")
        self.assertEqual(out["reason"], "stocked")
        start.assert_not_called()

    def test_skips_when_job_running(self):
        with mock.patch.object(self.svc, "has_active_job", return_value=True):
            with mock.patch.object(self.svc, "start_job") as start:
                out = self.svc.maybe_replenish_pool()
        self.assertEqual(out["reason"], "job_running")
        start.assert_not_called()

    def test_starts_when_below_min(self):
        # 不要读真实 data/gpt_register_jobs.json：那里若有刚跑完的自动补号任务，
        # 会命中 fail_cooldown 让本用例莫名 skip。
        with mock.patch("services.account_service.account_service") as acc:
            acc.count_image_available_accounts.return_value = 0
            with mock.patch.object(self.svc, "_last_finished_auto_job", return_value=None):
                with mock.patch.object(
                    self.svc, "start_job", return_value={"job_id": "auto1"}
                ) as start:
                    out = self.svc.maybe_replenish_pool()
        self.assertEqual(out["action"], "started")
        self.assertEqual(out["count"], 1)
        start.assert_called_once()
        self.assertEqual(start.call_args.args[0]["count"], 1)
        self.assertEqual(start.call_args.kwargs["trigger"], "auto_replenish")

    def test_fail_cooldown_after_empty_auto_job(self):
        from datetime import datetime, timezone

        self.svc._jobs["old"] = {
            "job_id": "old",
            "trigger": "auto_replenish",
            "status": "done",
            "added": 0,
            "failed": 1,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        with mock.patch("services.account_service.account_service") as acc:
            acc.count_image_available_accounts.return_value = 0
            with mock.patch.object(self.svc, "start_job") as start:
                out = self.svc.maybe_replenish_pool()
        self.assertEqual(out["reason"], "fail_cooldown")
        start.assert_not_called()
