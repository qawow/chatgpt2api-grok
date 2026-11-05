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
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from curl_cffi import requests

from services.account_service import AccountService
from services.config import config
from services.image_task_control import ImageTaskControl
from services.image_task_service import ImageTaskService
from services.openai_backend_api import ImagePollTimeoutError, OpenAIBackendAPI, ToolCooldownError, ToolQuotaError
from services.protocol import conversation
from services.protocol.conversation import ConversationRequest, ImageGenerationError
from services.storage.json_storage import JSONStorageBackend
from test.test_image_task_service import OWNER, wait_for_task
from utils.atomic import atomic_write_json
from utils.helper import SseProtocolError, SseStreamTimeoutError, UpstreamHTTPError
from utils.image_models import WEB_IMAGE_MODEL


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


RECOVERY_CID = "unit-recovery-conversation"
RECOVERY_FILE_ID = "file_00000000aaaabbbbccccddddeeeeffff"
RECOVERY_URL = "https://example.invalid/unit-image"


def _recovery_tool_frame(*, with_cid: bool = True) -> dict:
    frame = {
        "message": {
            "id": "unit-image-tool",
            "author": {"role": "tool"},
            "metadata": {"async_task_type": "image_gen"},
            "content": {"content_type": "multimodal_text", "parts": [{
                "content_type": "image_asset_pointer",
                "asset_pointer": f"file-service://{RECOVERY_FILE_ID}",
            }]},
        },
    }
    if with_cid:
        frame["conversation_id"] = RECOVERY_CID
    return frame


def _recovery_text_frame(*, with_cid: bool = True) -> dict:
    frame = {
        "message": {
            "id": "unit-image-assistant",
            "author": {"role": "assistant"},
            "content": {"content_type": "text", "parts": [
                json.dumps({"size": "1920x1088", "n": 1}),
            ]},
        },
    }
    if with_cid:
        frame["conversation_id"] = RECOVERY_CID
    return frame


class _RecoverySource:
    """Only JSON payloads and mock reads; never initialize a real backend."""
    def __init__(self, frames=(), error=None):
        self.frames = list(frames)
        self.error = error
        self.on_tail = None
        self._image_conversation_id = ""
        self._image_has_output = False
        self.task_control = None
        self.stream_conversation = Mock(side_effect=self._iterate)
        self.resolve_conversation_image_urls = Mock(return_value=[RECOVERY_URL])
        self.download_image_bytes = Mock(return_value=[b"unit-image-bytes"])
        self.find_conversation_by_prompt = Mock(return_value="")
        self._poll_image_results = Mock(return_value=([RECOVERY_FILE_ID], []))
        self.close = Mock()

    def _iterate(self, **_kwargs):
        for frame in self.frames:
            yield json.dumps(frame) if isinstance(frame, dict) else frame
        if self.on_tail is not None:
            self.on_tail()
        if self.error is not None:
            raise self.error
        yield "[DONE]"


