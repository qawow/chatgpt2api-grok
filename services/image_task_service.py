from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from services.config import DATA_DIR, config
from services.content_filter import request_text
from services.log_service import LOG_TYPE_CALL, log_service
from services.protocol import grok_v1_image_generations, openai_v1_image_edit, openai_v1_image_generations
from services.protocol.conversation import is_token_invalid_error, is_upstream_connection_error
from services.openai_backend_api import ImageContentPolicyError, ImagePollTimeoutError
from services.image_task_control import ImageTaskControl
from services.image_task_inputs import ImageTaskInputs
from utils.atomic import atomic_write_json
from utils.grok_models import is_grok_image_model, resolve_grok_image_model
from utils.helper import WEB_IMAGE_MODEL
from utils.log import logger

TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_ERROR = "error"
TERMINAL_STATUSES = {TASK_STATUS_SUCCESS, TASK_STATUS_ERROR}
UNFINISHED_STATUSES = {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING}


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _timestamp(value: object) -> float:
    if not isinstance(value, str) or not value.strip():
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:26], fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _clean(value: object, default: str = "") -> str:
    return str(value or default).strip()


def _owner_id(identity: dict[str, object]) -> str:
    return _clean(identity.get("id")) or "anonymous"


def _task_key(owner_id: str, task_id: str) -> str:
    return f"{owner_id}:{task_id}"


def _collect_image_urls(data: list[Any]) -> list[str]:
    urls: list[str] = []
    for item in data:
        if isinstance(item, dict):
            url = item.get("url")
            if isinstance(url, str) and url:
                urls.append(url)
    return urls


