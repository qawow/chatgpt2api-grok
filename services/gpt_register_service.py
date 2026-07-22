"""Batch ChatGPT free-account registration via any-register-engines.

Runs outside the request thread, pushes successes into the local ChatGPT
account pool (same process account_service when push_mode=local, or HTTP
POST when push_mode=http).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config import DATA_DIR, config

GPT_REGISTER_CONFIG_FILE = DATA_DIR / "gpt_register_config.json"
GPT_REGISTER_JOBS_FILE = DATA_DIR / "gpt_register_jobs.json"

_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}\s*$")

def _builtin_engines_dir() -> str:
    try:
        from gpt_free_register.runner import default_engines_dir

        return default_engines_dir()
    except Exception:
        return str(Path(__file__).resolve().parent.parent / "gpt_free_register" / "engines")


DEFAULT_SETTINGS: dict[str, Any] = {
    "engines_dir": "",  # empty → builtin gpt_free_register/engines
    "run_mode": "inprocess",  # inprocess | subprocess
    "python_bin": "",  # only for subprocess mode
    "count": 1,
    "concurrency": 1,
    "interval_secs": 2,
    "timeout_secs": 600,
    "executor": "protocol",
    "mail_provider": "cloudflare_d1_api",
    "captcha": "",
    "proxy": "",  # empty → engines .env REGISTER_PROXY_DEFAULT
    "bind_register_proxy": True,
    "plan_type": "free",
    "source_type": "",
    "cfd1_domain": "",  # optional override CFD1_DOMAIN for this job
    "push_enabled": True,
    "push_mode": "local",  # local | http
    # empty → auto (local in-process import; http mode uses container-aware default)
    "chatgpt2api_base_url": "",
    "chatgpt2api_auth_key": "",  # empty → config.auth_key
    "dry_run": False,
    # free 号 Codex 二次 OTP 几乎总是 add_phone 失败 → 默认跳过，直接 NextAuth session
    "skip_codex": True,
    # 关闭步骤间随机抖动（OPENAI_REGISTER_NO_DELAY）；默认关，避免无必要地改行为
    "register_no_delay": False,
    # 覆盖 OPENAI_SO_COLLECT_MS；空=引擎默认 5000ms create_account
    "so_collect_ms": "",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: object) -> str:
    return str(value or "").strip()


def _default_push_base_url() -> str:
    """HTTP push target when push_mode=http.

    In Docker the app listens on :80 inside the container; host-mapped 8000 is
    not visible as 127.0.0.1:8000 from inside. Prefer in-process local import
    (push_mode=local) so this URL is unused.
    """
    if Path("/.dockerenv").exists() or _clean(os.environ.get("CHATGPT2API_IN_DOCKER")):
        return "http://127.0.0.1:80"
    return "http://127.0.0.1:8000"


def _clamp_int(value: object, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _clamp_float(value: object, default: float, lo: float, hi: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _looks_like_legacy_or_missing_engines(path: str) -> bool:
    value = _clean(path)
    if not value:
        return True
    legacy = {
        "/root/any-register-engines",
        "any-register-engines",
        "/app/any-register-engines",
    }
    if value in legacy or value.rstrip("/").endswith("/any-register-engines"):
        return True
    p = Path(value)
    # Saved Docker/host path that no longer exists → fall back to builtin.
    if not p.is_dir():
        return True
    if not (p / "platforms" / "chatgpt" / "plugin.py").is_file():
        return True
    return False


def normalize_settings(raw: object | None) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_SETTINGS)
    out.update({k: src[k] for k in DEFAULT_SETTINGS if k in src})
    engines_dir = _clean(out.get("engines_dir"))
    if _looks_like_legacy_or_missing_engines(engines_dir):
        engines_dir = _builtin_engines_dir()
    out["engines_dir"] = engines_dir
    run_mode = _clean(out.get("run_mode")).lower() or "inprocess"
    if run_mode not in {"inprocess", "subprocess"}:
        run_mode = "inprocess"
    out["run_mode"] = run_mode
    out["python_bin"] = _clean(out.get("python_bin"))
    out["count"] = _clamp_int(out.get("count"), 1, 1, 50)
    out["concurrency"] = _clamp_int(out.get("concurrency"), 1, 1, 5)
    out["interval_secs"] = _clamp_float(out.get("interval_secs"), 2, 0, 600)
    out["timeout_secs"] = _clamp_int(out.get("timeout_secs"), 600, 60, 3600)
    executor = _clean(out.get("executor")).lower() or "protocol"
    if executor not in {"protocol", "headless", "headed"}:
        executor = "protocol"
    out["executor"] = executor
    out["mail_provider"] = _clean(out.get("mail_provider")) or "cloudflare_d1_api"
    out["captcha"] = _clean(out.get("captcha"))
    out["proxy"] = _clean(out.get("proxy"))
    out["bind_register_proxy"] = bool(out.get("bind_register_proxy"))
    out["plan_type"] = _clean(out.get("plan_type")) or "free"
    out["source_type"] = _clean(out.get("source_type"))
    out["cfd1_domain"] = _clean(out.get("cfd1_domain"))
    out["push_enabled"] = bool(out.get("push_enabled", True))
    push_mode = _clean(out.get("push_mode")).lower() or "local"
    if push_mode not in {"local", "http"}:
        push_mode = "local"
    out["push_mode"] = push_mode
    out["chatgpt2api_base_url"] = _clean(out.get("chatgpt2api_base_url")) or _default_push_base_url()
    out["chatgpt2api_auth_key"] = _clean(out.get("chatgpt2api_auth_key"))
    out["dry_run"] = bool(out.get("dry_run"))
    # default True: free accounts almost always fail Codex with add_phone
    if "skip_codex" not in src:
        out["skip_codex"] = True
    else:
        out["skip_codex"] = bool(out.get("skip_codex"))
    out["register_no_delay"] = bool(out.get("register_no_delay"))
    out["so_collect_ms"] = _clean(out.get("so_collect_ms"))
    return out


def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Hide secrets in API responses."""
    item = dict(settings)
    key = _clean(item.get("chatgpt2api_auth_key"))
    item["chatgpt2api_auth_key"] = ""
    item["has_chatgpt2api_auth_key"] = bool(key) or bool(_clean(config.auth_key))
    return item


