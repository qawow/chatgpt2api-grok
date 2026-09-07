from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from services.image_task_service import ImageTaskService


OWNER = {"id": "owner-1", "name": "Owner", "role": "admin"}
OTHER_OWNER = {"id": "owner-2", "name": "Other", "role": "user"}


def wait_for_task(service: ImageTaskService, identity: dict[str, object], task_id: str, status: str, timeout: float = 2.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        result = service.list_tasks(identity, [task_id])
        last = (result.get("items") or [None])[0]
        if last and last.get("status") == status:
            return last
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} did not reach {status}, last={last}")


class ImageTaskServiceTests(unittest.TestCase):
    def make_service(self, path: Path, handler=None) -> ImageTaskService:
        return ImageTaskService(
            path,
            generation_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/image.png"}]}),
            edit_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/edit.png"}]}),
            retention_days_getter=lambda: 30,
        )

    def test_duplicate_submit_uses_existing_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            calls = 0

            def handler(_payload):
                nonlocal calls
                calls += 1
                time.sleep(0.05)
                return {"data": [{"url": "http://example.test/image.png"}]}

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", handler)
            first = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            second = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            self.assertEqual(first["id"], "task-1")
            self.assertEqual(second["id"], "task-1")
            task = wait_for_task(service, OWNER, "task-1", "success")
            self.assertEqual(task["data"][0]["url"], "http://example.test/image.png")
            self.assertEqual(calls, 1)

    def test_different_owner_cannot_query_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_tasks.json")
            service.submit_generation(
                OWNER,
                client_task_id="private-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            wait_for_task(service, OWNER, "private-task", "success")
            result = service.list_tasks(OTHER_OWNER, ["private-task"])

            self.assertEqual(result["items"], [])
            self.assertEqual(result["missing_ids"], ["private-task"])

    def test_success_task_persists_to_new_service_instance(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            service = self.make_service(path)
            service.submit_generation(
                OWNER,
                client_task_id="persisted-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            wait_for_task(service, OWNER, "persisted-task", "success")

            reloaded = self.make_service(path)
            result = reloaded.list_tasks(OWNER, ["persisted-task"])

            self.assertEqual(result["missing_ids"], [])
            self.assertEqual(result["items"][0]["status"], "success")
            self.assertEqual(result["items"][0]["data"][0]["url"], "http://example.test/image.png")

    def test_startup_marks_unfinished_tasks_as_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "queued-task",
                                "owner_id": "owner-1",
                                "status": "queued",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                            {
                                "id": "running-task",
                                "owner_id": "owner-1",
                                "status": "running",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            service = self.make_service(path)
            result = service.list_tasks(OWNER, ["queued-task", "running-task"])

            self.assertEqual([item["status"] for item in result["items"]], ["error", "error"])
            self.assertTrue(all("已中断" in item.get("error", "") for item in result["items"]))

    def test_task_wall_clock_timeout_marks_error(self):
        """Hung handler must fail the task instead of leaving progress=generating."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            def hanging_handler(_payload):
                time.sleep(30)
                return {"data": [{"url": "http://example.test/late.png"}]}

            service = ImageTaskService(
                Path(tmp_dir) / "image_tasks.json",
                generation_handler=hanging_handler,
                edit_handler=hanging_handler,
                retention_days_getter=lambda: 30,
            )
            import services.image_task_service as its

            cfg = its.config
            prev = cfg.data.get("image_task_timeout_secs")
            cfg.data["image_task_timeout_secs"] = 1.0
            try:
                task = service.submit_generation(
                    OWNER,
                    client_task_id="timeout-task",
                    prompt="cat",
                    model="gpt-image-2",
                    size=None,
                    base_url="http://local.test",
                )
                self.assertEqual(task["id"], "timeout-task")
                finished = wait_for_task(service, OWNER, "timeout-task", "error", timeout=4.0)
                self.assertIn("超时", finished.get("error", ""))
            finally:
                if prev is None:
                    cfg.data.pop("image_task_timeout_secs", None)
                else:
                    cfg.data["image_task_timeout_secs"] = prev

    def test_route_image_generation_dispatches_grok_models(self):
        import services.image_task_service as its

        with mock.patch.object(
            its.grok_v1_image_generations,
            "handle",
            return_value={"created": 1, "data": [{"b64_json": "QQ=="}], "_meta": {"upstream_path": "responses+image_generation"}},
        ) as grok_handle, mock.patch.object(
            its.openai_v1_image_generations,
            "handle",
            side_effect=AssertionError("chatgpt path must not run for grok models"),
        ):
            result = its.route_image_generation(
                {
                    "prompt": "apple",
                    "model": "grok-2-image",
                    "n": 1,
                    "progress_callback": lambda _step: None,
                }
            )
            self.assertEqual(result["data"][0]["b64_json"], "QQ==")
            grok_handle.assert_called_once()
            payload = grok_handle.call_args.args[0]
            self.assertEqual(payload["model"], "grok-2-image")
            self.assertNotIn("progress_callback", payload)

    def test_submit_generation_routes_grok_model_to_handler(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            calls: list[str] = []

            def handler(payload):
                calls.append(str(payload.get("model") or ""))
                return {"data": [{"url": "http://example.test/grok.png"}]}

            service = ImageTaskService(
                Path(tmp_dir) / "image_tasks.json",
                generation_handler=handler,
                edit_handler=handler,
                retention_days_getter=lambda: 30,
            )
            service.submit_generation(
                OWNER,
                client_task_id="grok-task",
                prompt="apple",
                model="grok-imagine",
                size=None,
                base_url="http://local.test",
            )
            task = wait_for_task(service, OWNER, "grok-task", "success")
            self.assertEqual(task["data"][0]["url"], "http://example.test/grok.png")
            self.assertEqual(calls, ["grok-imagine"])

    def test_resume_poll_uses_account_token_and_egress(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            service = self.make_service(path)
            key = "owner-1:resume-task"
            now = "2026-09-07 12:00:00"
            service._tasks[key] = {
                "id": "resume-task",
                "owner_id": "owner-1",
                "status": "error",
                "mode": "generate",
                "model": "gpt-image-2",
                "error": "图片任务超时（已等待 1 秒仍未完成）",
                "conversation_id": "conv-1",
                "account_email": "a@example.com",
                "created_at": now,
                "updated_at": now,
            }
            created: list[tuple[str, str | None]] = []

            class FakeBackend:
                def __init__(self, access_token: str = "", *, force_proxy: str | None = None) -> None:
                    self.access_token = access_token
                    self.force_proxy = force_proxy
                    created.append((access_token, force_proxy))

                def _poll_image_results(self, conversation_id: str, extra_timeout_secs: float):
                    if self.force_proxy:
                        raise OSError("ProxyError: connection refused")
                    return ["file-1"], []

                def resolve_conversation_image_urls(self, conversation_id, file_ids, sediment_ids, poll=False):
                    return ["https://cdn.example/a.png"]

                def download_image_bytes(self, urls):
                    return [b"png"]

                def close(self) -> None:
                    return None

            with mock.patch("services.account_service.account_service") as accounts, mock.patch(
                "services.openai_backend_api.OpenAIBackendAPI", FakeBackend
            ), mock.patch(
                "services.proxy_service.proxy_settings.list_egress_candidates",
                return_value=[("account", "socks5h://dead.example:1080"), ("direct", "")],
            ), mock.patch(
                "services.protocol.conversation.format_image_result",
                return_value={"data": [{"url": "http://example.test/ok.png"}]},
            ):
                accounts.find_access_token_by_email.return_value = "token-a"
                accounts.get_account.return_value = {"email": "a@example.com", "access_token": "token-a"}
                service.resume_poll(OWNER, "resume-task", extra_timeout_secs=1)
                task = wait_for_task(service, OWNER, "resume-task", "success", timeout=3.0)

            self.assertEqual(created, [("token-a", "socks5h://dead.example:1080"), ("token-a", "")])
            self.assertTrue(task.get("data"))


if __name__ == "__main__":
    unittest.main()
