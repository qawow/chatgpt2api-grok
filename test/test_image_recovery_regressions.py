from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from services.account_service import AccountService
from services.config import config
from services.image_task_control import ImageTaskControl
from services.image_task_service import ImageTaskService
from services.openai_backend_api import ImagePollTimeoutError, OpenAIBackendAPI
from services.storage.json_storage import JSONStorageBackend
from test.test_image_task_service import OWNER, wait_for_task
from utils.atomic import atomic_write_json


class ImageRecoveryRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.received = []

        def handler(payload):
            self.received.append(payload)
            return {"data": [{"url": "http://example.test/result.png"}]}

        self.service = ImageTaskService(self.root / "tasks.json", generation_handler=handler, edit_handler=handler,
                                        retention_days_getter=lambda: 30)
        self.service._log_call = Mock()

    def task(self, **extra):
        task = {
            "id": "task", "owner_id": OWNER["id"], "status": "error", "attempt": 0,
            "mode": "generate", "model": "gpt-image-2", "account_email": "a@example.test",
            "error": "upstream connection timed out", "updated_at": "2099-01-01 00:00:00",
            "payload": {"prompt": "cat", "model": "gpt-image-2"}, **extra,
        }
        self.service._tasks[f'{OWNER["id"]}:task'] = task
        return task

    def test_transient_failure_expires_but_revocation_does_not(self):
        fingerprint = hashlib.sha256(b"token").hexdigest()
        for kind, retry_at, excluded in (
            ("transient", time.time() + 60, ["token"]),
            ("transient", time.time() - 60, []),
            ("revoked", time.time() - 60, ["token"]),
        ):
            with self.subTest(kind=kind, excluded=excluded):
                self.task(account_token_hash=fingerprint, account_failures={fingerprint: {"kind": kind, "retry_at": retry_at}})
                with patch("services.account_service.account_service.list_accounts", return_value=[{"access_token": "token", "status": "正常", "quota": 5}]):
                    self.service.resume_poll(OWNER, "task")
                    wait_for_task(self.service, OWNER, "task", "success")
                self.assertEqual(self.received[-1]["_excluded_tokens"], excluded)

    def test_legacy_exclusions_age_out_instead_of_being_reset_on_resume(self):
        self.task(account_token_hash=hashlib.sha256(b"token").hexdigest(), last_failure_at=time.time() - 120)
        with patch("services.account_service.account_service") as accounts:
            accounts.list_accounts.return_value = [{"access_token": "token"}]
            accounts.find_access_token_by_email.return_value = "token"
            self.service.resume_poll(OWNER, "task")
            wait_for_task(self.service, OWNER, "task", "success")
        self.assertEqual(self.received[-1]["_excluded_tokens"], [])

    def test_resume_success_settles_original_generation_without_releasing_slot(self):
        self.task(conversation_id="conv", generation_id="generation-1")
        with patch("services.account_service.account_service") as accounts, patch(
            "services.openai_backend_api.OpenAIBackendAPI"
        ) as backend, patch("services.proxy_service.proxy_settings.list_egress_candidates", return_value=[("direct", "")]), patch(
            "services.protocol.conversation.format_image_result", return_value={"data": [{"url": "ok"}]}
        ):
            accounts.find_access_token_by_email.return_value = "token"
            accounts._token_looks_revoked.return_value = False
            backend.return_value._poll_image_results.return_value = (["file"], [])
            backend.return_value.resolve_conversation_image_urls.return_value = ["http://example.test/file"]
            backend.return_value.download_image_bytes.return_value = [b"image"]
            self.service.resume_poll(OWNER, "task")
            wait_for_task(self.service, OWNER, "task", "success")
            accounts.mark_image_result.assert_called_once_with("token", True, release_slot=False, result_id="generation-1")

    def test_hung_resume_times_out_and_late_poll_cannot_download_or_settle(self):
        self.task(conversation_id="conv")
        release, closed = threading.Event(), threading.Event()

        def poll(*args):
            release.wait(3)
            return ["file"], []

        with patch.dict(config.data, {"image_task_timeout_secs": 1}), patch("services.account_service.account_service") as accounts, patch(
            "services.openai_backend_api.OpenAIBackendAPI"
        ) as backend, patch("services.proxy_service.proxy_settings.list_egress_candidates", return_value=[("direct", "")]):
            accounts.find_access_token_by_email.return_value = "token"
            accounts._token_looks_revoked.return_value = False
            backend.return_value._poll_image_results.side_effect = poll
            backend.return_value.close.side_effect = closed.set
            try:
                self.service.resume_poll(OWNER, "task", 30)
                task = wait_for_task(self.service, OWNER, "task", "error", timeout=2)
                self.assertIn("超时", task["error"])
            finally:
                release.set()
                self.assertTrue(closed.wait(2))
            backend.return_value.download_image_bytes.assert_not_called()
            accounts.mark_image_result.assert_not_called()
            self.assertEqual(self.service.list_tasks(OWNER, ["task"])["items"][0]["status"], "error")

    def test_replay_inherits_remaining_budget(self):
        task = self.task(status="running", attempt=1, account_email="")
        control = ImageTaskControl(5)
        self.service._replay_task(f'{OWNER["id"]}:task', OWNER, "generate", "gpt-image-2", 1, control)
        self.assertIs(self.received[-1]["_task_control"], control)
        self.assertEqual(task["status"], "success")

    def test_inputs_stored_separately_and_progress_writes_throttled(self):
        original = b"large-reference" * 10000
        saved = self.service.inputs.encode("owner:task", {"prompt": "cat", "images": [(original, "ref.png", "image/png")]})
        task = self.task(status="running", payload=saved)
        key = f'{OWNER["id"]}:task'
        self.service._save_locked()
        self.assertLess(self.service.path.stat().st_size, 2000)
        self.assertEqual(self.service.inputs.decode(saved)["images"][0][0], original)
        with patch.object(self.service, "_save_locked", wraps=self.service._save_locked) as save, patch(
            "services.image_task_service.time.monotonic", return_value=self.service._last_progress_save + .1
        ):
            for step in range(20):
                self.service._update_task(key, expected_attempt=0, progress=f"step-{step}")
            save.assert_not_called()
            self.service._update_task(key, expected_attempt=0, conversation_id="conv")
            self.assertEqual(save.call_count, 1)
            self.service._update_task(key, expected_attempt=0, status="success")
            self.assertEqual(save.call_count, 2)
        self.assertEqual(task["progress"], "step-19")

    def test_legacy_inputs_migrate_and_survive_reload(self):
        raw = b"legacy-reference"
        task = self.task(payload={"prompt": "cat", "images": [[base64.b64encode(raw).decode(), "ref.png", "image/png"]]})
        atomic_write_json(self.service.path, {"tasks": [task]})
        migrated = ImageTaskService(self.service.path)
        payload = migrated._tasks[f'{OWNER["id"]}:task']["payload"]
        self.assertIsInstance(payload["images"][0], dict)
        self.assertNotIn(base64.b64encode(raw).decode(), self.service.path.read_text())
        reloaded = ImageTaskService(self.service.path)
        self.assertEqual(reloaded.inputs.decode(payload)["images"], [(raw, "ref.png", "image/png")])

    def test_migration_disk_failure_preserves_inline_input(self):
        task = self.task(payload={"images": [["aW1hZ2U=", "ref.png", "image/png"]]})
        atomic_write_json(self.service.path, {"tasks": [task]})
        with patch("services.image_task_inputs.atomic_write_bytes", side_effect=OSError("disk full")):
            migrated = ImageTaskService(self.service.path)
        self.assertEqual(migrated._tasks[f'{OWNER["id"]}:task']["payload"], task["payload"])
        self.assertIn("aW1hZ2U=", self.service.path.read_text())

    def test_expired_blobs_deleted_only_after_metadata_commits(self):
        saved = self.service.inputs.encode("owner:task", {"images": [(b"ref", "ref.png", "image/png")]})
        self.task(updated_at="2000-01-01 00:00:00", payload=saved)
        blob = self.service.inputs.directory / saved["images"][0]["blob"]
        self.service._save_locked()
        with self.service._lock:
            self.assertTrue(self.service._cleanup_locked())
            with patch("services.image_task_service.atomic_write_json", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    self.service._save_locked()
            self.assertTrue(blob.exists())
            self.service._save_locked()
        self.assertFalse(blob.exists())

    def test_cleanup_does_not_delete_blob_reused_by_live_task(self):
        saved = self.service.inputs.encode("owner:task", {"images": [(b"ref", "ref.png", "image/png")]})
        self.task(updated_at="2000-01-01 00:00:00", payload=saved)
        self.service._cleanup_locked()
        self.task(status="running", payload=saved)
        self.service._save_locked()
        self.assertEqual(self.service.inputs.decode(saved)["images"][0][0], b"ref")

    def test_input_reference_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            self.service.inputs.decode({"images": [{"blob": "../secret", "filename": "x", "mime": "image/png"}]})

    def test_backup_includes_input_blobs_without_generated_images(self):
        from services.backup_service import BackupService

        directory = self.root / "image_tasks_inputs"
        directory.mkdir()
        from utils.atomic import atomic_write_bytes
        atomic_write_bytes(directory / "example.bin", b"reference")
        with patch("services.backup_service.DATA_DIR", self.root):
            archive = BackupService()._build_backup_archive({"include": {"image_tasks": True}}, trigger="test")
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            self.assertEqual(tar.extractfile("data/image_tasks_inputs/example.bin").read(), b"reference")


class ImageResultSettlementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = JSONStorageBackend(Path(self.temp.name) / "accounts.json")
        self.service = AccountService(self.storage)
        self.service.add_account_items([{"access_token": "token", "status": "正常", "quota": 5}])

    def test_concurrent_and_reloaded_settlement_charges_once(self):
        self.service._image_inflight["token"] = 2
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(lambda _: self.service.mark_image_result("token", True, release_slot=False, result_id="one"), range(8)))
        self.assertEqual(self.service.get_account("token")["quota"], 4)
        self.assertEqual(self.service.get_account("token")["success"], 1)
        self.assertEqual(self.service._image_inflight["token"], 2)
        reloaded = AccountService(self.storage)
        reloaded.mark_image_result("token", True, release_slot=False, result_id="one")
        self.assertEqual(reloaded.get_account("token")["quota"], 4)

    def test_failed_persistence_rolls_back_quota_and_id(self):
        with patch.object(self.storage, "save_accounts", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.service.mark_image_result("token", True, release_slot=False, result_id="one")
        self.assertEqual(self.service.get_account("token")["quota"], 5)
        self.assertNotIn("one", self.service.get_account("token").get("image_result_ids", {}))
        self.service.mark_image_result("token", True, release_slot=False, result_id="one")
        self.assertEqual(self.service.get_account("token")["quota"], 4)

    def test_auto_remove_save_failure_restores_account_and_reservations(self):
        self.service.update_account("token", {"quota": 1})
        self.service._image_inflight["token"] = 1
        self.service._token_aliases["old"] = "token"
        with patch.dict(config.data, {"auto_remove_rate_limited_accounts": True}), patch.object(
            self.storage, "save_accounts", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                self.service.mark_image_result("token", True, release_slot=False, result_id="one")
        self.assertEqual(self.service.get_account("token")["quota"], 1)
        self.assertEqual(self.service._image_inflight["token"], 1)
        self.assertEqual(self.service._token_aliases["old"], "token")


class ImageDeadlineTests(unittest.TestCase):
    def test_network_timeout_uses_smallest_remaining_budget(self):
        backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        backend.base_url = "https://example.invalid"
        backend._headers = Mock(return_value={})
        backend.session = Mock()
        backend.session.get.return_value.status_code = 200
        backend.task_control = ImageTaskControl(2)
        backend._poll_deadline = time.monotonic() + .1
        backend._get_conversation("conv")
        self.assertGreater(backend.session.get.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(backend.session.get.call_args.kwargs["timeout"], .1)
        backend.task_control.cancelled.set()
        with self.assertRaises(TimeoutError):
            backend._get_conversation("conv")
        backend.session.get.assert_called_once()

    def test_poll_deadline_is_restored_and_timeout_keeps_conversation_id(self):
        backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        backend._poll_image_results_impl = Mock(side_effect=ImagePollTimeoutError("timeout"))
        with self.assertRaises(ImagePollTimeoutError) as caught:
            backend._poll_image_results("conv", 5)
        self.assertEqual(caught.exception.conversation_id, "conv")
        self.assertIsNone(backend._poll_deadline)


if __name__ == "__main__":
    unittest.main()