class WebImageRecoveryBusinessTests(unittest.TestCase):
    @contextmanager
    def offline(self, backend):
        """Isolate every persistence, account, proxy, timer and image-save hook."""
        with ExitStack() as stack:
            clock = [1000.0]
            sleep = Mock()
            fake_time = SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic, sleep=sleep)
            fake_config = SimpleNamespace(
                global_system_prompt="", image_poll_timeout_secs=1, image_poll_initial_wait_secs=0,
                image_account_failover_retries=3, image_poll_failover_retries=2,
                image_text_failover_retries=2, image_tool_quota_cooldown_secs=3600,
                image_tool_short_cooldown_secs=60,
            )
            stack.enter_context(patch.object(conversation, "time", fake_time))
            stack.enter_context(patch.object(conversation, "config", fake_config))
            stack.enter_context(patch.object(conversation, "logger"))
            accounts = stack.enter_context(patch.object(conversation, "account_service"))
            accounts.get_available_access_token.return_value = "a"
            accounts.get_account.return_value = {"email": "a@example.invalid"}
            accounts.refresh_access_token.return_value = None
            accounts.confirm_token_revoked.return_value = False
            factory = stack.enter_context(patch.object(conversation, "OpenAIBackendAPI", return_value=backend))
            proxy = stack.enter_context(patch.object(conversation, "proxy_settings"))
            proxy.list_egress_candidates.return_value = [("direct", "")]
            stack.enter_context(patch("services.proxy_service.mark_egress_unusable"))
            stack.enter_context(patch.object(conversation, "model_not_found_breaker_remaining", return_value=0))
            claim = stack.enter_context(patch.object(conversation, "claim_conversation", return_value=True))
            exclusions = {"owned-by-other"}
            stack.enter_context(patch.object(conversation, "claimed_conversation_ids", return_value=exclusions))
            tasks = stack.enter_context(patch.object(conversation, "_get_detailed_error_from_tasks", return_value=""))
            formatter = stack.enter_context(patch.object(conversation, "format_image_result", return_value={
                "data": [{"url": "http://example.test/result.png"}],
            }))
            remover = stack.enter_context(patch.object(conversation, "_remove_image_conversation_later"))
            yield SimpleNamespace(
                accounts=accounts, factory=factory, sleep=sleep, clock=clock, claim=claim,
                exclusions=exclusions, tasks=tasks, formatter=formatter, remover=remover,
            )

    @staticmethod
    def request(**kwargs):
        return ConversationRequest(model=WEB_IMAGE_MODEL, prompt="cat", **kwargs)

    def assert_one_generation(self, backend, harness):
        harness.factory.assert_called_once()
        backend.stream_conversation.assert_called_once()
        harness.accounts.get_available_access_token.assert_called_once()
        backend.close.assert_called_once()
        harness.accounts.refresh_access_token.assert_not_called()
        harness.accounts.remove_invalid_token.assert_not_called()

    @staticmethod
    def terminal_errors():
        errors = [("cancel", TimeoutError("unit task cancelled")),
                  ("protocol", SseProtocolError("unit malformed delta"))]
        errors.extend((f"http-{status}", UpstreamHTTPError("unit-image-read", status, {"detail": "unit terminal"}))
                      for status in (400, 401, 403, 422, 429))
        return errors

    def test_stream_tail_transport_with_output_ids_resolves_without_reposting(self):
        for with_cid in (True, False):
            with self.subTest(with_cid=with_cid):
                error = requests.exceptions.RequestException("unit connection reset")
                backend = _RecoverySource([_recovery_tool_frame(with_cid=with_cid)], error)
                with self.offline(backend) as harness:
                    outputs = list(conversation.stream_image_outputs(backend, self.request()))
                    self.assertEqual(outputs[-1].kind, "result")
                    self.assertEqual(outputs[-1].conversation_id, RECOVERY_CID if with_cid else "")
                    backend.stream_conversation.assert_called_once()
                    backend.resolve_conversation_image_urls.assert_called_once()
                    self.assertEqual(backend.resolve_conversation_image_urls.call_args.args[1], [RECOVERY_FILE_ID])
                    backend.download_image_bytes.assert_called_once_with([RECOVERY_URL])
                    backend.find_conversation_by_prompt.assert_not_called()
                    harness.factory.assert_not_called()

    def test_stream_tail_with_only_known_cid_still_resolves_same_conversation(self):
        backend = _RecoverySource([{"conversation_id": RECOVERY_CID}], SseStreamTimeoutError("unit idle timeout"))
        with self.offline(backend):
            outputs = list(conversation.stream_image_outputs(backend, self.request()))
            self.assertEqual(outputs[-1].conversation_id, RECOVERY_CID)
            self.assertEqual(backend.resolve_conversation_image_urls.call_args.args[0], RECOVERY_CID)
            backend.stream_conversation.assert_called_once()

    def test_stream_without_checkpoint_or_output_ids_does_not_recover(self):
        error = requests.exceptions.RequestException("unit connection reset")
        backend = _RecoverySource([{"type": "server_ste_metadata", "metadata": {"tool_invoked": True}}], error)
        with self.offline(backend):
            with self.assertRaises(type(error)) as caught:
                list(conversation.stream_image_outputs(backend, self.request()))
            self.assertIs(caught.exception, error)
            backend.resolve_conversation_image_urls.assert_not_called()
            backend.download_image_bytes.assert_not_called()
            backend.find_conversation_by_prompt.assert_not_called()
            self.assertFalse(backend._image_has_output)

    def test_terminal_stream_errors_are_not_interpreted_as_disconnects(self):
        errors = self.terminal_errors() + [("tool-quota", ToolQuotaError("unit tool quota"))]
        sdk_http = requests.exceptions.RequestException("unit SDK HTTP error")
        sdk_http.response = SimpleNamespace(status_code=401)
        errors.append(("sdk-http-401", sdk_http))
        for label, error in errors:
            with self.subTest(error=label):
                backend = _RecoverySource([_recovery_tool_frame()], error)
                with self.offline(backend):
                    with self.assertRaises(type(error)) as caught:
                        list(conversation.stream_image_outputs(backend, self.request()))
                    self.assertIs(caught.exception, error)
                    backend.resolve_conversation_image_urls.assert_not_called()
                    backend.download_image_bytes.assert_not_called()

    def test_cancelled_control_remaining_prevents_stream_recovery(self):
        control = ImageTaskControl(30)
        control.cancelled.set()
        backend = _RecoverySource([_recovery_tool_frame()], SseStreamTimeoutError("unit idle timeout"))
        with self.offline(backend):
            with self.assertRaises(TimeoutError) as caught:
                list(conversation.stream_image_outputs(backend, self.request(task_control=control)))
            self.assertNotIsInstance(caught.exception, SseStreamTimeoutError)
            backend.resolve_conversation_image_urls.assert_not_called()
            backend.download_image_bytes.assert_not_called()

    def test_input_reference_ids_are_not_accepted_as_output_checkpoint(self):
        frames = [
            {"type": "server_ste_metadata", "metadata": {"tool_invoked": True}},
            {"type": "input_message", "input_message": {
                "author": {"role": "user"},
                "content": {"content_type": "multimodal_text", "parts": [{
                    "asset_pointer": f"file-service://{RECOVERY_FILE_ID}",
                }]},
            }},
        ]
        error = requests.exceptions.RequestException("unit connection reset")
        backend = _RecoverySource(frames, error)
        with self.offline(backend):
            with self.assertRaises(type(error)) as caught:
                list(conversation.stream_image_outputs(backend, self.request()))
            self.assertIs(caught.exception, error)
            backend.resolve_conversation_image_urls.assert_not_called()
            self.assertFalse(backend._image_has_output)

    def test_pool_output_ids_without_cid_download_failure_never_reposts(self):
        for kind in ("file", "sediment"):
            for stream_disconnect in (False, True):
                with self.subTest(output=kind, stream_disconnect=stream_disconnect):
                    frame = _recovery_tool_frame(with_cid=False)
                    expected_file_ids = [RECOVERY_FILE_ID]
                    expected_sediment_ids = []
                    if kind == "sediment":
                        sediment_id = "unit-recovery-sediment"
                        frame["message"]["content"]["parts"][0]["asset_pointer"] = f"sediment://{sediment_id}"
                        expected_file_ids = []
                        expected_sediment_ids = [sediment_id]
                    stream_error = requests.exceptions.RequestException("unit stream connection reset") if stream_disconnect else None
                    backend = _RecoverySource([frame], stream_error)
                    self.assertFalse(backend._image_has_output)
                    download_error = requests.exceptions.RequestException("unit download connection reset")
                    backend.download_image_bytes.side_effect = download_error
                    with self.offline(backend) as harness:
                        with self.assertRaises(ImageGenerationError) as caught:
                            conversation._generate_single_image(self.request(), 1, 1)
                        self.assertEqual(caught.exception.code, "upstream_result_pending")
                        self.assertEqual(caught.exception.conversation_id, "")
                        self.assertIs(caught.exception.__cause__, download_error)
                        self.assertTrue(backend._image_has_output)
                        self.assertEqual(backend._image_conversation_id, "")
                        backend.resolve_conversation_image_urls.assert_called_once_with(
                            "", expected_file_ids, expected_sediment_ids, poll_timeout_secs=1,
                        )
                        backend.download_image_bytes.assert_called_once_with([RECOVERY_URL])
                        backend.find_conversation_by_prompt.assert_not_called()
                        harness.formatter.assert_not_called()
                        harness.remover.assert_not_called()
                        harness.sleep.assert_not_called()
                        self.assert_one_generation(backend, harness)

    def test_pool_output_ids_without_cid_quota_or_cooldown_never_fails_over(self):
        for error_type in (ToolQuotaError, ToolCooldownError):
            with self.subTest(error=error_type.__name__):
                error = error_type("unit image tool pacing verdict")
                backend = _RecoverySource([_recovery_tool_frame(with_cid=False)], error)
                self.assertFalse(backend._image_has_output)
                with self.offline(backend) as harness:
                    with self.assertRaises(error_type) as caught:
                        conversation._generate_single_image(self.request(), 1, 1)
                    self.assertIs(caught.exception, error)
                    self.assertTrue(backend._image_has_output)
                    self.assertEqual(backend._image_conversation_id, "")
                    backend.resolve_conversation_image_urls.assert_not_called()
                    backend.download_image_bytes.assert_not_called()
                    backend.find_conversation_by_prompt.assert_not_called()
                    harness.sleep.assert_not_called()
                    self.assert_one_generation(backend, harness)

    def test_pool_recovered_cid_download_failure_is_pending_not_new_generation(self):
        error = requests.exceptions.RequestException("unit connection reset")
        backend = _RecoverySource([_recovery_text_frame(with_cid=False)])
        backend.find_conversation_by_prompt.return_value = RECOVERY_CID
        backend.download_image_bytes.side_effect = error
        checkpoint = Mock()
        with self.offline(backend) as harness:
            backend.on_tail = lambda: harness.clock.__setitem__(0, 1900.0)
            with self.assertRaises(ImageGenerationError) as caught:
                conversation._generate_single_image(self.request(checkpoint_callback=checkpoint), 1, 1)
            self.assertEqual(caught.exception.code, "upstream_result_pending")
            self.assertEqual(caught.exception.conversation_id, RECOVERY_CID)
            self.assertIs(caught.exception.__cause__, error)
            self.assertEqual(backend._image_conversation_id, RECOVERY_CID)
            backend.find_conversation_by_prompt.assert_called_once_with(
                "cat", 1000.0, timeout_secs=5.0, exclude_ids=harness.exclusions,
            )
            harness.claim.assert_called_once_with(RECOVERY_CID)
            checkpoint.assert_any_call({"conversation_id": RECOVERY_CID})
            self.assert_one_generation(backend, harness)
            harness.formatter.assert_not_called()
            harness.remover.assert_not_called()

    def test_all_recovery_paths_reuse_start_time_exclusions_claim_and_checkpoint(self):
        for mode in ("early-text", "late-text", "empty-image-fallback"):
            with self.subTest(mode=mode):
                frames = [_recovery_text_frame(with_cid=False)] if mode != "empty-image-fallback" else []
                backend = _RecoverySource(frames)
                backend.find_conversation_by_prompt.side_effect = ["", RECOVERY_CID] if mode == "late-text" else [RECOVERY_CID]
                if mode != "early-text":
                    backend.resolve_conversation_image_urls.side_effect = [[], [RECOVERY_URL]]
                checkpoint = Mock()
                request = self.request(checkpoint_callback=checkpoint, images=["unit-reference"] if mode == "empty-image-fallback" else None)
                with self.offline(backend) as harness:
                    backend.on_tail = lambda: harness.clock.__setitem__(0, 1900.0)
                    outputs = conversation._generate_single_image(request, 1, 1)
                    self.assertEqual(outputs[-1].kind, "result")
                    self.assertEqual(outputs[-1].conversation_id, RECOVERY_CID)
                    for lookup in backend.find_conversation_by_prompt.call_args_list:
                        self.assertEqual(lookup.args, ("cat", 1000.0))
                        self.assertEqual(lookup.kwargs["exclude_ids"], harness.exclusions)
                    self.assertEqual(backend.find_conversation_by_prompt.call_count, 2 if mode == "late-text" else 1)
                    harness.claim.assert_called_once_with(RECOVERY_CID)
                    checkpoint.assert_any_call({"conversation_id": RECOVERY_CID})
                    self.assertEqual(backend._image_conversation_id, RECOVERY_CID)
                    self.assert_one_generation(backend, harness)

    def test_recovery_claim_collision_is_not_adopted(self):
        backend = _RecoverySource([_recovery_text_frame(with_cid=False)])
        backend.find_conversation_by_prompt.return_value = RECOVERY_CID
        backend.resolve_conversation_image_urls.return_value = []
        checkpoint = Mock()
        with self.offline(backend) as harness:
            harness.claim.return_value = False
            outputs = list(conversation.stream_image_outputs(backend, self.request(checkpoint_callback=checkpoint)))
            harness.claim.assert_any_call(RECOVERY_CID)
            self.assertEqual(backend._image_conversation_id, "")
            self.assertTrue(all(not output.conversation_id for output in outputs))
            checkpoint.assert_not_called()
            backend._poll_image_results.assert_not_called()
            backend.download_image_bytes.assert_not_called()
            self.assertTrue(all(call.args[0] == "" for call in backend.resolve_conversation_image_urls.call_args_list))

    def test_pool_late_poll_timeout_keeps_cid_and_does_not_return_text_or_repost(self):
        error = ImagePollTimeoutError("unit image poll timeout")
        backend = _RecoverySource([_recovery_text_frame()])
        backend.resolve_conversation_image_urls.return_value = []
        backend._poll_image_results.side_effect = error
        with self.offline(backend) as harness:
            with self.assertRaises(ImagePollTimeoutError) as caught:
                conversation._generate_single_image(self.request(message_as_error=True), 1, 1)
            self.assertIs(caught.exception, error)
            self.assertEqual(caught.exception.conversation_id, RECOVERY_CID)
            backend._poll_image_results.assert_called_once()
            harness.formatter.assert_not_called()
            harness.sleep.assert_not_called()
            self.assert_one_generation(backend, harness)

    def test_pool_late_poll_terminal_errors_remain_original_without_waiting(self):
        for label, error in self.terminal_errors():
            with self.subTest(error=label):
                backend = _RecoverySource([_recovery_text_frame()])
                backend.resolve_conversation_image_urls.return_value = []
                backend._poll_image_results.side_effect = error
                with self.offline(backend) as harness:
                    with self.assertRaises(type(error)) as caught:
                        conversation._generate_single_image(self.request(message_as_error=True), 1, 1)
                    self.assertIs(caught.exception, error)
                    backend._poll_image_results.assert_called_once()
                    harness.sleep.assert_not_called()
                    harness.formatter.assert_not_called()
                    self.assert_one_generation(backend, harness)

    def test_pool_first_resolver_terminal_errors_are_not_swallowed(self):
        for label, error in self.terminal_errors():
            with self.subTest(error=label):
                backend = _RecoverySource([_recovery_text_frame()])
                backend.resolve_conversation_image_urls.side_effect = error
                with self.offline(backend) as harness:
                    with self.assertRaises(type(error)) as caught:
                        conversation._generate_single_image(self.request(), 1, 1)
                    self.assertIs(caught.exception, error)
                    backend._poll_image_results.assert_not_called()
                    harness.sleep.assert_not_called()
                    self.assert_one_generation(backend, harness)

    def test_pool_callback_request_exception_is_not_resumable_pending(self):
        for callback_kind in ("checkpoint", "progress"):
            with self.subTest(callback=callback_kind):
                error = requests.exceptions.RequestException("unit callback failure")
                backend = _RecoverySource([_recovery_tool_frame()])

                def callback(value):
                    if value == {"conversation_id": RECOVERY_CID} or value == "image_stream_resolve_start":
                        raise error

                request = self.request(**{f"{callback_kind}_callback": Mock(side_effect=callback)})
                with self.offline(backend) as harness:
                    with self.assertRaises(type(error)) as caught:
                        conversation._generate_single_image(request, 1, 1)
                    self.assertIs(caught.exception, error)
                    self.assertTrue(getattr(error, "_image_callback_error", False))
                    backend.resolve_conversation_image_urls.assert_not_called()
                    backend.download_image_bytes.assert_not_called()
                    self.assert_one_generation(backend, harness)

    def test_pool_known_cid_terminal_stream_errors_are_not_pending(self):
        for label, error in self.terminal_errors():
            with self.subTest(error=label):
                backend = _RecoverySource([{"conversation_id": RECOVERY_CID}], error)
                with self.offline(backend) as harness:
                    with self.assertRaises(type(error)) as caught:
                        conversation._generate_single_image(self.request(), 1, 1)
                    self.assertIs(caught.exception, error)
                    backend.resolve_conversation_image_urls.assert_not_called()
                    self.assert_one_generation(backend, harness)

    def test_pool_transient_late_poll_uses_task_wait_and_same_generation(self):
        backend = _RecoverySource([_recovery_text_frame()])
        backend.resolve_conversation_image_urls.side_effect = [[], [RECOVERY_URL]]
        backend._poll_image_results.side_effect = [
            requests.exceptions.RequestException("unit connection reset"), ([RECOVERY_FILE_ID], []),
        ]
        control = Mock(spec=ImageTaskControl)
        control.remaining.return_value = 30.0
        with self.offline(backend) as harness:
            outputs = conversation._generate_single_image(self.request(task_control=control), 1, 1)
            self.assertEqual(outputs[-1].kind, "result")
            control.wait.assert_called_once_with(30)
            harness.sleep.assert_not_called()
            self.assertEqual(backend._poll_image_results.call_count, 2)
            self.assert_one_generation(backend, harness)

    def test_pool_exhausted_transient_late_poll_is_pending_not_text(self):
        error = requests.exceptions.RequestException("unit connection reset")
        backend = _RecoverySource([_recovery_text_frame()])
        backend.resolve_conversation_image_urls.return_value = []
        backend._poll_image_results.side_effect = error
        control = Mock(spec=ImageTaskControl)
        control.remaining.return_value = 30.0
        with self.offline(backend) as harness:
            with self.assertRaises(ImageGenerationError) as caught:
                conversation._generate_single_image(self.request(task_control=control, message_as_error=True), 1, 1)
            self.assertEqual(caught.exception.code, "upstream_result_pending")
            self.assertEqual(caught.exception.conversation_id, RECOVERY_CID)
            self.assertIs(caught.exception.__cause__, error)
            self.assertEqual(backend._poll_image_results.call_count, 3)
            self.assertEqual([call.args[0] for call in control.wait.call_args_list], [30, 60])
            harness.sleep.assert_not_called()
            harness.formatter.assert_not_called()
            self.assert_one_generation(backend, harness)


