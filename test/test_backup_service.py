"""备份范围与失败重试的回归测试。

1. docs/operations.md「至少备份」列出的 Grok 号池、注册机密钥与表单，此前不在备份包里；
2. 定时备份失败后与成功一样要等满整个周期（默认 6 小时）才重试。
"""
from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from services import backup_service as mod
from services import config as config_module
from services.backup_service import BackupService
from services.config import _normalize_backup_include


class BackupScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / "gpt_register.env").write_text("CFD1_API_TOKEN=x\n", encoding="utf-8")
        (self.root / "gpt_register_config.json").write_text('{"mail_provider": "tempmail"}', encoding="utf-8")
        (self.root / "grok_accounts.json").write_text('[{"token": "g"}]', encoding="utf-8")

    def _archived(self, include: dict[str, bool]) -> set[str]:
        with mock.patch.object(mod, "DATA_DIR", self.root), mock.patch.object(config_module, "DATA_DIR", self.root):
            archive = BackupService()._build_backup_archive({"include": include}, trigger="test")
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            return set(tar.getnames())

    def test_documented_must_backup_files_are_archived_by_default(self) -> None:
        names = self._archived(_normalize_backup_include({}))
        self.assertIn("data/grok_accounts.json", names)
        self.assertIn("data/gpt_register.env", names)
        self.assertIn("data/gpt_register_config.json", names)

    def test_each_group_can_be_switched_off(self) -> None:
        include = _normalize_backup_include({"grok_accounts": False, "register": False})
        names = self._archived(include)
        self.assertNotIn("data/grok_accounts.json", names)
        self.assertNotIn("data/gpt_register.env", names)
        self.assertNotIn("data/gpt_register_config.json", names)


class BackupRetryTests(unittest.TestCase):
    def _runs(self, *, status: str, failures: int, minutes_ago: float, interval: int = 360) -> bool:
        service = BackupService()
        finished = (mod._utc_now() - timedelta(minutes=minutes_ago)).replace(microsecond=0)
        state = {
            "last_status": status,
            "consecutive_failures": failures,
            "last_finished_at": finished.isoformat().replace("+00:00", "Z"),
            "running": False,
        }
        with (
            mock.patch.object(mod.config, "get_backup_settings", return_value={"enabled": True, "interval_minutes": interval}),
            mock.patch.object(service, "get_status", return_value=state),
            mock.patch.object(service, "run_backup") as run_backup,
        ):
            service.run_scheduled_backup_if_needed()
        return run_backup.called

    def test_failed_run_retries_after_backoff_not_the_full_interval(self) -> None:
        self.assertFalse(self._runs(status="error", failures=1, minutes_ago=4))
        self.assertTrue(self._runs(status="error", failures=1, minutes_ago=6))
        # 5 → 10 → 20 分钟，按连续失败次数翻倍
        self.assertFalse(self._runs(status="error", failures=3, minutes_ago=19))
        self.assertTrue(self._runs(status="error", failures=3, minutes_ago=21))

    def test_backoff_never_exceeds_the_configured_interval(self) -> None:
        self.assertFalse(self._runs(status="error", failures=40, minutes_ago=59, interval=60))
        self.assertTrue(self._runs(status="error", failures=40, minutes_ago=61, interval=60))

    def test_successful_run_still_waits_the_full_interval(self) -> None:
        self.assertFalse(self._runs(status="success", failures=0, minutes_ago=300))
        self.assertTrue(self._runs(status="success", failures=0, minutes_ago=361))

    def test_failure_counter_climbs_and_resets_on_success(self) -> None:
        service = BackupService()
        stored: dict[str, object] = {"consecutive_failures": 0}

        def save(state: dict[str, object]) -> dict[str, object]:
            stored.update(state)
            return dict(stored)

        outcomes = [RuntimeError("r2 down"), RuntimeError("r2 down"), {"key": "backups/b.tar.gz"}]
        with (
            mock.patch.object(mod, "save_backup_state", side_effect=save),
            mock.patch.object(mod, "load_backup_state", side_effect=lambda: dict(stored)),
            mock.patch.object(service, "_run_backup_once", side_effect=outcomes),
        ):
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    service.run_backup(trigger="schedule")
            self.assertEqual(stored["consecutive_failures"], 2)
            service.run_backup(trigger="schedule")
        self.assertEqual(stored["consecutive_failures"], 0)
        self.assertEqual(stored["last_status"], "success")


if __name__ == "__main__":
    unittest.main()
