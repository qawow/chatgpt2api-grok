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

DEFAULT_SETTINGS: dict[str, Any] = {
    "engines_dir": "/root/any-register-engines",
    "python_bin": "",  # empty → <engines_dir>/.venv/bin/python or python3
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
    "chatgpt2api_base_url": "http://127.0.0.1:8000",
    "chatgpt2api_auth_key": "",  # empty → config.auth_key
    "dry_run": False,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: object) -> str:
    return str(value or "").strip()


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


def normalize_settings(raw: object | None) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_SETTINGS)
    out.update({k: src[k] for k in DEFAULT_SETTINGS if k in src})
    out["engines_dir"] = _clean(out.get("engines_dir")) or DEFAULT_SETTINGS["engines_dir"]
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
    out["chatgpt2api_base_url"] = _clean(out.get("chatgpt2api_base_url")) or "http://127.0.0.1:8000"
    out["chatgpt2api_auth_key"] = _clean(out.get("chatgpt2api_auth_key"))
    out["dry_run"] = bool(out.get("dry_run"))
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

    def _append_log(self, job_id: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            logs = list(job.get("logs") or [])
            logs.append({"at": _now_iso(), "message": message[:500]})
            job["logs"] = logs[-200:]
            job["updated_at"] = _now_iso()
            self._jobs[job_id] = job

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
        self._patch_job(job_id, status="running", started_at=_now_iso())
        self._append_log(job_id, f"任务开始：count={settings['count']} concurrency={settings['concurrency']}")

        total = int(settings["count"])
        concurrency = int(settings["concurrency"])
        interval = float(settings["interval_secs"])
        success = failed = added = completed = 0
        items: list[dict[str, Any]] = []

        try:
            self._validate_engines(settings)
        except Exception as exc:
            self._patch_job(
                job_id,
                status="failed",
                error=str(exc)[:300],
                finished_at=_now_iso(),
            )
            self._append_log(job_id, f"启动失败：{exc}")
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
                }

        # sequential with optional limited concurrency batches
        index = 0
        while index < total:
            if cancel.is_set():
                self._append_log(job_id, "收到取消请求，停止后续注册")
                break
            batch_size = min(concurrency, total - index)
            batch_indexes = list(range(index + 1, index + batch_size + 1))
            index += batch_size

            if concurrency <= 1:
                outcomes = [one(batch_indexes[0])]
            else:
                with ThreadPoolExecutor(max_workers=batch_size) as pool:
                    futs = [pool.submit(one, i) for i in batch_indexes]
                    outcomes = [f.result() for f in as_completed(futs)]
                    outcomes.sort(key=lambda x: int(x.get("index") or 0))

            for outcome in outcomes:
                completed += 1
                item = {
                    "index": outcome.get("index"),
                    "ok": bool(outcome.get("ok")),
                    "email": outcome.get("email"),
                    "error": outcome.get("error"),
                    "added": int(outcome.get("added") or 0),
                    "has_token": bool(outcome.get("has_token")),
                    "push": outcome.get("push"),
                }
                items.append(item)
                if item["ok"]:
                    success += 1
                    added += int(item["added"] or 0)
                    self._append_log(
                        job_id,
                        f"[{completed}/{total}] 成功 {item.get('email') or ''} added={item['added']}",
                    )
                else:
                    failed += 1
                    self._append_log(
                        job_id,
                        f"[{completed}/{total}] 失败 {item.get('email') or ''} {item.get('error') or ''}",
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
                time.sleep(interval)

        status = "cancelled" if cancel.is_set() else "done"
        self._patch_job(
            job_id,
            status=status,
            finished_at=_now_iso(),
            completed=completed,
            success=success,
            failed=failed,
            added=added,
            items=list(items),
        )
        self._append_log(
            job_id,
            f"任务结束 status={status} success={success} failed={failed} added={added}",
        )

    def _validate_engines(self, settings: dict[str, Any]) -> None:
        engines = Path(settings["engines_dir"])
        if not engines.is_dir():
            raise RuntimeError(f"注册机目录不存在: {engines}")
        cli = engines / "register_cli.py"
        if not cli.is_file():
            raise RuntimeError(f"找不到 register_cli.py: {cli}")
        py = self._resolve_python(settings)
        if not Path(py).exists() and py in {"python3", "python"}:
            return
        if not Path(py).exists():
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

        base_url = str(settings.get("chatgpt2api_base_url") or "http://127.0.0.1:8000")
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
        parsed = _extract_json_object(stdout)
        if not parsed:
            err = (stderr or stdout or f"exit={proc.returncode}")[-400:]
            return {
                "ok": False,
                "error": f"注册输出无法解析: {err}",
                "email": None,
                "has_token": False,
                "added": 0,
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
        }

    def _import_local(self, account: dict[str, Any], settings: dict[str, Any]) -> int:
        """Import registration result into local account_service without HTTP."""
        from services.account_service import account_service

        extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
        access = _clean(account.get("token")) or _clean(extra.get("access_token"))
        if not access:
            return 0
        payload = {
            "access_token": access,
            "refresh_token": _clean(extra.get("refresh_token")),
            "id_token": _clean(extra.get("id_token")),
            "session_token": _clean(extra.get("session_token")),
            "email": _clean(account.get("email")),
            "password": _clean(account.get("password")),
            "account_id": _clean(account.get("user_id")) or _clean(extra.get("account_id")),
            "type": _clean(settings.get("plan_type")) or "free",
            "source_type": _clean(settings.get("source_type"))
            or ("codex" if _clean(extra.get("refresh_token")) and _clean(extra.get("id_token")) else "register"),
            "status": "正常",
        }
        if payload["source_type"] == "codex":
            payload["export_type"] = "codex"
        if settings.get("bind_register_proxy") and settings.get("proxy"):
            payload["proxy"] = settings["proxy"]
        result = account_service.add_account_items([payload])
        return int(result.get("added") or 0)


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