class ConversationPromptRecoveryTests(unittest.TestCase):
    def find(self, items, *, started_at=1000.0, exclude_ids=None):
        backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        backend._list_recent_conversations = Mock(return_value=items)
        with patch("services.openai_backend_api.logger"):
            found = backend.find_conversation_by_prompt("cat", started_at, timeout_secs=2.0, exclude_ids=exclude_ids)
        backend._list_recent_conversations.assert_called_once_with(limit=10, timeout_secs=2.0)
        return found

    def test_excluded_only_match_is_not_returned_by_a_latest_fallback(self):
        self.assertEqual(self.find([{"id": "other", "title": "Image cat", "update_time": 1000}],
                                   exclude_ids={"other"}), "")

    def test_excluded_match_does_not_hide_an_eligible_unique_match(self):
        items = [{"id": "other", "title": "Image cat", "update_time": 1000},
                 {"id": "mine", "title": "Image cat", "update_time": 1000}]
        self.assertEqual(self.find(items, exclude_ids={"other"}), "mine")

    def test_time_window_bounds_are_inclusive_and_outside_is_rejected(self):
        for timestamp, expected in ((969, ""), (970, "mine"), (1600, "mine"), (1601, "")):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(self.find([{"id": "mine", "title": "Image cat", "update_time": timestamp}]), expected)

    def test_missing_invalid_or_unknown_timestamps_are_not_guessed(self):
        for timestamp in (None, 0, "bad", "nan", "inf", {}):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(self.find([{"id": "mine", "title": "Image cat", "update_time": timestamp}]), "")
        self.assertEqual(self.find([{"id": "mine", "title": "Image cat"}]), "")
        self.assertEqual(self.find([{"id": "mine", "title": "Image cat", "update_time": 1000}], started_at=0), "")

    def test_updated_at_alias_is_supported(self):
        self.assertEqual(self.find([{"conversation_id": "mine", "title": "Image cat", "updated_at": "1000"}]), "mine")

    def test_equal_best_titles_across_conversations_are_ambiguous(self):
        items = [{"id": "one", "title": "Image cat", "update_time": 1000},
                 {"id": "two", "title": "Image cat", "update_time": 1001}]
        self.assertEqual(self.find(items), "")

    def test_unique_higher_score_wins_after_a_lower_score_tie(self):
        items = [{"id": "one", "title": "cat", "update_time": 1000},
                 {"id": "two", "title": "cat", "update_time": 1000},
                 {"id": "mine", "title": "Image cat", "update_time": 1000}]
        self.assertEqual(self.find(items), "mine")

    def test_duplicate_rows_for_same_conversation_are_not_ambiguous(self):
        item = {"id": "mine", "title": "Image cat", "update_time": 1000}
        self.assertEqual(self.find([item, dict(item)]), "mine")

    def test_unrelated_and_generic_image_titles_never_guess_latest(self):
        for title in ("unrelated", "Image", "Image generation", ""):
            with self.subTest(title=title):
                self.assertEqual(self.find([{"id": "latest", "title": title, "update_time": 1001}]), "")


if __name__ == "__main__":
    unittest.main()