class GptRegisterConfig:
    def __init__(self, path: Path | None = None):
        self.path = path or GPT_REGISTER_CONFIG_FILE
        self._lock = threading.RLock()
        self._settings = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return normalize_settings(None)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return normalize_settings(None)
        return normalize_settings(raw)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def get(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._settings)

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            merged = {**self._settings, **(patch or {})}
            # empty auth key means keep previous
            if "chatgpt2api_auth_key" in (patch or {}) and not _clean(patch.get("chatgpt2api_auth_key")):
                merged["chatgpt2api_auth_key"] = self._settings.get("chatgpt2api_auth_key") or ""
            self._settings = normalize_settings(merged)
            self._save()
            return dict(self._settings)


class GptRegisterService:
    def __init__(self, config_store: GptRegisterConfig | None = None):
        self.config_store = config_store or GptRegisterConfig()
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cancel_flags: dict[str, threading.Event] = {}
        self._load_jobs()

    def _load_jobs(self) -> None:
        if not GPT_REGISTER_JOBS_FILE.exists():
            return
        try:
            raw = json.loads(GPT_REGISTER_JOBS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict) and item.get("job_id"):
                        # mark unfinished as failed on restart
                        if item.get("status") in {"pending", "running"}:
                            item["status"] = "failed"
                            item["error"] = item.get("error") or "interrupted by restart"
                            item["finished_at"] = _now_iso()
                        self._jobs[str(item["job_id"])] = item
        except Exception:
            pass

    def _save_jobs(self) -> None:
        GPT_REGISTER_JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        # keep last 20 jobs
        items = sorted(
            self._jobs.values(),
            key=lambda j: str(j.get("created_at") or ""),
            reverse=True,
        )[:20]
        GPT_REGISTER_JOBS_FILE.write_text(
            json.dumps(items, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(
                self._jobs.values(),
                key=lambda j: str(j.get("created_at") or ""),
                reverse=True,
            )
            return [dict(j) for j in items]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(_clean(job_id))
            return dict(job) if job else None

    def cancel_job(self, job_id: str) -> dict[str, Any] | None:
        jid = _clean(job_id)
        with self._lock:
            job = self._jobs.get(jid)
            if not job:
                return None
            flag = self._cancel_flags.get(jid)
            if flag:
                flag.set()
            if job.get("status") in {"pending", "running"}:
                job = dict(job)
                job["cancel_requested"] = True
                self._jobs[jid] = job
                self._save_jobs()
            return dict(self._jobs[jid])

    def start_job(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        base = self.config_store.get()
        if overrides:
            # empty auth key keep stored
            merged = {**base, **overrides}
            if "chatgpt2api_auth_key" in overrides and not _clean(overrides.get("chatgpt2api_auth_key")):
                merged["chatgpt2api_auth_key"] = base.get("chatgpt2api_auth_key") or ""
            settings = normalize_settings(merged)
        else:
            settings = normalize_settings(base)

        # only one running job at a time
        with self._lock:
            for job in self._jobs.values():
                if job.get("status") in {"pending", "running"}:
                    raise RuntimeError("已有注册任务在运行，请等待结束或先取消")

            job_id = uuid.uuid4().hex
            job = {
                "job_id": job_id,
                "status": "pending",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "started_at": None,
                "finished_at": None,
                "settings": public_settings(settings),
                "total": int(settings["count"]),
                "completed": 0,
                "success": 0,
                "failed": 0,
                "added": 0,
                "items": [],
                "logs": [],
                "error": None,
                "cancel_requested": False,
            }
            self._jobs[job_id] = job
            self._cancel_flags[job_id] = threading.Event()
            self._save_jobs()

        thread = threading.Thread(
            target=self._run_job,
            args=(job_id, settings),
            name=f"gpt-register-{job_id[:8]}",
            daemon=True,
        )
        thread.start()
        return dict(job)

    def _append_log(
        self,
        job_id: str,
        message: str,
        *,
        level: str = "info",
        force_save: bool = False,
    ) -> None:
        msg = str(message or "").strip()
        if not msg:
            return
        # also emit to process stdout for docker logs / journal
        try:
            print(f"[gpt-register:{job_id[:8]}] {msg}", flush=True)
        except Exception:
            pass
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            logs = list(job.get("logs") or [])
            logs.append({"at": _now_iso(), "level": level, "message": msg[:800]})
            job["logs"] = logs[-400:]
            job["updated_at"] = _now_iso()
            self._jobs[job_id] = job
            # Throttle disk writes: every log line used to rewrite jobs JSON.
            # Persist on force, errors, or every N lines / ~2s.
            should_save = force_save or level in {"error", "warn", "warning"}
            if not should_save:
                last_save = float(job.get("_last_log_save_at") or 0)
                log_count = int(job.get("_log_save_counter") or 0) + 1
                job["_log_save_counter"] = log_count
                now = time.time()
                if log_count >= 8 or (now - last_save) >= 2.0:
                    should_save = True
            if should_save:
                job["_last_log_save_at"] = time.time()
                job["_log_save_counter"] = 0
                try:
                    self._save_jobs()
                except Exception:
                    pass

    def _patch_job(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job = dict(job)
            job.update(fields)
            job["updated_at"] = _now_iso()
            self._jobs[job_id] = job
            self._save_jobs()

    def _run_job(self, job_id: str, settings: dict[str, Any]) -> None:
        cancel = self._cancel_flags.get(job_id) or threading.Event()
        started_at = _now_iso()
        self._patch_job(job_id, status="running", started_at=started_at)
        self._append_log(
            job_id,
            "任务开始："
            f"count={settings.get('count')} concurrency={settings.get('concurrency')} "
            f"interval={settings.get('interval_secs')}s mode={settings.get('run_mode')} "
            f"executor={settings.get('executor')} mail={settings.get('mail_provider')} "
            f"push={settings.get('push_enabled')}/{settings.get('push_mode')} "
            f"proxy={'yes' if settings.get('proxy') else 'default/env'} "
            f"engines={settings.get('engines_dir')}",
        )

        total = int(settings["count"])
        concurrency = int(settings["concurrency"])
        interval = float(settings["interval_secs"])
        success = failed = added = completed = 0
        items: list[dict[str, Any]] = []
        t0 = time.time()

        try:
            self._validate_engines(settings)
            self._append_log(job_id, f"注册机校验通过：{settings.get('engines_dir')}")
        except Exception as exc:
            self._patch_job(
                job_id,
                status="failed",
                error=str(exc)[:300],
                finished_at=_now_iso(),
                summary={
                    "status": "failed",
                    "error": str(exc)[:300],
                    "duration_secs": round(time.time() - t0, 2),
                },
            )
            self._append_log(job_id, f"启动失败：{exc}", level="error")
            self._emit_completion_log(
                job_id,
                status="failed",
                success=0,
                failed=0,
                added=0,
                completed=0,
                total=total,
                duration=time.time() - t0,
                items=[],
                error=str(exc),
                settings=settings,
            )
            return

        def one(index: int) -> dict[str, Any]:
            if cancel.is_set():
                return {
                    "index": index,
                    "ok": False,
                    "cancelled": True,
                    "error": "cancelled",
                }
            try:
                result = self._register_once(settings)
                return {"index": index, **result}
            except Exception as exc:
                return {
                    "index": index,
                    "ok": False,
                    "error": str(exc)[:300],
                    "email": None,
                    "logs": [f"exception: {exc}"],
                }

        # sequential with optional limited concurrency batches
        index = 0
        while index < total:
            if cancel.is_set():
                self._append_log(job_id, "收到取消请求，停止后续注册", level="warn")
                break
            batch_size = min(concurrency, total - index)
            batch_indexes = list(range(index + 1, index + batch_size + 1))
            index += batch_size
            self._append_log(
                job_id,
                f"开始批次 indexes={batch_indexes[0]}-{batch_indexes[-1]} size={batch_size}",
            )

            if concurrency <= 1:
                outcomes = [one(batch_indexes[0])]
            else:
                with ThreadPoolExecutor(max_workers=batch_size) as pool:
                    futs = [pool.submit(one, i) for i in batch_indexes]
                    outcomes = [f.result() for f in as_completed(futs)]
                    outcomes.sort(key=lambda x: int(x.get("index") or 0))

            for outcome in outcomes:
                completed += 1
                engine_logs = outcome.get("logs") if isinstance(outcome.get("logs"), list) else []
                item = {
                    "index": outcome.get("index"),
                    "ok": bool(outcome.get("ok")),
                    "email": outcome.get("email"),
                    "error": outcome.get("error"),
                    "added": int(outcome.get("added") or 0),
                    "has_token": bool(outcome.get("has_token")),
                    "push": outcome.get("push"),
                    "mode": outcome.get("mode"),
                    "logs_tail": [str(x)[:200] for x in engine_logs[-8:]],
                }
                items.append(item)

                # forward engine step logs for troubleshooting
                for line in engine_logs[-30:]:
                    self._append_log(job_id, f"  · #{item.get('index')}: {line}")

                if item["ok"]:
                    success += 1
                    added += int(item["added"] or 0)
                    push = item.get("push") if isinstance(item.get("push"), dict) else {}
                    self._append_log(
                        job_id,
                        f"[{completed}/{total}] 成功 email={item.get('email') or '-'} "
                        f"added={item['added']} has_token={item['has_token']} "
                        f"mode={item.get('mode') or '-'} push_ok={push.get('ok')}",
                    )
                else:
                    failed += 1
                    self._append_log(
                        job_id,
                        f"[{completed}/{total}] 失败 email={item.get('email') or '-'} "
                        f"error={item.get('error') or 'unknown'} mode={item.get('mode') or '-'}",
                        level="error",
                    )
                self._patch_job(
                    job_id,
                    completed=completed,
                    success=success,
                    failed=failed,
                    added=added,
                    items=list(items),
                )

            if index < total and interval > 0 and not cancel.is_set():
                self._append_log(job_id, f"批次间隔 sleep {interval}s")
                time.sleep(interval)

        status = "cancelled" if cancel.is_set() else "done"
        duration = time.time() - t0
        failed_brief = [
            {
                "index": it.get("index"),
                "email": it.get("email"),
                "error": (it.get("error") or "")[:200],
            }
            for it in items
            if not it.get("ok")
        ][:20]
        success_emails = [it.get("email") for it in items if it.get("ok") and it.get("email")][:20]
        summary = {
            "status": status,
            "total": total,
            "completed": completed,
            "success": success,
            "failed": failed,
            "added": added,
            "duration_secs": round(duration, 2),
            "success_rate": round((success / completed) * 100, 1) if completed else 0.0,
            "success_emails": success_emails,
            "failed_items": failed_brief,
            "run_mode": settings.get("run_mode"),
            "mail_provider": settings.get("mail_provider"),
            "executor": settings.get("executor"),
            "engines_dir": settings.get("engines_dir"),
            "push_mode": settings.get("push_mode"),
            "push_enabled": settings.get("push_enabled"),
        }
        self._patch_job(
            job_id,
            status=status,
            finished_at=_now_iso(),
            completed=completed,
            success=success,
            failed=failed,
            added=added,
            items=list(items),
            summary=summary,
        )
        self._append_log(
            job_id,
            "任务结束 "
            f"status={status} completed={completed}/{total} success={success} "
            f"failed={failed} added={added} duration={duration:.1f}s "
            f"success_rate={summary['success_rate']}%",
            level="info" if failed == 0 and status == "done" else "warn",
        )
        if success_emails:
            self._append_log(job_id, "成功邮箱: " + ", ".join(str(e) for e in success_emails))
        if failed_brief:
            for row in failed_brief[:10]:
                self._append_log(
                    job_id,
                    f"失败明细 #{row.get('index')}: {row.get('email') or '-'} | {row.get('error') or '-'}",
                    level="error",
                )
        self._emit_completion_log(
            job_id,
            status=status,
            success=success,
            failed=failed,
            added=added,
            completed=completed,
            total=total,
            duration=duration,
            items=items,
            error=None,
            settings=settings,
            summary=summary,
        )

    def _emit_completion_log(
        self,
        job_id: str,
        *,
        status: str,
        success: int,
        failed: int,
        added: int,
        completed: int,
        total: int,
        duration: float,
        items: list[dict[str, Any]],
        error: str | None,
        settings: dict[str, Any],
        summary: dict[str, Any] | None = None,
    ) -> None:
        """Write a durable completion record for ops troubleshooting."""
        payload = summary or {
            "status": status,
            "success": success,
            "failed": failed,
            "added": added,
            "completed": completed,
            "total": total,
            "duration_secs": round(duration, 2),
            "error": (error or "")[:300] or None,
        }
        # file under data/ for docker volume persistence
        try:
            out_dir = DATA_DIR / "gpt_register_logs"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{job_id}.json"
            record = {
                "job_id": job_id,
                "finished_at": _now_iso(),
                "summary": payload,
                "settings": public_settings(settings),
                "items": [
                    {
                        "index": it.get("index"),
                        "ok": it.get("ok"),
                        "email": it.get("email"),
                        "error": it.get("error"),
                        "added": it.get("added"),
                        "has_token": it.get("has_token"),
                        "mode": it.get("mode"),
                        "logs_tail": it.get("logs_tail") or [],
                    }
                    for it in (items or [])
                ],
            }
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self._append_log(job_id, f"完成日志已写入 data/gpt_register_logs/{job_id}.json")
        except Exception as exc:
            self._append_log(job_id, f"写入完成日志文件失败: {exc}", level="warn")

        # also into main app log stream if available
        try:
            from services.log_service import LOG_TYPE_ACCOUNT, log_service

            log_service.add(
                LOG_TYPE_ACCOUNT,
                f"GPT注册任务结束 {status} success={success} failed={failed} added={added}",
                {
                    "job_id": job_id,
                    "summary": payload,
                    "engines_dir": settings.get("engines_dir"),
                    "mail_provider": settings.get("mail_provider"),
                },
            )
        except Exception:
            pass

    def _validate_engines(self, settings: dict[str, Any]) -> None:
        engines = Path(settings["engines_dir"])
        if not engines.is_dir():
            raise RuntimeError(
                f"注册机目录不存在: {engines}。"
                "请确认仓库内 gpt_free_register/engines 已部署，"
                "或在设置中把 engines_dir 指到可用目录。"
            )
        plugin = engines / "platforms" / "chatgpt" / "plugin.py"
        if not plugin.is_file():
            raise RuntimeError(f"注册机不完整，缺少 ChatGPT 插件: {plugin}")
        if str(settings.get("run_mode") or "inprocess") == "subprocess":
            cli = engines / "register_cli.py"
            if not cli.is_file():
                raise RuntimeError(f"subprocess 模式需要 register_cli.py: {cli}")
            py = self._resolve_python(settings)
            if not Path(py).exists() and py not in {"python3", "python"}:
                raise RuntimeError(f"Python 不存在: {py}")

    def _resolve_python(self, settings: dict[str, Any]) -> str:
        custom = _clean(settings.get("python_bin"))
        if custom:
            return custom
        venv_py = Path(settings["engines_dir"]) / ".venv" / "bin" / "python"
        if venv_py.is_file():
            return str(venv_py)
        return "python3"

    def _register_once(self, settings: dict[str, Any]) -> dict[str, Any]:
        run_mode = str(settings.get("run_mode") or "inprocess").strip().lower()
        if run_mode != "subprocess":
            return self._register_once_inprocess(settings)
        return self._register_once_subprocess(settings)

    def _register_once_inprocess(self, settings: dict[str, Any]) -> dict[str, Any]:
        logs: list[str] = []

        def _log(message: str) -> None:
            text_msg = str(message or "").strip()
            if text_msg:
                logs.append(text_msg[:500])

        try:
            from gpt_free_register.runner import register_chatgpt_once

            parsed = register_chatgpt_once(settings=settings, log=_log)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"内置注册机执行失败: {exc}"[:400],
                "email": None,
                "has_token": False,
                "added": 0,
                "logs": logs[-80:],
            }

        if not isinstance(parsed, dict):
            return {
                "ok": False,
                "error": "内置注册机返回非 dict",
                "email": None,
                "has_token": False,
                "added": 0,
                "logs": logs[-80:],
            }

        email = _clean(parsed.get("email"))
        token = _clean(parsed.get("token")) or _clean((parsed.get("extra") or {}).get("access_token"))
        push = parsed.get("chatgpt2api") if isinstance(parsed.get("chatgpt2api"), dict) else None
        added = 0
        if push and push.get("ok"):
            imp = push.get("import") if isinstance(push.get("import"), dict) else {}
            added = int(imp.get("added") or 0)

        if (
            settings.get("push_enabled")
            and not settings.get("dry_run")
            and token
            and not (push and push.get("ok"))
        ):
            try:
                added = self._import_local(parsed, settings)
                push = {"ok": True, "import": {"added": added, "mode": "local"}}
            except Exception as exc:
                push = {"ok": False, "error": str(exc)[:200]}

        ok = bool(token)
        error = None
        if not ok:
            error = _clean(parsed.get("error")) or _clean(parsed.get("status")) or "no access_token"
            if isinstance(parsed.get("extra"), dict) and parsed["extra"].get("error"):
                error = str(parsed["extra"]["error"])[:300]
        if push and push.get("ok") is False:
            error = (error + "; " if error else "") + str(push.get("error") or "push failed")[:200]
        return {
            "ok": ok,
            "email": email or None,
            "has_token": bool(token),
            "added": added,
            "push": push,
            "error": error,
            "logs": logs[-80:],
            "mode": "inprocess",
        }

    def _register_once_subprocess(self, settings: dict[str, Any]) -> dict[str, Any]:
        engines = Path(settings["engines_dir"])
        py = self._resolve_python(settings)
        cmd = [
            py,
            str(engines / "register_cli.py"),
            "register",
            "chatgpt",
            "--executor",
            str(settings["executor"]),
            "--mail-provider",
            str(settings["mail_provider"]),
        ]
        if settings.get("captcha"):
            cmd.extend(["--captcha", str(settings["captcha"])])
        if settings.get("proxy"):
            cmd.extend(["--proxy", str(settings["proxy"])])

        base_url = str(settings.get("chatgpt2api_base_url") or _default_push_base_url())
        auth_key = _clean(settings.get("chatgpt2api_auth_key")) or _clean(config.auth_key)
        if settings.get("push_enabled"):
            cmd.append("--push-chatgpt2api")
            if settings.get("dry_run"):
                cmd.append("--chatgpt2api-dry-run")
            if settings.get("bind_register_proxy"):
                cmd.append("--chatgpt2api-bind-register-proxy")
            if settings.get("plan_type"):
                cmd.extend(["--chatgpt2api-plan-type", str(settings["plan_type"])])
            if settings.get("source_type"):
                cmd.extend(["--chatgpt2api-source-type", str(settings["source_type"])])
            cmd.extend(["--chatgpt2api-base-url", base_url])
            if auth_key:
                cmd.extend(["--chatgpt2api-auth-key", auth_key])

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        if settings.get("cfd1_domain"):
            env["CFD1_DOMAIN"] = str(settings["cfd1_domain"])
        # latency knobs for subprocess path (inprocess goes through runner.py)
        env["OPENAI_SKIP_CODEX"] = "1" if settings.get("skip_codex", True) else "0"
        if settings.get("register_no_delay"):
            env["OPENAI_REGISTER_NO_DELAY"] = "1"
        so_ms = _clean(settings.get("so_collect_ms"))
        if so_ms:
            env["OPENAI_SO_COLLECT_MS"] = so_ms
        if settings.get("push_enabled") and auth_key:
            env["CHATGPT2API_AUTH_KEY"] = auth_key
            env["CHATGPT2API_BASE_URL"] = base_url

        proc = subprocess.run(
            cmd,
            cwd=str(engines),
            env=env,
            capture_output=True,
            text=True,
            timeout=int(settings["timeout_secs"]),
            check=False,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        logs: list[str] = []
        for stream_name, blob in (("stdout", stdout), ("stderr", stderr)):
            for line in str(blob).splitlines():
                line = line.strip()
                if line:
                    logs.append(f"{stream_name}: {line[:400]}")
        if proc.returncode not in (0, None):
            logs.append(f"returncode={proc.returncode}")
        parsed = _extract_json_object(stdout)
        if not parsed:
            err = (stderr or stdout or f"exit={proc.returncode}")[-400:]
            return {
                "ok": False,
                "error": f"注册输出无法解析: {err}",
                "email": None,
                "has_token": False,
                "added": 0,
                "logs": logs[-80:],
                "mode": "subprocess",
            }

        email = _clean(parsed.get("email"))
        token = _clean(parsed.get("token")) or _clean((parsed.get("extra") or {}).get("access_token"))
        push = parsed.get("chatgpt2api") if isinstance(parsed.get("chatgpt2api"), dict) else None
        added = 0
        if push and push.get("ok"):
            imp = push.get("import") if isinstance(push.get("import"), dict) else {}
            added = int(imp.get("added") or 0)

        # If CLI didn't push but we have token and push_mode local, import in-process
        if (
            settings.get("push_enabled")
            and not settings.get("dry_run")
            and settings.get("push_mode") == "local"
            and token
            and not (push and push.get("ok"))
        ):
            try:
                added = self._import_local(parsed, settings)
                push = {"ok": True, "import": {"added": added, "mode": "local_fallback"}}
            except Exception as exc:
                push = {"ok": False, "error": str(exc)[:200]}

        ok = bool(token)
        error = None
        if not ok:
            error = _clean(parsed.get("status")) or "no access_token"
            # common failure fields
            if isinstance(parsed.get("extra"), dict) and parsed["extra"].get("error"):
                error = str(parsed["extra"]["error"])[:300]
        if push and push.get("ok") is False:
            error = (error + "; " if error else "") + str(push.get("error") or "push failed")[:200]
            # still count as partial ok if token exists
        return {
            "ok": ok,
            "email": email or None,
            "has_token": bool(token),
            "added": added,
            "push": push,
            "error": error,
            "returncode": proc.returncode,
            "logs": logs[-80:],
            "mode": "subprocess",
        }

    def _import_local(self, account: dict[str, Any], settings: dict[str, Any]) -> int:
        """Import registration result into local account_service without HTTP."""
        from services.account_service import account_service
        from services.log_service import LOG_TYPE_ACCOUNT, log_service
        from utils.helper import anonymize_token

        extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
        access = _clean(account.get("token")) or _clean(extra.get("access_token"))
        if not access:
            return 0
        refresh = _clean(extra.get("refresh_token"))
        id_token = _clean(extra.get("id_token"))
        session_only = not bool(refresh)
        payload = {
            "access_token": access,
            "refresh_token": refresh,
            "id_token": id_token,
            "session_token": _clean(extra.get("session_token")),
            "email": _clean(account.get("email")),
            "password": _clean(account.get("password")),
            "account_id": _clean(account.get("user_id")) or _clean(extra.get("account_id")),
            "type": _clean(settings.get("plan_type")) or "free",
            "source_type": _clean(settings.get("source_type"))
            or ("codex" if refresh and id_token else "register"),
            "status": "正常",
            # NextAuth-only fallback has no refresh_token → fragile/session-only.
            "session_only": session_only,
            "fragile": session_only,
        }
        if payload["source_type"] == "codex":
            payload["export_type"] = "codex"
        if settings.get("bind_register_proxy") and settings.get("proxy"):
            payload["proxy"] = settings["proxy"]
        # Default free image quota until remote fetch fills real limits_progress.
        # Without this, quota stays 0 → "no available image quota" even for fresh accounts.
        if payload.get("quota") in (None, "", 0):
            payload["quota"] = int(os.environ.get("GPT_FREE_DEFAULT_IMAGE_QUOTA", "30") or 30)
        result = account_service.add_account_items([payload])
        added = int(result.get("added") or 0)

        # Populate real quota/status/type in background — do not block the register worker
        # (fetch_remote_info can take 25–60s on slow/proxied paths).
        email = payload.get("email") or ""
        token_for_refresh = access

        def _refresh_quota() -> None:
            try:
                token = token_for_refresh
                try:
                    accounts = account_service.list_accounts() or []
                except Exception:
                    accounts = []
                if isinstance(accounts, list):
                    for acc in accounts:
                        if not isinstance(acc, dict):
                            continue
                        if (email and str(acc.get("email") or "") == email) or str(
                            acc.get("access_token") or ""
                        ) == token:
                            token = str(acc.get("access_token") or token)
                            break
                account_service.fetch_remote_info(
                    token,
                    event="gpt_register_import",
                    defer_invalid_removal=True,
                )
            except Exception as exc:
                try:
                    log_service.add(
                        LOG_TYPE_ACCOUNT,
                        "注册入库后刷新额度失败",
                        {
                            "token": anonymize_token(access),
                            "email": email,
                            "session_only": session_only,
                            "error": str(exc)[:300],
                        },
                    )
                except Exception:
                    pass

        try:
            threading.Thread(
                target=_refresh_quota,
                name=f"gpt-reg-quota-{(email or access)[:12]}",
                daemon=True,
            ).start()
        except Exception:
            # Fallback: never fail import if thread spawn fails
            try:
                _refresh_quota()
            except Exception:
                pass
        return added


def _extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    # try whole text
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass
    # find last balanced-looking object from final '{'
    idx = text.rfind("\n{")
    if idx < 0:
        idx = text.rfind("{")
    else:
        idx = idx + 1
    if idx < 0:
        return None
    chunk = text[idx:].strip()
    # trim trailing noise after final }
    last_brace = chunk.rfind("}")
    if last_brace >= 0:
        chunk = chunk[: last_brace + 1]
    try:
        data = json.loads(chunk)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


gpt_register_config = GptRegisterConfig()
gpt_register_service = GptRegisterService(gpt_register_config)