def route_image_generation(body: dict[str, Any]) -> dict[str, Any]:
    """Dispatch UI/task image generations by model.

    Web UI uses ``/api/image-tasks/generations`` (not ``/v1/images/generations``),
    so Grok free image models must be routed here too — otherwise ChatGPT's
    ``is_supported_image_model`` rejects ``grok-2-image`` with unsupported model.
    """
    model = str(body.get("model") or "").strip()
    if is_grok_image_model(model):
        payload = dict(body)
        payload["model"] = resolve_grok_image_model(model)
        # Grok free path is synchronous; progress callback is ChatGPT-SSE only.
        for key in ("progress_callback", "checkpoint_callback", "_is_cancelled", "_task_control"):
            payload.pop(key, None)
        result = grok_v1_image_generations.handle(payload)
        if not isinstance(result, dict):
            raise RuntimeError("grok image generation returned unexpected streaming result")
        return result
    return openai_v1_image_generations.handle(body)


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    item = {
        "id": task.get("id"),
        "status": task.get("status"),
        "mode": task.get("mode"),
        "model": task.get("model"),
        "size": task.get("size"),
        "quality": task.get("quality"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }
    if task.get("conversation_id"):
        item["conversation_id"] = task.get("conversation_id")
    if task.get("data") is not None:
        item["data"] = task.get("data")
    if task.get("usage") is not None:
        item["usage"] = task.get("usage")
    if task.get("error"):
        item["error"] = task.get("error")
    if task.get("progress"):
        item["progress"] = task.get("progress")
    if task.get("duration_ms") is not None:
        item["duration_ms"] = task.get("duration_ms")
    if task.get("status") in (TASK_STATUS_RUNNING, TASK_STATUS_QUEUED):
        if task.get("status") == TASK_STATUS_RUNNING:
            # RUNNING 状态仅在 started_ts 被设置后（image_stream_resolve_start）才计时
            base_ts = task.get("started_ts")
        else:
            # QUEUED 状态从 created_ts 开始计时（排队等待中）
            base_ts = task.get("created_ts") or task.get("updated_ts")
        if base_ts:
            item["elapsed_secs"] = round(time.time() - base_ts, 1)
    return item


class ImageTaskService:
    _ACCOUNT_FAILURE_COOLDOWN_SECS = 60.0
    _PROGRESS_SAVE_INTERVAL_SECS = 1.0

    def __init__(
        self,
        path: Path,
        *,
        generation_handler: Callable[[dict[str, Any]], dict[str, Any]] = route_image_generation,
        edit_handler: Callable[[dict[str, Any]], dict[str, Any]] = openai_v1_image_edit.handle,
        retention_days_getter: Callable[[], int] | None = None,
    ):
        self.path = path
        self.generation_handler = generation_handler
        self.edit_handler = edit_handler
        self.retention_days_getter = retention_days_getter or (lambda: config.image_retention_days)
        self._lock = threading.RLock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self.inputs = ImageTaskInputs(path.with_name(f"{path.stem}_inputs"))
        self._pending_input_cleanup: list[dict[str, Any]] = []
        self._last_progress_save = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._tasks = self._load_locked()
            changed = self._recover_unfinished_locked()
            for key, task in self._tasks.items():
                if isinstance(task.get("payload"), dict):
                    try:
                        payload = self.inputs.migrate(key, task["payload"])
                        if payload is not task["payload"]:
                            task["payload"] = payload
                            changed = True
                    except (OSError, ValueError):
                        # Keep the original recoverable record if migration
                        # fails; never replace it with an incomplete reference.
                        logger.warning({"event": "image_task_input_migration_failed", "task_id": task["id"]})
            changed = self._cleanup_locked() or changed
            if changed:
                self._save_locked()

    def submit_generation(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        prompt: str,
        model: str,
        size: str | None,
        quality: str = "auto",
        base_url: str = "",
    ) -> dict[str, Any]:
        payload = {
            "prompt": prompt,
            "model": model,
            "n": 1,
            "size": size,
            "quality": quality,
            "response_format": "url",
            "base_url": base_url,
        }
        return self._submit(identity, client_task_id=client_task_id, mode="generate", payload=payload)

    def submit_edit(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        prompt: str,
        model: str,
        size: str | None,
        quality: str = "auto",
        base_url: str = "",
        images: list[tuple[bytes, str, str]] | None = None,
        masks: list[tuple[bytes, str, str]] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "prompt": prompt,
            "images": images or [],
            "mask": masks or [],
            "model": model,
            "n": 1,
            "size": size,
            "quality": quality,
            "response_format": "url",
            "base_url": base_url,
        }
        return self._submit(identity, client_task_id=client_task_id, mode="edit", payload=payload)

    def list_tasks(self, identity: dict[str, object], task_ids: list[str]) -> dict[str, Any]:
        owner = _owner_id(identity)
        requested_ids = [_clean(task_id) for task_id in task_ids if _clean(task_id)]
        with self._lock:
            if self._cleanup_locked():
                self._save_locked()
            items = []
            missing_ids = []
            for task_id in requested_ids:
                task = self._tasks.get(_task_key(owner, task_id))
                if task is None:
                    missing_ids.append(task_id)
                else:
                    items.append(_public_task(task))
            if not requested_ids:
                items = [
                    _public_task(task)
                    for task in self._tasks.values()
                    if task.get("owner_id") == owner
                ]
                items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
                missing_ids = []
            return {"items": items, "missing_ids": missing_ids}

    def _submit(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        mode: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        task_id = _clean(client_task_id)
        if not task_id:
            raise ValueError("client_task_id is required")
        owner = _owner_id(identity)
        key = _task_key(owner, task_id)
        now = _now_iso()
        should_start = False
        with self._lock:
            cleaned = self._cleanup_locked()
            task = self._tasks.get(key)
            if task is not None:
                if cleaned:
                    self._save_locked()
                return _public_task(task)
            task = {
                "id": task_id,
                "owner_id": owner,
                "status": TASK_STATUS_QUEUED,
                "mode": mode,
                "model": _clean(payload.get("model"), WEB_IMAGE_MODEL),
                "size": _clean(payload.get("size")),
                "quality": _clean(payload.get("quality"), "auto"),
                "created_at": now,
                "updated_at": now,
                "created_ts": time.time(),
                "payload": self.inputs.encode(key, payload),
                "attempt": 0,
            }
            self._tasks[key] = task
            try:
                self._save_locked()
            except Exception:
                self._tasks.pop(key, None)
                raise
            should_start = True

        if should_start:
            thread = threading.Thread(
                target=self._run_task,
                args=(key, mode, payload, dict(identity), _clean(payload.get("model"), WEB_IMAGE_MODEL)),
                name=f"image-task-{task_id[:16]}",
                daemon=True,
            )
            thread.start()
        return _public_task(task)

    def _run_task(
        self,
        key: str,
        mode: str,
        payload: dict[str, Any],
        identity: dict[str, object],
        model: str,
        attempt: int = 0,
        control: ImageTaskControl | None = None,
    ) -> None:
        started = time.time()
        control = control or ImageTaskControl(float(config.image_task_timeout_secs))

        def update(**updates: Any) -> None:
            self._update_task(key, expected_attempt=attempt, **updates)

        # 创建进度回调，每个步骤完成后更新任务状态
        def progress_callback(step: str) -> None:
            if step == "image_stream_resolve_start":
                update(started_ts=time.time())
            update(progress=step)

        def checkpoint_callback(checkpoint: dict[str, Any]) -> None:
            with self._lock:
                task = self._tasks.get(key) or {}
                updates = {
                    k: v for k, v in checkpoint.items()
                    if k in {"account_email", "conversation_id", "account_token_hash", "generation_id"}
                }
                failed_email = checkpoint.get("failed_account_email")
                if failed_email:
                    updates["failed_account_emails"] = list(dict.fromkeys([
                        *task.get("failed_account_emails", []), failed_email,
                    ]))
                failed_hash = checkpoint.get("failed_token_hash")
                if failed_hash:
                    updates["failed_token_hashes"] = list(dict.fromkeys([
                        *task.get("failed_token_hashes", []), failed_hash,
                    ]))
                    failures = dict(task.get("account_failures") or {})
                    failures[failed_hash] = {
                        "kind": checkpoint.get("failure_kind", "transient"),
                        "retry_at": time.time() + self._ACCOUNT_FAILURE_COOLDOWN_SECS,
                        "attempt": attempt,
                    }
                    updates["account_failures"] = failures
                update(**updates)

        def is_cancelled() -> bool:
            with self._lock:
                task = self._tasks.get(key)
                return (
                    control.is_cancelled() or task is None or task.get("status") in TERMINAL_STATUSES
                    or int(task.get("attempt") or 0) != attempt
                )
        # 将进度回调添加到 payload 中（handler 会提取并传递给 ConversationRequest）
        payload_with_progress = {
            **payload, "progress_callback": progress_callback,
            "checkpoint_callback": checkpoint_callback, "_is_cancelled": is_cancelled,
            "_task_control": control,
        }
        try:
            update(status=TASK_STATUS_RUNNING, error="")
            try:
                task_timeout = float(config.image_task_timeout_secs)
            except Exception:
                task_timeout = 600.0
            task_timeout = max(1.0, task_timeout)

            if mode == "edit" and is_grok_image_model(model):
                raise RuntimeError("Grok 本地池不支持图生图")
            else:
                handler = self.edit_handler if mode == "edit" else self.generation_handler
            # Mark progress for Grok path (no SSE steps) so UI is not stuck blank.
            if mode != "edit" and is_grok_image_model(model):
                progress_callback("generating")
            # Run the handler in a nested worker so a hung SSE/proxy socket cannot
            # leave the task stuck at progress=generating forever. The outer
            # deadline fails the task even if the worker thread is still blocked.
            result_box: dict[str, Any] = {}
            error_box: dict[str, BaseException] = {}
            done = threading.Event()

            def _worker() -> None:
                try:
                    result_box["result"] = handler(payload_with_progress)
                except BaseException as exc:  # noqa: BLE001 - surface to outer task
                    error_box["error"] = exc
                finally:
                    done.set()

            worker = threading.Thread(
                target=_worker,
                name=f"image-task-worker-{key[-16:]}",
                daemon=True,
            )
            worker.start()
            if not done.wait(timeout=control.remaining()):
                control.cancelled.set()
                raise TimeoutError(
                    f"图片任务超时（已等待 {int(task_timeout)} 秒仍未完成；"
                    f"常见于上游 SSE 经代理半开卡住）。可在 config 中调大 "
                    f"image_task_timeout_secs / image_sse_idle_timeout_secs。"
                )
            control.remaining()
            if "error" in error_box:
                raise error_box["error"]
            result = result_box.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("image task returned streaming result unexpectedly")
            data = result.get("data")
            account_email = _clean(result.get("_account_email") or result.get("account_email"))
            if not isinstance(data, list) or not data:
                upstream = _clean(result.get("message"))
                if upstream:
                    message = upstream
                else:
                    message = "号池中没有可用账号或所有账号均被限流，请检查号池状态（账号额度、是否被封禁、是否到达生图上限）"
                error = RuntimeError(message)
                if account_email:
                    setattr(error, "account_email", account_email)
                raise error
            usage = result.get("usage")
            duration_ms = int((time.time() - started) * 1000)
            update(
                status=TASK_STATUS_SUCCESS,
                data=data,
                usage=usage,
                error="",
                duration_ms=duration_ms,
                account_email=account_email,
            )
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用完成",
                request_preview=request_text(payload.get("prompt")),
                urls=_collect_image_urls(data),
                account_email=account_email,
            )
        except Exception as exc:
            control.cancelled.set()
            error_message = str(exc) or "image task failed"
            account_email = _clean(getattr(exc, "account_email", ""))
            conversation_id = _clean(getattr(exc, "conversation_id", ""))
            duration_ms = int((time.time() - started) * 1000)
            update(
                status=TASK_STATUS_ERROR,
                error=error_message,
                data=[],
                duration_ms=duration_ms,
                error_code=_clean(getattr(exc, "code", "")),
                **({"account_email": account_email} if account_email else {}),
                **({"conversation_id": conversation_id} if conversation_id else {}),
            )
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用失败",
                request_preview=request_text(payload.get("prompt")),
                status="failed",
                error=error_message,
                account_email=account_email,
            )

    def _log_call(
        self,
        identity: dict[str, object],
        mode: str,
        model: str,
        started: float,
        suffix: str,
        *,
        request_preview: str = "",
        status: str = "success",
        error: str = "",
        urls: list[str] | None = None,
        account_email: str = "",
    ) -> None:
        endpoint = "/v1/images/edits" if mode == "edit" else "/v1/images/generations"
        summary_prefix = "图生图" if mode == "edit" else "文生图"
        detail = {
            "key_id": identity.get("id"),
            "key_name": identity.get("name"),
            "role": identity.get("role"),
            "endpoint": endpoint,
            "model": model,
            "started_at": datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at": _now_iso(),
            "duration_ms": int((time.time() - started) * 1000),
            "status": status,
        }
        if request_preview:
            detail["request_text"] = request_preview
        if error:
            detail["error"] = error
        if account_email:
            detail["account_email"] = account_email
        if urls:
            detail["urls"] = list(dict.fromkeys(urls))
        try:
            log_service.add(LOG_TYPE_CALL, f"{summary_prefix}{suffix}", detail)
        except Exception:
            pass

    def _update_task(self, key: str, *, expected_attempt: int | None = None, **updates: Any) -> None:
        with self._lock:
            task = self._tasks.get(key)
            if task is None:
                return
            if expected_attempt is not None and (
                int(task.get("attempt") or 0) != expected_attempt
                or task.get("status") in TERMINAL_STATUSES
            ):
                return
            # Prevent a late-arriving worker from overwriting an ERROR status
            # (e.g. set by a timeout) with SUCCESS. Once a task is marked
            # ERROR/timeout, only resume_poll or explicit retry can change it.
            current_status = _clean(task.get("status"))
            new_status = _clean(updates.get("status"))
            if (
                current_status == TASK_STATUS_ERROR
                and new_status
                and new_status != TASK_STATUS_ERROR
                and new_status != TASK_STATUS_RUNNING
            ):
                return
            task.update(updates)
            if new_status == TASK_STATUS_ERROR:
                task["last_failure_at"] = time.time()
                fingerprint = _clean(task.get("account_token_hash"))
                failures = dict(task.get("account_failures") or {})
                if fingerprint and failures.get(fingerprint, {}).get("attempt") != task.get("attempt", 0):
                    failures[fingerprint] = {
                        "kind": "transient", "attempt": task.get("attempt", 0),
                        "retry_at": time.time() + self._ACCOUNT_FAILURE_COOLDOWN_SECS,
                    }
                    task["account_failures"] = failures
            task["updated_at"] = _now_iso()
            task["updated_ts"] = time.time()
            if set(updates) <= {"progress", "started_ts"}:
                if time.monotonic() - self._last_progress_save < self._PROGRESS_SAVE_INTERVAL_SECS:
                    return
            self._save_locked()

    def _load_locked(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        raw_items = raw.get("tasks") if isinstance(raw, dict) else raw
        if not isinstance(raw_items, list):
            return {}
        tasks: dict[str, dict[str, Any]] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            task_id = _clean(item.get("id"))
            owner = _clean(item.get("owner_id"))
            if not task_id or not owner:
                continue
            status = _clean(item.get("status"))
            if status not in {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING, TASK_STATUS_SUCCESS, TASK_STATUS_ERROR}:
                status = TASK_STATUS_ERROR
            task = {
                "id": task_id,
                "owner_id": owner,
                "status": status,
                "mode": "edit" if item.get("mode") == "edit" else "generate",
                "model": _clean(item.get("model"), WEB_IMAGE_MODEL),
                "size": _clean(item.get("size")),
                "quality": _clean(item.get("quality"), "auto"),
                "created_at": _clean(item.get("created_at"), _now_iso()),
                "updated_at": _clean(item.get("updated_at"), _clean(item.get("created_at"), _now_iso())),
                "created_ts": item.get("created_ts"),
                "updated_ts": item.get("updated_ts"),
                "started_ts": item.get("started_ts"),
                "duration_ms": item.get("duration_ms"),
                "attempt": int(item.get("attempt") or 0),
                "account_email": _clean(item.get("account_email")),
                "failed_account_emails": item.get("failed_account_emails") or [],
                "account_token_hash": _clean(item.get("account_token_hash")),
                "failed_token_hashes": item.get("failed_token_hashes") or [],
                "error_code": _clean(item.get("error_code")),
                "generation_id": _clean(item.get("generation_id")),
                "account_failures": item.get("account_failures") or {},
                "last_failure_at": item.get("last_failure_at") or item.get("updated_ts") or _timestamp(item.get("updated_at")),
            }
            if isinstance(item.get("payload"), dict):
                task["payload"] = item["payload"]
            data = item.get("data")
            if isinstance(data, list):
                task["data"] = data
            usage = item.get("usage")
            if isinstance(usage, dict):
                task["usage"] = usage
            error = _clean(item.get("error"))
            if error:
                task["error"] = error
            progress = _clean(item.get("progress"))
            if progress:
                task["progress"] = progress
            conversation_id = _clean(item.get("conversation_id"))
            if conversation_id:
                task["conversation_id"] = conversation_id
            tasks[_task_key(owner, task_id)] = task
        return tasks

    def _save_locked(self) -> None:
        items = sorted(self._tasks.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        atomic_write_json(self.path, {"tasks": items})
        self._last_progress_save = time.monotonic()
        # Remove expired attachments only AFTER the metadata deletion commits.
        pending, self._pending_input_cleanup = self._pending_input_cleanup, []
        live_blobs = {
            entry["blob"] for task in self._tasks.values()
            for field in ("images", "mask")
            for entry in (task.get("payload") or {}).get(field, [])
            if isinstance(entry, dict) and "blob" in entry
        }
        for payload in pending:
            try:
                self.inputs.delete(payload, keep=live_blobs)
            except (OSError, ValueError):
                self._pending_input_cleanup.append(payload)
                logger.warning({"event": "image_task_input_cleanup_failed"})

    def _recover_unfinished_locked(self) -> bool:
        changed = False
        for task in self._tasks.values():
            if task.get("status") in UNFINISHED_STATUSES:
                task["status"] = TASK_STATUS_ERROR
                task["error"] = "服务已重启，未完成的图片任务已中断"
                task["updated_at"] = _now_iso()
                changed = True
        return changed

    def _cleanup_locked(self) -> bool:
        try:
            retention_days = max(1, int(self.retention_days_getter()))
        except Exception:
            retention_days = 30
        cutoff = time.time() - retention_days * 86400
        removed_keys = [
            key
            for key, task in self._tasks.items()
            if task.get("status") in TERMINAL_STATUSES and _timestamp(task.get("updated_at")) < cutoff
        ]
        for key in removed_keys:
            task = self._tasks.pop(key)
            self._pending_input_cleanup.append(task.get("payload") or {})
        return bool(removed_keys)

    def resume_poll(
        self,
        identity: dict[str, object],
        task_id: str,
        extra_timeout_secs: float = 30.0,
    ) -> dict[str, Any]:
        """Resume the same task: poll its conversation, or replay saved inputs on another account."""
        owner = _owner_id(identity)
        key = _task_key(owner, _clean(task_id))
        with self._lock:
            task = self._tasks.get(key)
            if task is None:
                raise ValueError("task not found")
            if task.get("status") != TASK_STATUS_ERROR:
                raise ValueError("task is not in error state")
            error_msg = _clean(task.get("error"))
            if task.get("error_code") == "content_policy_violation":
                raise ValueError("内容策略拒绝不支持换号续传，请修改提示词")
            conversation_id = _clean(task.get("conversation_id"))
            # An expired account cannot read its old conversation, and another
            # account never owns that conversation. Replay the original inputs.
            if is_token_invalid_error(error_msg):
                conversation_id = ""
            if not conversation_id and not task.get("payload"):
                raise ValueError("旧任务没有保存原始请求，无法换号续传，请重新生成")
            mode = task.get("mode", "generate")
            model = task.get("model", WEB_IMAGE_MODEL)
            task["last_failure_at"] = task.get("last_failure_at") or task.get("updated_ts") or _timestamp(task.get("updated_at")) or time.time()
            # 将任务状态重置为 running
            attempt = int(task.get("attempt") or 0) + 1
            self._update_task(key, status=TASK_STATUS_RUNNING, error="", attempt=attempt,
                              started_ts=time.time(), progress="resuming", duration_ms=None)
            public = _public_task(task)

        # 启动新线程继续轮询
        thread = threading.Thread(
            target=self._run_resume_task,
            args=(key, conversation_id, extra_timeout_secs, dict(identity), mode, model, attempt),
            name=f"image-resume-{_clean(task_id)[:16]}",
            daemon=True,
        )
        thread.start()
        return public

    def _replay_task(self, key: str, identity: dict[str, object], mode: str, model: str, attempt: int,
                     control: ImageTaskControl | None = None) -> None:
        from services.account_service import account_service
        from services.content_filter import check_request

        with self._lock:
            task = self._tasks[key]
            if not task.get("payload"):
                raise ValueError("旧任务没有保存原始请求，无法换号续传，请重新生成")
            saved_payload = dict(task["payload"])
            emails = set(task.get("failed_account_emails") or [])
            if task.get("account_email"):
                emails.add(task["account_email"])
            hashes = set(task.get("failed_token_hashes") or [])
            if task.get("account_token_hash"):
                hashes.add(task["account_token_hash"])
            failures = dict(task.get("account_failures") or {})
            legacy_retry_at = float(task.get("last_failure_at") or time.time()) + self._ACCOUNT_FAILURE_COOLDOWN_SECS
            for fingerprint in hashes:
                failures.setdefault(fingerprint, {"kind": "transient", "retry_at": legacy_retry_at})
            hashes = {
                fingerprint for fingerprint in hashes | failures.keys()
                if failures.get(fingerprint, {}).get("kind") == "revoked"
                or float(failures.get(fingerprint, {}).get("retry_at", legacy_retry_at)) > time.time()
            }
        # Blob reads can be large; do not block status updates or the timeout
        # watchdog on the global task lock while loading reference images.
        payload = self.inputs.decode(saved_payload)
        check_request(request_text(payload.get("prompt")))
        if hashes:
            # Match failed credentials, not the email identity. After re-login a
            # new token for the same account is eligible again (even without email).
            payload["_excluded_tokens"] = [
                token for account in account_service.list_accounts()
                if (token := str(account.get("access_token") or ""))
                and hashlib.sha256(token.encode()).hexdigest() in hashes
            ]
        elif not failures and legacy_retry_at > time.time():
            # Compatibility for older tasks that only recorded account email.
            payload["_excluded_tokens"] = [
                token for email in emails if (token := account_service.find_access_token_by_email(email))
            ]
        else:
            payload["_excluded_tokens"] = []
        self._update_task(
            key, expected_attempt=attempt, conversation_id="", progress="switching_account",
            account_failures=failures, account_email="", account_token_hash="", generation_id="",
        )
        self._run_task(key, mode, payload, identity, model, attempt, control)

    def _run_resume_task(self, key: str, conversation_id: str, extra_timeout_secs: float,
                         identity: dict[str, object], mode: str, model: str, attempt: int) -> None:
        """Keep one wall-clock budget across poll, download and replay."""
        control = ImageTaskControl(float(config.image_task_timeout_secs))
        done = threading.Event()

        def worker() -> None:
            try:
                self._run_resume_poll(key, conversation_id, extra_timeout_secs, identity, mode, model, attempt, control)
            finally:
                done.set()

        thread = threading.Thread(target=worker, name=f"image-resume-worker-{key[-16:]}", daemon=True)
        thread.start()
        try:
            if done.wait(timeout=control.remaining()):
                return
        except TimeoutError:
            pass
        if not done.is_set():
            control.cancelled.set()
            self._update_task(key, expected_attempt=attempt, status=TASK_STATUS_ERROR,
                              error="图片续传任务超时，请稍后重试", error_code="task_timeout")

    def _run_resume_poll(
        self,
        key: str,
        conversation_id: str,
        extra_timeout_secs: float,
        identity: dict[str, object],
        mode: str,
        model: str,
        attempt: int = 0,
        control: ImageTaskControl | None = None,
    ) -> None:
        """后台线程：继续轮询已有 conversation_id 的图片结果。"""
        started = time.time()
        control = control or ImageTaskControl(float(config.image_task_timeout_secs))
        backend = None
        token = ""
        try:
            if not conversation_id:
                self._replay_task(key, identity, mode, model, attempt, control)
                return
            from services.account_service import account_service
            from services.openai_backend_api import OpenAIBackendAPI
            from services.protocol.conversation import format_image_result
            from services.proxy_service import proxy_settings

            with self._lock:
                task = self._tasks.get(key) or {}
                email = _clean(task.get("account_email"))
                fingerprint = _clean(task.get("account_token_hash"))
            token = account_service.find_access_token_by_email(email) if email else ""
            if not token and fingerprint:
                token = next((
                    value for item in account_service.list_accounts()
                    if (value := str(item.get("access_token") or ""))
                    and hashlib.sha256(value.encode()).hexdigest() == fingerprint
                ), "")
            if not token:
                self._replay_task(key, identity, mode, model, attempt, control)
                return

            account = account_service.get_account(token) or {}
            if account_service._token_looks_revoked(account) is True:
                self._replay_task(key, identity, mode, model, attempt, control)
                return
            last_error: BaseException | None = None
            file_ids: list[str] = []
            sediment_ids: list[str] = []
            image_urls: list[str] = []
            poll_deadline = time.monotonic() + extra_timeout_secs
            for _source, proxy_url in proxy_settings.list_egress_candidates(account=account, upstream=True):
                control.remaining()
                poll_remaining = poll_deadline - time.monotonic()
                if poll_remaining <= 0:
                    raise ImagePollTimeoutError("图片续轮询超时")
                candidate = OpenAIBackendAPI(access_token=token, force_proxy=proxy_url)
                candidate.task_control = control
                try:
                    file_ids, sediment_ids = candidate._poll_image_results(
                        conversation_id,
                        control.remaining(poll_remaining),
                    )
                    control.remaining()
                    if not file_ids and not sediment_ids:
                        raise ImagePollTimeoutError(
                            f"继续等待 {extra_timeout_secs} 秒后仍未找到图片结果。"
                        )
                    image_urls = candidate.resolve_conversation_image_urls(
                        conversation_id, file_ids, sediment_ids, poll=False,
                    )
                    if not image_urls:
                        raise RuntimeError("图片 URL 解析失败")
                    backend = candidate
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if not is_upstream_connection_error(str(exc)):
                        candidate.close()
                        raise
                    candidate.close()
            if backend is None:
                raise last_error or RuntimeError("续轮询失败：无法连接上游")

            control.remaining()
            image_items = [
                {"b64_json": base64.b64encode(image_data).decode("ascii")}
                for image_data in backend.download_image_bytes(image_urls)
            ]
            control.remaining()
            with self._lock:
                task = self._tasks.get(key) or {}
                payload = task.get("payload") or {}
                generation_id = _clean(task.get("generation_id")) or f"conversation:{conversation_id}"
            data = format_image_result(
                image_items,
                _clean(payload.get("prompt")),
                _clean(payload.get("response_format"), "url"),
                _clean(payload.get("base_url")),
                int(time.time()),
            )["data"]
            if not data:
                raise RuntimeError("续传没有返回图片数据")
            control.remaining()
            # This poll owns no image slot. Use the original generation ID to
            # settle exactly once, even if the original worker already did so.
            account_service.mark_image_result(token, True, release_slot=False, result_id=generation_id)
            self._update_task(key, expected_attempt=attempt, status=TASK_STATUS_SUCCESS, data=data, error="", duration_ms=int((time.time() - started) * 1000))
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用完成（续轮询）",
                status="success",
                urls=_collect_image_urls(data),
            )
        except Exception as exc:
            # A dead session/transport cannot be continued across accounts.
            # Replay only when original inputs exist; never replay policy errors.
            with self._lock:
                can_replay = bool((self._tasks.get(key) or {}).get("payload"))
            if not control.is_cancelled() and conversation_id and can_replay and (
                is_token_invalid_error(str(exc)) or is_upstream_connection_error(str(exc))
                or isinstance(exc, ImagePollTimeoutError)
            ):
                try:
                    self._replay_task(key, identity, mode, model, attempt, control)
                    return
                except Exception as replay_exc:
                    exc = replay_exc
            error_message = str(exc) or "resume poll failed"
            duration_ms = int((time.time() - started) * 1000)
            self._update_task(
                key, expected_attempt=attempt, status=TASK_STATUS_ERROR,
                error=error_message, data=[], duration_ms=duration_ms,
                error_code="content_policy_violation" if isinstance(exc, ImageContentPolicyError) else _clean(getattr(exc, "code", "")),
            )
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用失败（续轮询）",
                status="failed",
                error=error_message,
            )
        finally:
            if backend is not None:
                backend.close()


image_task_service = ImageTaskService(DATA_DIR / "image_tasks.json")
