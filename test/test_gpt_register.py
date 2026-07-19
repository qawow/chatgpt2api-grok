"""GPT free batch register settings + job helpers."""
from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
