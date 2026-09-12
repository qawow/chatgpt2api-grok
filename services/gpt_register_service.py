"""Batch ChatGPT free-account registration via any-register-engines.

Runs outside the request thread, pushes successes into the local ChatGPT
account pool (same process account_service when push_mode=local, or HTTP
POST when push_mode=http).
"""
from __future__ import annotations

import json
import os
import random
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

_FP_KEY_ALIASES = {
    "user_agent": "user-agent",
    "user-agent": "user-agent",
    "impersonate": "impersonate",
    "sec_ch_ua": "sec-ch-ua",
    "sec-ch-ua": "sec-ch-ua",
    "sec_ch_ua_mobile": "sec-ch-ua-mobile",
    "sec-ch-ua-mobile": "sec-ch-ua-mobile",
    "sec_ch_ua_platform": "sec-ch-ua-platform",
    "sec-ch-ua-platform": "sec-ch-ua-platform",
    "oai-device-id": "oai-device-id",
    "oai_device_id": "oai-device-id",
    "device_id": "oai-device-id",
    "oai-session-id": "oai-session-id",
    "oai_session_id": "oai-session-id",
}


def _merge_register_fp(base: dict[str, Any], profile: object) -> dict[str, Any]:
    merged = dict(base or {})
    if not isinstance(profile, dict):
        return merged
    for raw_key, value in profile.items():
        dest = _FP_KEY_ALIASES.get(str(raw_key).strip())
        text = str(value or "").strip()
        if dest and text:
            merged[dest] = text
    return merged

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
    "max_per_proxy": 0,
    "stagger_secs": 0.15,
    "interval_secs": 3,
    "timeout_secs": 600,
    "executor": "protocol",
    "mail_provider": "cloudflare_d1_api",
    "captcha": "",
    "proxy": "",  # empty → engines .env REGISTER_PROXY_DEFAULT
    "bind_register_proxy": True,
    "plan_type": "free",
    "source_type": "",
    "cfd1_domain": "",  # optional override CFD1_DOMAIN for this job
    # 域名池：多条按行/逗号分隔，每次注册随机取一个作为 cfd1_domain。
    # 对抗上游按邮箱域名的批量封禁波（实测整池同域名号在同一分钟内集体被吊销）。
    # 所有域名必须已配置 Cloudflare Email Routing catch-all 到同一个 Worker/D1。
    "cfd1_domains": "",
    "push_enabled": True,
    "push_mode": "local",  # local | http
    # empty → auto (local in-process import; http mode uses container-aware default)
    "chatgpt2api_base_url": "",
    "chatgpt2api_auth_key": "",  # empty → config.auth_key
    "dry_run": False,
    # free 号 Codex 二次 OTP 几乎总是 add_phone 失败 → 默认跳过，直接 NextAuth session
    "skip_codex": True,
    # 不再默认入库后自动 Codex：二次 OAuth 会踢掉刚用来生图的 NextAuth session
    "auto_codex_upgrade": False,
    # 步骤间随机抖动（OPENAI_REGISTER_NO_DELAY）；默认保留，批量更稳
    "register_no_delay": False,
    # 覆盖 OPENAI_SO_COLLECT_MS；空=引擎默认 0ms（现网 create_account 不需要 5s collect）
    "so_collect_ms": "",
    # 号池自动补号：可用账号低于阈值时用当前注册配置开任务
    "auto_replenish_enabled": True,
    # 免费号被上游吊销得快（实测约 2 小时），阈值 1 时经常「补一个死一个」，
    # 请求侧就撞 upstream session expired。留 2 个可用号做缓冲。
    "auto_replenish_min_available": 2,
    # 主动维持的目标水位：可用号低于 target 就小步补，而不是等跌破 min 再抢救。
    # target > min_available 时，即使同时死 2 个号也还有 min 可用，避免「全死完」窗口。
    "auto_replenish_target_available": 4,
    "auto_replenish_batch": 1,
    "auto_replenish_interval_secs": 90,
    # 两次「成功」自动补号的最小间隔：把注册时间摊开，死亡时间跟着摊开，
    # 号池各号年龄错开，不会同一时刻集体被吊销。跌破 min 的紧急情况不受此限。
    "auto_replenish_spacing_secs": 600,
    "auto_replenish_fail_cooldown_secs": 600,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: object) -> datetime | None:
    text = _clean(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _clean(value: object) -> str:
    return str(value or "").strip()


def _is_network_register_error(error: object) -> bool:
    """Heuristic: proxy/TLS/timeout failures vs OpenAI business errors."""
    text = str(error or "").lower()
    if not text:
        return False
    markers = (
        "curl: (35)",
        "curl: (7)",
        "curl: (28)",
        "curl: (56)",
        "curl: (55)",
        "curl: (6)",
        "curl: (52)",
        "tls connect",
        "tls handshake",
        "openssl_internal",
        "sslerror",
        "timed out",
        "timeout",
        "connection reset",
        "connection aborted",
        "network is unreachable",
        "name or service not known",
        "could not resolve",
        "socks",
        "proxy error",
        "proxyerror",
        "开始 oauth 流程失败",
        "初始化会话失败",
        "检查 ip",
        "oai_did_missing",
        "invalid_state",
        "403 forbidden",
        "cloudflare",
        "just a moment",
        "csrf token",
    )
    return any(marker in text for marker in markers)


def parse_proxy_pool(text: object) -> list[str]:
    """Split a proxy field into a round-robin pool.

    Accepts newlines, commas, or whitespace-separated URLs. Lines starting
    with ``#`` are comments. Empty input → empty pool (caller uses env default).
    """
    raw = str(text or "").replace(",", "\n")
    out: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def parse_domain_pool(text: object) -> list[str]:
    """Split a domain field into a pool for per-registration random pick.

    Same separators as parse_proxy_pool. Domains are lowercased, leading
    ``@`` stripped. All domains must route (Cloudflare Email Routing
    catch-all) into the same Worker/D1 mailbox the job reads from.
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in parse_proxy_pool(text):
        domain = item.lstrip("@").strip().lower()
        if not domain or "." not in domain:
            continue
        if domain not in seen:
            seen.add(domain)
            out.append(domain)
    return out


_PROXY_SEMS: dict[str, threading.Semaphore] = {}
_PROXY_SEMS_LOCK = threading.Lock()


def _proxy_semaphore(proxy: str, max_per: int) -> threading.Semaphore | None:
    if max_per <= 0:
        return None
    key = f"{max_per}|{proxy or '_default'}"
    with _PROXY_SEMS_LOCK:
        sem = _PROXY_SEMS.get(key)
        if sem is None:
            sem = threading.Semaphore(max_per)
            _PROXY_SEMS[key] = sem
        return sem


def _batch_network_trip(outcomes: list[dict[str, Any]], *, min_fails: int = 5, rate: float = 0.5) -> bool:
    n = len(outcomes)
    if n == 0:
        return False
    net = sum(
        1
        for row in outcomes
        if not row.get("ok") and _is_network_register_error(row.get("error"))
    )
    return net >= min_fails and (net / n) >= rate


def pick_proxy(pool: list[str], index: int) -> str:
    if not pool:
        return ""
    if index <= 0:
        index = 1
    return pool[(index - 1) % len(pool)]


def _proxy_endpoint(proxy: str) -> tuple[str, int] | None:
    from urllib.parse import urlparse

    lines = str(proxy or "").strip().splitlines()
    raw = (lines[0].split(",")[0].strip() if lines else "")
    if not raw or raw.lower() in {"direct", "none", "-"}:
        return None
    parsed = urlparse(raw if "://" in raw else f"socks5h://{raw}")
    host = parsed.hostname
    port = parsed.port
    if not host:
        return None
    if not port:
        port = 1080
    return host, int(port)


def _proxy_tcp_open(host: str, port: int, timeout: float = 1.5) -> bool:
    import socket

    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False


def _local_clash_dir() -> Path:
    candidates = [
        Path("/root/gpt-register-study/data/clash"),
        Path(globals().get("__file__") or ".").resolve().parent.parent / "data" / "clash",
    ]
    for cand in candidates:
        if (cand / "mihomo").is_file():
            return cand
    return candidates[0]


def _ensure_local_mihomo(host: str, port: int) -> str:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return "remote"
    if _proxy_tcp_open(host, port, timeout=0.4):
        return "already_up"
    clash = _local_clash_dir()
    binary = clash / "mihomo"
    cfg = clash / "config.yaml"
    if not binary.is_file() or not cfg.is_file():
        return "no_binary"
    log_f = open(clash / "mihomo.log", "ab")
    subprocess.Popen(
        [str(binary), "-d", str(clash), "-f", str(cfg)],
        stdout=log_f,
        stderr=log_f,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 4.0
    while time.time() < deadline:
        if _proxy_tcp_open(host, port, timeout=0.3):
            return "started"
        time.sleep(0.15)
    return "start_failed"


def _refresh_local_clash_nodes(host: str) -> str:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return ""
    script = _local_clash_dir() / "healthcheck.py"
    if not script.is_file():
        return ""
    import sys

    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        tail = lines[-1] if lines else f"exit={proc.returncode}"
        if proc.returncode != 0:
            return f"节点健康检查失败 {tail}"
        return f"节点健康检查 {tail}"
    except Exception as exc:
        return f"节点健康检查跳过: {exc}"


def preflight_register_proxy(proxy: str) -> str:
    pool = parse_proxy_pool(proxy) if str(proxy or "").strip() else []
    target = pool[0] if pool else str(proxy or "").strip()
    ep = _proxy_endpoint(target)
    if ep is None:
        return "出口预检：直连（未配置代理）"
    host, port = ep
    status = "already_up" if _proxy_tcp_open(host, port) else _ensure_local_mihomo(host, port)
    if status not in {"already_up", "started"} or not _proxy_tcp_open(host, port):
        raise RuntimeError(
            f"注册代理不可达 {host}:{port} (mihomo={status})。"
            "32 并发会在开跑前集体失败，先把 SOCKS 拉起来。"
        )
    health = _refresh_local_clash_nodes(host)
    bits = [f"出口预检通过 {_mask_proxy_url(target)}"]
    if status == "started":
        bits.append("mihomo started")
    if health:
        bits.append(health)
    return "；".join(bits)


def _mask_proxy_url(proxy: object) -> str:
    text = str(proxy or "").strip()
    if not text:
        return ""
    if "@" not in text:
        return text
    scheme, rest = text.split("://", 1) if "://" in text else ("", text)
    creds, host = rest.rsplit("@", 1)
    if ":" in creds:
        user, _password = creds.split(":", 1)
        hidden = f"{user}:***"
    else:
        hidden = "***"
    return f"{scheme}://{hidden}@{host}" if scheme else f"{hidden}@{host}"


def _circuit_break_threshold(settings: dict[str, Any] | None = None) -> int:
    raw = ""
    if settings:
        raw = str(settings.get("circuit_break") or "").strip()
    if not raw:
        raw = str(os.environ.get("GPT_REGISTER_CIRCUIT_BREAK") or "3").strip()
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 3
    return max(0, min(20, n))


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
    out["count"] = _clamp_int(out.get("count"), 1, 1, 128)
    out["concurrency"] = _clamp_int(out.get("concurrency"), 1, 1, 32)
    out["max_per_proxy"] = _clamp_int(out.get("max_per_proxy"), 0, 0, 32)
    out["stagger_secs"] = _clamp_float(out.get("stagger_secs"), 0.15, 0, 5)
    out["interval_secs"] = _clamp_float(out.get("interval_secs"), 3, 0, 600)
    out["timeout_secs"] = _clamp_int(out.get("timeout_secs"), 600, 60, 3600)
    executor = _clean(out.get("executor")).lower() or "protocol"
    if executor != "protocol":
        executor = "protocol"
    out["executor"] = executor
    out["mail_provider"] = "cloudflare_d1_api"
    out["captcha"] = ""
    out["proxy"] = _clean(out.get("proxy"))
    out["bind_register_proxy"] = bool(out.get("bind_register_proxy"))
    out["plan_type"] = _clean(out.get("plan_type")) or "free"
    out["source_type"] = _clean(out.get("source_type"))
    out["cfd1_domain"] = _clean(out.get("cfd1_domain"))
    out["cfd1_domains"] = _clean(out.get("cfd1_domains"))
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
    # default False: auto Codex after import is a second login and kills session
    if "auto_codex_upgrade" not in src:
        out["auto_codex_upgrade"] = False
    else:
        out["auto_codex_upgrade"] = bool(out.get("auto_codex_upgrade"))
    out["register_no_delay"] = bool(out.get("register_no_delay"))
    out["so_collect_ms"] = _clean(out.get("so_collect_ms"))
    if "auto_replenish_enabled" not in src:
        out["auto_replenish_enabled"] = True
    else:
        out["auto_replenish_enabled"] = bool(out.get("auto_replenish_enabled"))
    out["auto_replenish_min_available"] = _clamp_int(
        out.get("auto_replenish_min_available"), 1, 1, 20
    )
    out["auto_replenish_target_available"] = _clamp_int(
        out.get("auto_replenish_target_available"), 4, 1, 20
    )
    if out["auto_replenish_target_available"] < out["auto_replenish_min_available"]:
        out["auto_replenish_target_available"] = out["auto_replenish_min_available"]
    out["auto_replenish_batch"] = _clamp_int(out.get("auto_replenish_batch"), 1, 1, 5)
    out["auto_replenish_interval_secs"] = _clamp_int(
        out.get("auto_replenish_interval_secs"), 90, 30, 3600
    )
    out["auto_replenish_spacing_secs"] = _clamp_int(
        out.get("auto_replenish_spacing_secs"), 600, 0, 7200
    )
    out["auto_replenish_fail_cooldown_secs"] = _clamp_int(
        out.get("auto_replenish_fail_cooldown_secs"), 600, 60, 7200
    )
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
        # Keep last 20 jobs both on disk and in memory to prevent OOM.
        items = sorted(
            self._jobs.values(),
            key=lambda j: str(j.get("created_at") or ""),
            reverse=True,
        )[:20]
        # Trim in-memory dict: remove old completed jobs not in the top 20.
        keep_ids = {str(j.get("job_id")) for j in items}
        for jid in list(self._jobs.keys()):
            if jid not in keep_ids:
                self._jobs.pop(jid, None)
                self._cancel_flags.pop(jid, None)
        # Clean up cancel flags for completed jobs (Events are one-shot; no
        # need to keep them after the job is done).
        for jid, job in list(self._jobs.items()):
            status = str(job.get("status") or "")
            if status in {"done", "failed", "cancelled"}:
                self._cancel_flags.pop(jid, None)
        from utils.atomic import atomic_write_json
        atomic_write_json(GPT_REGISTER_JOBS_FILE, items)

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

    def has_active_job(self) -> bool:
        with self._lock:
            return any(job.get("status") in {"pending", "running"} for job in self._jobs.values())

    def _last_finished_auto_job(self) -> dict[str, Any] | None:
        finished: list[dict[str, Any]] = []
        with self._lock:
            for job in self._jobs.values():
                if job.get("trigger") != "auto_replenish":
                    continue
                if job.get("status") not in {"done", "failed", "cancelled"}:
                    continue
                finished.append(dict(job))
        if not finished:
            return None
        return max(finished, key=lambda job: str(job.get("finished_at") or job.get("updated_at") or ""))

    def maybe_replenish_pool(self) -> dict[str, Any]:
        """Keep the image pool topped up with age-staggered batches.

        低于 target 小步补（一次 batch 个），低于 min 紧急补（无视 spacing）；
        成功补号间隔 spacing 秒，把各号注册/死亡时间摊开，避免集中暴毙。
        """
        settings = normalize_settings(self.config_store.get())
        interval = int(settings["auto_replenish_interval_secs"])
        result: dict[str, Any] = {
            "action": "skip",
            "reason": "",
            "available": 0,
            "min_available": int(settings["auto_replenish_min_available"]),
            "target_available": int(settings["auto_replenish_target_available"]),
            "wait_secs": interval,
        }
        if not settings.get("auto_replenish_enabled"):
            result["reason"] = "disabled"
            return result
        if settings.get("dry_run"):
            result["reason"] = "dry_run"
            return result
        if not settings.get("push_enabled"):
            result["reason"] = "push_disabled"
            return result
        if self.has_active_job():
            result["reason"] = "job_running"
            return result

        from services.account_service import account_service

        available = int(account_service.count_image_available_accounts())
        min_available = int(settings["auto_replenish_min_available"])
        target = max(int(settings["auto_replenish_target_available"]), min_available)
        result["available"] = available
        result["min_available"] = min_available
        result["target_available"] = target
        if available >= target:
            result["reason"] = "stocked"
            return result

        last = self._last_finished_auto_job()
        cooldown = int(settings["auto_replenish_fail_cooldown_secs"])
        spacing = int(settings["auto_replenish_spacing_secs"])
        if last is not None:
            finished_at = _parse_iso(last.get("finished_at"))
            if finished_at is not None:
                elapsed = (datetime.now(timezone.utc) - finished_at).total_seconds()
                added = int(last.get("added") or 0)
                if added <= 0:
                    if elapsed < cooldown:
                        result["reason"] = "fail_cooldown"
                        result["wait_secs"] = max(30, int(cooldown - elapsed))
                        return result
                elif available >= min_available and elapsed < spacing:
                    # 非紧急（还没到硬底线）：摊开来补，拉开各号注册/死亡时间。
                    result["reason"] = "spacing"
                    result["wait_secs"] = max(30, int(spacing - elapsed))
                    return result

        need = min(max(1, target - available), int(settings["auto_replenish_batch"]))
        try:
            job = self.start_job({"count": need}, trigger="auto_replenish")
        except RuntimeError as exc:
            result["reason"] = str(exc)[:120]
            return result

        result["action"] = "started"
        result["reason"] = "below_min" if available < min_available else "below_target"
        result["count"] = need
        result["job_id"] = job.get("job_id")
        try:
            from services.log_service import LOG_TYPE_ACCOUNT, log_service

            log_service.add(
                LOG_TYPE_ACCOUNT,
                "自动补号已启动",
                {
                    "available": available,
                    "min_available": min_available,
                    "target_available": target,
                    "count": need,
                    "job_id": job.get("job_id"),
                },
            )
        except Exception:
            pass
        print(
            f"[account-replenish] start count={need} available={available} "
            f"min={min_available} target={target} job={str(job.get('job_id') or '')[:8]}"
        )
        return result

    def start_job(self, overrides: dict[str, Any] | None = None, *, trigger: str = "manual") -> dict[str, Any]:
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
                "trigger": _clean(trigger) or "manual",
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
        proxy_pool = parse_proxy_pool(settings.get("proxy"))
        domain_pool = parse_domain_pool(settings.get("cfd1_domains"))
        if domain_pool:
            self._append_log(job_id, f"邮箱域名池启用：{len(domain_pool)} 个域名随机轮换")
        if proxy_pool and concurrency > len(proxy_pool):
            self._append_log(
                job_id,
                f"并发 {concurrency} 大于代理池 {len(proxy_pool)}，钳到 {len(proxy_pool)} "
                "（避免多号同出口）",
                level="warn",
            )
            concurrency = len(proxy_pool)
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
            per_settings = dict(settings)
            if proxy_pool:
                per_settings["proxy"] = pick_proxy(proxy_pool, index)
            if domain_pool:
                # random, not index round-robin: auto-replenish jobs are count=1
                # (always index 1), which would pin every registration to pool[0].
                per_settings["cfd1_domain"] = random.choice(domain_pool)
            if concurrency > 1:
                # gpt-auto-register: stagger workers so N accounts don't hit
                # the same egress in the same second.
                time.sleep(0.8 * ((max(index, 1) - 1) % concurrency))
            try:
                result = self._register_once(per_settings)
                prefix_logs: list[str] = []
                if proxy_pool:
                    prefix_logs.append(
                        f"proxy={_mask_proxy_url(per_settings.get('proxy'))} "
                        f"pool={len(proxy_pool)}"
                    )
                if domain_pool:
                    prefix_logs.append(f"mail_domain={per_settings.get('cfd1_domain')}")
                if prefix_logs:
                    result.setdefault("logs", [])
                    if isinstance(result.get("logs"), list):
                        result["logs"] = prefix_logs + list(result["logs"])
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
        consecutive_network = 0
        circuit_threshold = _circuit_break_threshold(settings)
        circuit_tripped = False
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
                    consecutive_network = 0
                    push = item.get("push") if isinstance(item.get("push"), dict) else {}
                    self._append_log(
                        job_id,
                        f"[{completed}/{total}] 成功 email={item.get('email') or '-'} "
                        f"added={item['added']} has_token={item['has_token']} "
                        f"mode={item.get('mode') or '-'} push_ok={push.get('ok')}",
                    )
                else:
                    failed += 1
                    if _is_network_register_error(item.get("error")):
                        consecutive_network += 1
                    else:
                        consecutive_network = 0
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

            if (
                circuit_threshold
                and consecutive_network >= circuit_threshold
                and index < total
                and not cancel.is_set()
            ):
                circuit_tripped = True
                self._append_log(
                    job_id,
                    f"连续 {consecutive_network} 次网络错误，熔断停止后续注册 "
                    f"(GPT_REGISTER_CIRCUIT_BREAK={circuit_threshold})",
                    level="error",
                )
                break

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
            "circuit_break": circuit_tripped,
            "circuit_break_threshold": circuit_threshold,
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
        # Match register TLS profile so first image gen does not curl(35) on chrome110.
        payload["impersonate"] = "chrome142"
        payload["fp"] = {
            "impersonate": "chrome142",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/142.0.7540.34 Safari/537.36"
            ),
            "sec-ch-ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
        device_id = _clean(extra.get("oai-device-id")) or _clean(extra.get("device_id"))
        if device_id:
            payload["oai-device-id"] = device_id
            payload["fp"]["oai-device-id"] = device_id
        payload["fp"] = _merge_register_fp(payload["fp"], extra.get("profile") or extra.get("fp"))
        if payload["fp"].get("impersonate"):
            payload["impersonate"] = payload["fp"]["impersonate"]
        if payload["fp"].get("oai-device-id") and not payload.get("oai-device-id"):
            payload["oai-device-id"] = payload["fp"]["oai-device-id"]
        if payload["fp"].get("oai-session-id"):
            payload["oai-session-id"] = payload["fp"]["oai-session-id"]
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
        password = payload.get("password") or ""

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

        # Never auto-schedule Codex after import. skip_codex=false still often
        # lands as NextAuth session (Codex add_phone); a follow-up
        # authorize/continue on the same email kicks that session in ~minutes.
        # Manual 号池「Codex 补 refresh」 remains available.
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
