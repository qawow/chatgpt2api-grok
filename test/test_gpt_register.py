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
    _extract_json_object,
    normalize_settings,
    public_settings,
)


class NormalizeSettingsTest(unittest.TestCase):
    def test_defaults_and_clamps(self):
        s = normalize_settings({"count": 999, "concurrency": 0, "executor": "weird"})
        self.assertEqual(s["count"], 50)
        self.assertEqual(s["concurrency"], 1)
        self.assertEqual(s["executor"], "protocol")
        self.assertTrue(s["push_enabled"])

    def test_public_hides_auth_key(self):
        raw = normalize_settings({"chatgpt2api_auth_key": "secret-key"})
        pub = public_settings(raw)
        self.assertEqual(pub["chatgpt2api_auth_key"], "")
        self.assertTrue(pub["has_chatgpt2api_auth_key"])


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
                import sys

                for k in list(sys.modules):
                    if (
                        k == "core"
                        or k.startswith("core.")
                        or k.startswith("platforms")
                        or k.startswith("providers")
                        or k.startswith("infrastructure")
                    ):
                        del sys.modules[k]
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
        fake_svc.fetch_remote_info.assert_called_once()
        self.assertEqual(fake_svc.fetch_remote_info.call_args[0][0], "access-only")

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
        fake_svc.fetch_remote_info.return_value = {"access_token": "at-codex", "quota": 2}
        with mock.patch("services.account_service.account_service", fake_svc):
            added = svc._import_local(account, settings)
        self.assertEqual(added, 1)
        payload = fake_svc.add_account_items.call_args[0][0][0]
        self.assertFalse(payload["session_only"])
        self.assertEqual(payload["source_type"], "codex")
        fake_svc.fetch_remote_info.assert_called_once()
