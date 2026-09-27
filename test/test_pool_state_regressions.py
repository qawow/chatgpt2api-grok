"""号池 / 补号 / 请求边界的状态回归测试。

这些行为此前都无覆盖，且失效时都是静默的：账号自己回池、补号永久停摆、
配置字段被 FastAPI 丢弃、内部编排钩子被客户端注入。
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from services.account_service import AccountService
from services.protocol import openai_v1_image_generations
from services.storage.json_storage import JSONStorageBackend

TOKEN = "sk-test-account-token"


def _service(tmp_dir: str) -> AccountService:
    service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
    service.add_account_items([{"access_token": TOKEN, "quota": 10, "status": "正常"}])
    return service


class DisabledAccountTests(unittest.TestCase):
    """禁用是运维决定，刷新/探活链路不得把它洗回正常。"""

    def test_refresh_style_update_cannot_clear_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir)
            service.update_account(TOKEN, {"status": "禁用"}, allow_status_override=True)

            # fetch_remote_info 拿到 quota>0 时会写 status=正常，走的就是这条路径
            updated = service.update_account(TOKEN, {"status": "正常", "quota": 25}, quiet=True)

            self.assertEqual(updated["status"], "禁用")
            self.assertEqual(updated["quota"], 25, "非 status 字段仍应正常写入")

    def test_admin_endpoint_can_clear_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir)
            service.update_account(TOKEN, {"status": "禁用"}, allow_status_override=True)

            updated = service.update_account(TOKEN, {"status": "正常"}, allow_status_override=True)

            self.assertEqual(updated["status"], "正常")

    def test_disabled_can_still_be_set_automatically(self) -> None:
        """守卫只挡「离开禁用」，不挡「进入禁用」。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir)
            updated = service.update_account(TOKEN, {"status": "禁用", "quota": 0}, quiet=True)
            self.assertEqual(updated["status"], "禁用")


class RegisterJobTerminalStateTests(unittest.TestCase):
    """_run_job 收尾阶段抛异常曾让 job 永远停在 running，补号随之永久关闭。"""

    def _service(self):
        from services.gpt_register_service import GptRegisterService

        service = GptRegisterService.__new__(GptRegisterService)
        service._lock = threading.RLock()
        service._jobs = {}
        service._cancel_flags = {}
        return service

    def test_bookkeeping_failure_still_reaches_terminal_status(self) -> None:
        service = self._service()
        service._jobs["j1"] = {"job_id": "j1", "status": "running", "created_at": "2026-01-01T00:00:00+00:00"}

        def _boom(job_id, settings):
            raise RuntimeError("atomic_write_json failed")

        service._run_job_impl = _boom
        service._save_jobs = lambda: None

        with self.assertRaises(RuntimeError):
            service._run_job("j1", {})

        self.assertEqual(service._jobs["j1"]["status"], "failed")
        self.assertFalse(service.has_active_job(), "job 已终态，补号必须能重新开工")

    def test_successful_run_keeps_its_own_status(self) -> None:
        service = self._service()
        service._jobs["j1"] = {"job_id": "j1", "status": "running", "created_at": "2026-01-01T00:00:00+00:00"}
        service._save_jobs = lambda: None
        service._run_job_impl = lambda job_id, settings: service._jobs.__setitem__(
            "j1", {**service._jobs["j1"], "status": "done"}
        )

        service._run_job("j1", {})

        self.assertEqual(service._jobs["j1"]["status"], "done")

    def test_silent_running_job_eventually_stops_blocking_replenish(self) -> None:
        service = self._service()
        service._jobs["j1"] = {
            "job_id": "j1",
            "status": "running",
            "updated_at": "2020-01-01T00:00:00+00:00",
        }
        self.assertFalse(service.has_active_job())

    def test_recent_running_job_still_blocks_replenish(self) -> None:
        from services.gpt_register_service import _now_iso

        service = self._service()
        service._jobs["j1"] = {"job_id": "j1", "status": "running", "updated_at": _now_iso()}
        self.assertTrue(service.has_active_job())


class RegisterSettingsSchemaTests(unittest.TestCase):
    def test_model_declares_every_persisted_setting(self) -> None:
        """Pydantic 未声明的键会被 FastAPI 静默丢弃 —— 前端填了也存不进去。"""
        from api.gpt_register import GptRegisterSettingsUpdate
        from services.gpt_register_service import DEFAULT_SETTINGS

        missing = sorted(set(DEFAULT_SETTINGS) - set(GptRegisterSettingsUpdate.model_fields))
        self.assertEqual(missing, [], f"这些设置项会被静默丢弃: {missing}")

    def test_min_total_quota_round_trips_through_the_model(self) -> None:
        from api.gpt_register import GptRegisterSettingsUpdate

        body = GptRegisterSettingsUpdate(auto_replenish_min_total_quota=500)
        self.assertEqual(body.model_dump(exclude_none=True)["auto_replenish_min_total_quota"], 500)


class InternalPayloadKeyTests(unittest.TestCase):
    """/v1/images/generations 是 extra="allow"，编排钩子必须在入口剥离。"""

    def test_strip_internal_keys_removes_orchestration_hooks(self) -> None:
        body = {
            "prompt": "a cat",
            "progress_callback": "not-a-callable",
            "checkpoint_callback": "x",
            "_excluded_tokens": ["sk-someone-elses-token"],
            "_is_cancelled": True,
            "_task_control": object(),
        }
        cleaned = openai_v1_image_generations.strip_internal_keys(body)
        self.assertEqual(cleaned, {"prompt": "a cat"})

    def test_strip_internal_keys_preserves_real_fields(self) -> None:
        body = {"prompt": "a cat", "model": "gpt-image-2.5", "n": 2, "size": "1024x1024"}
        self.assertEqual(openai_v1_image_generations.strip_internal_keys(body), body)

    def test_internal_keys_cover_everything_the_handler_reads(self) -> None:
        import inspect

        source = inspect.getsource(openai_v1_image_generations.handle)
        for key in openai_v1_image_generations.INTERNAL_PAYLOAD_KEYS:
            self.assertIn(key, source, f"{key} 不在 handle() 里，清单可能已过时")


if __name__ == "__main__":
    unittest.main()
