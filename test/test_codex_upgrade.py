"""Codex protocol upgrade for session_only accounts (replace browser OAuth primary path)."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from services.gpt_register_service import (
    GptRegisterConfig,
    GptRegisterService,
    normalize_settings,
)


class AutoCodexUpgradeSettingsTest(unittest.TestCase):
    def test_auto_codex_upgrade_default_true(self):
        s = normalize_settings(None)
        self.assertTrue(s["auto_codex_upgrade"])
        s2 = normalize_settings({"auto_codex_upgrade": False})
        self.assertFalse(s2["auto_codex_upgrade"])

    def test_api_model_accepts_auto_codex_upgrade(self):
        from api.gpt_register import GptRegisterSettingsUpdate

        body = GptRegisterSettingsUpdate(auto_codex_upgrade=False, skip_codex=True)
        patch = body.model_dump(exclude_none=True)
        self.assertIn("auto_codex_upgrade", patch)
        self.assertFalse(patch["auto_codex_upgrade"])

    def test_update_persists_auto_codex_upgrade(self):
        path = Path("/tmp/nope-gpt-reg-auto-codex.json")
        if path.exists():
            path.unlink()
        cfg = GptRegisterConfig(path=path)
        updated = cfg.update({"auto_codex_upgrade": False})
        self.assertFalse(updated["auto_codex_upgrade"])
        reloaded = GptRegisterConfig(path=path).get()
        self.assertFalse(reloaded["auto_codex_upgrade"])
        try:
            path.unlink()
        except OSError:
            pass


class CodexUpgradeServiceTest(unittest.TestCase):
    def test_apply_tokens_replaces_old_session_row(self):
        from services.codex_upgrade_service import apply_codex_tokens_to_pool

        fake_svc = mock.Mock()
        fake_svc.add_account_items.return_value = {"added": 1, "items": [{"access_token": "new-at"}]}
        fake_svc.delete_accounts.return_value = {"removed": 1}
        fake_svc.fetch_remote_info.return_value = {"access_token": "new-at", "quota": 10}

        with mock.patch("services.account_service.account_service", fake_svc):
            out = apply_codex_tokens_to_pool(
                result={
                    "access_token": "new-at",
                    "refresh_token": "rt",
                    "id_token": "id",
                    "account_id": "acc1",
                    "email": "a@x.com",
                },
                replace_access_token="old-session",
                email="a@x.com",
            )
        self.assertTrue(out["ok"])
        self.assertEqual(out["replaced"], 1)
        payload = fake_svc.add_account_items.call_args[0][0][0]
        self.assertEqual(payload["access_token"], "new-at")
        self.assertEqual(payload["refresh_token"], "rt")
        self.assertFalse(payload["session_only"])
        self.assertFalse(payload["fragile"])
        self.assertEqual(payload["source_type"], "codex_upgrade")
        fake_svc.delete_accounts.assert_called_once_with(["old-session"])

    def test_upgrade_soft_fails_on_add_phone(self):
        from services import codex_upgrade_service as cus

        with mock.patch(
            "gpt_free_register.codex_upgrade.obtain_codex_tokens_for_email",
            return_value={
                "ok": False,
                "email": "a@x.com",
                "access_token": "",
                "refresh_token": "",
                "id_token": "",
                "account_id": "",
                "error": "add_phone required",
                "reason": "add_phone",
                "logs": ["add_phone"],
            },
        ), mock.patch.object(cus, "gpt_register_config") as cfg:
            cfg.get.return_value = normalize_settings(None)
            out = cus.upgrade_session_account_via_codex(
                email="a@x.com",
                replace_access_token="old",
            )
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "add_phone")
        self.assertEqual(out["replaced"], 0)

    def test_upgrade_success_writes_tokens(self):
        from services import codex_upgrade_service as cus

        with mock.patch(
            "gpt_free_register.codex_upgrade.obtain_codex_tokens_for_email",
            return_value={
                "ok": True,
                "email": "a@x.com",
                "access_token": "new-at",
                "refresh_token": "rt",
                "id_token": "id",
                "account_id": "acc",
                "error": None,
                "reason": None,
                "logs": ["ok"],
            },
        ), mock.patch.object(
            cus,
            "apply_codex_tokens_to_pool",
            return_value={
                "ok": True,
                "added": 1,
                "replaced": 1,
                "access_token": "new-at",
                "email": "a@x.com",
                "items": [],
                "error": None,
            },
        ) as apply, mock.patch.object(cus, "gpt_register_config") as cfg:
            cfg.get.return_value = normalize_settings(None)
            out = cus.upgrade_session_account_via_codex(
                email="a@x.com",
                replace_access_token="old",
            )
        self.assertTrue(out["ok"])
        self.assertEqual(out["replaced"], 1)
        apply.assert_called_once()

    def test_codex_upgrade_request_model(self):
        from api.accounts import CodexUpgradeRequest

        body = CodexUpgradeRequest(email="a@x.com", access_token="tok")
        self.assertEqual(body.email, "a@x.com")
        self.assertEqual(body.access_token, "tok")


class ImportLocalSchedulesCodexUpgradeTest(unittest.TestCase):
    def test_import_local_schedules_auto_upgrade_for_session_only(self):
        svc = GptRegisterService(
            config_store=GptRegisterConfig(path=Path("/tmp/nope-gpt-reg-import-auto.json"))
        )
        settings = normalize_settings(
            {"plan_type": "free", "bind_register_proxy": False, "auto_codex_upgrade": True}
        )
        account = {
            "email": "s@x.com",
            "password": "",
            "token": "access-only",
            "user_id": "u1",
            "extra": {"access_token": "access-only"},
        }
        fake_svc = mock.Mock()
        fake_svc.add_account_items.return_value = {"added": 1, "skipped": 0, "items": []}
        fake_svc.list_accounts.return_value = []
        fake_svc.fetch_remote_info.return_value = {"access_token": "access-only"}

        with mock.patch("services.account_service.account_service", fake_svc), mock.patch(
            "services.codex_upgrade_service.schedule_codex_upgrade"
        ) as sched:
            added = svc._import_local(account, settings)
        self.assertEqual(added, 1)
        sched.assert_called_once()
        kwargs = sched.call_args.kwargs
        self.assertEqual(kwargs["email"], "s@x.com")
        self.assertEqual(kwargs["replace_access_token"], "access-only")

    def test_import_local_skips_auto_upgrade_when_disabled(self):
        svc = GptRegisterService(
            config_store=GptRegisterConfig(path=Path("/tmp/nope-gpt-reg-import-noauto.json"))
        )
        settings = normalize_settings(
            {"plan_type": "free", "auto_codex_upgrade": False}
        )
        account = {
            "email": "s@x.com",
            "token": "access-only",
            "extra": {"access_token": "access-only"},
        }
        fake_svc = mock.Mock()
        fake_svc.add_account_items.return_value = {"added": 1}
        fake_svc.list_accounts.return_value = []
        fake_svc.fetch_remote_info.return_value = {}

        with mock.patch("services.account_service.account_service", fake_svc), mock.patch(
            "services.codex_upgrade_service.schedule_codex_upgrade"
        ) as sched:
            svc._import_local(account, settings)
        sched.assert_not_called()

    def test_import_local_skips_auto_upgrade_when_has_refresh(self):
        svc = GptRegisterService(
            config_store=GptRegisterConfig(path=Path("/tmp/nope-gpt-reg-import-rt.json"))
        )
        settings = normalize_settings({"plan_type": "free", "auto_codex_upgrade": True})
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
        fake_svc.list_accounts.return_value = []
        fake_svc.fetch_remote_info.return_value = {}

        with mock.patch("services.account_service.account_service", fake_svc), mock.patch(
            "services.codex_upgrade_service.schedule_codex_upgrade"
        ) as sched:
            svc._import_local(account, settings)
        sched.assert_not_called()


if __name__ == "__main__":
    unittest.main()
