from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import os
import sys
from pathlib import Path
import time

from services.storage.base import StorageBackend

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.json"
VERSION_FILE = BASE_DIR / "VERSION"
BACKUP_STATE_FILE = DATA_DIR / "backup_state.json"

DEFAULT_BACKUP_INCLUDE = {
    "config": True,
    "logs": True,
    "image_tasks": True,
    "accounts_snapshot": True,
    "auth_keys_snapshot": True,
    "images": False,
}

DEFAULT_IMAGE_STORAGE = {
    "enabled": False,
    "mode": "local",
    "webdav_url": "",
    "webdav_username": "",
    "webdav_password": "",
    "webdav_root_path": "chatgpt2api/images",
    "public_base_url": "",
}

DEFAULT_CHAT_COMPLETION_CACHE = {
    "enabled": True,
    "ttl_seconds": 60,
    "max_entries": 256,
    "dedupe_inflight": True,
    "stream_cache": True,
    "normalize_messages": True,
    "drop_adjacent_duplicates": True,
    "drop_assistant_history": False,
}

DEFAULT_PROXY_RUNTIME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

DEFAULT_PROXY_RUNTIME = {
    "enabled": False,
    "egress_mode": "direct",
    "proxy_url": "",
    "resource_proxy_url": "",
    "skip_ssl_verify": False,
    "reset_session_status_codes": [403],
    "clearance": {
        "enabled": False,
        "mode": "none",
        "cf_cookies": "",
        "cf_clearance": "",
        "user_agent": DEFAULT_PROXY_RUNTIME_USER_AGENT,
        "browser": "chrome",
        "flaresolverr_url": "",
        "timeout_sec": 60,
        "refresh_interval": 3600,
        "warm_up_on_start": False,
    },
}

DEFAULT_THIRD_PARTY_APPS = {
    "infinite_canvas": {
        "enabled": False,
        "url": "https://canvas.best",
    },
}

DEFAULT_WAIFU2X = {
    "base_url": "https://www.waifu2x.net",
    "timeout_sec": 180,
    "capsolver_key": "",
    "twocaptcha_key": "",
    "yescaptcha_key": "",
    "ses_id": "",
    "user_agent": DEFAULT_PROXY_RUNTIME_USER_AGENT,
}

DEFAULT_DOUBAO = {
    "base_url": "https://www.doubao.com",
    "timeout_sec": 180,
    "aid": "497858",
    "cookies": "",
    "a_bogus": "",
    "captcha_site_key": "",
    "captcha_task_type": "",
    "capsolver_key": "",
    "twocaptcha_key": "",
    "yescaptcha_key": "",
    "solve_url": "",
    "user_agent": DEFAULT_PROXY_RUNTIME_USER_AGENT,
}

DEFAULT_ZHITU360 = {
    "base_url": "https://image.360.com",
    "timeout_sec": 180,
    "poll_interval_sec": 2,
    "cookies": "",
    "api_user": "chacha",
    "feature": "tools_text2image",
    "srcg": "360_pic",
    "captcha_site_key": "",
    "capsolver_key": "",
    "twocaptcha_key": "",
    "yescaptcha_key": "",
    "solve_url": "",
    "user_agent": DEFAULT_PROXY_RUNTIME_USER_AGENT,
}


def _normalize_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return default
    if value is None:
        return default
    return bool(value)


def _normalize_positive_int(value: object, default: int, minimum: int = 0) -> int:
    try:
        normalized = int(value)
    except (OverflowError, TypeError, ValueError):
        normalized = default
    return max(minimum, normalized)


def _normalize_backup_include(value: object) -> dict[str, bool]:
    source = value if isinstance(value, dict) else {}
    normalized = dict(DEFAULT_BACKUP_INCLUDE)
    for key in normalized:
        normalized[key] = _normalize_bool(source.get(key), normalized[key])
    return normalized


def _normalize_backup_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "enabled": _normalize_bool(source.get("enabled"), False),
        "provider": "cloudflare_r2",
        "account_id": str(source.get("account_id") or "").strip(),
        "access_key_id": str(source.get("access_key_id") or "").strip(),
        "secret_access_key": str(source.get("secret_access_key") or "").strip(),
        "bucket": str(source.get("bucket") or "").strip(),
        "prefix": str(source.get("prefix") or "backups").strip().strip("/") or "backups",
        "interval_minutes": _normalize_positive_int(source.get("interval_minutes"), 360, 1),
        "rotation_keep": _normalize_positive_int(source.get("rotation_keep"), 10, 0),
        "encrypt": _normalize_bool(source.get("encrypt"), False),
        "passphrase": str(source.get("passphrase") or "").strip(),
        "include": _normalize_backup_include(source.get("include")),
    }


def _normalize_backup_state(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "last_started_at": str(source.get("last_started_at") or "").strip() or None,
        "last_finished_at": str(source.get("last_finished_at") or "").strip() or None,
        "last_status": str(source.get("last_status") or "idle").strip() or "idle",
        "last_error": str(source.get("last_error") or "").strip() or None,
        "last_object_key": str(source.get("last_object_key") or "").strip() or None,
    }


def _normalize_image_storage_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    mode = str(source.get("mode") or "local").strip().lower()
    if mode not in {"local", "webdav", "both"}:
        mode = "local"
    enabled = _normalize_bool(source.get("enabled"), False)
    if not enabled:
        mode = "local"
    root_path = str(source.get("webdav_root_path") or DEFAULT_IMAGE_STORAGE["webdav_root_path"]).strip().strip("/")
    return {
        "enabled": enabled,
        "mode": mode,
        "webdav_url": str(source.get("webdav_url") or "").strip().rstrip("/"),
        "webdav_username": str(source.get("webdav_username") or "").strip(),
        "webdav_password": str(source.get("webdav_password") or "").strip(),
        "webdav_root_path": root_path or str(DEFAULT_IMAGE_STORAGE["webdav_root_path"]),
        "public_base_url": str(source.get("public_base_url") or "").strip().rstrip("/"),
    }


def _normalize_chat_completion_cache_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "enabled": _normalize_bool(source.get("enabled"), DEFAULT_CHAT_COMPLETION_CACHE["enabled"]),
        "ttl_seconds": _normalize_positive_int(
            source.get("ttl_seconds"),
            int(DEFAULT_CHAT_COMPLETION_CACHE["ttl_seconds"]),
            0,
        ),
        "max_entries": _normalize_positive_int(
            source.get("max_entries"),
            int(DEFAULT_CHAT_COMPLETION_CACHE["max_entries"]),
            1,
        ),
        "dedupe_inflight": _normalize_bool(
            source.get("dedupe_inflight"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["dedupe_inflight"]),
        ),
        "stream_cache": _normalize_bool(
            source.get("stream_cache"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["stream_cache"]),
        ),
        "normalize_messages": _normalize_bool(
            source.get("normalize_messages"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["normalize_messages"]),
        ),
        "drop_adjacent_duplicates": _normalize_bool(
            source.get("drop_adjacent_duplicates"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["drop_adjacent_duplicates"]),
        ),
        "drop_assistant_history": _normalize_bool(
            source.get("drop_assistant_history"),
            bool(DEFAULT_CHAT_COMPLETION_CACHE["drop_assistant_history"]),
        ),
    }


def _normalize_status_codes(value: object) -> list[int]:
    items = value if isinstance(value, list) else DEFAULT_PROXY_RUNTIME["reset_session_status_codes"]
    normalized: list[int] = []
    for item in items:
        if isinstance(item, bool):
            continue
        try:
            status = int(item)
        except (OverflowError, TypeError, ValueError):
            continue
        if 100 <= status <= 599 and status not in normalized:
            normalized.append(status)
    if not normalized:
        return list(DEFAULT_PROXY_RUNTIME["reset_session_status_codes"])
    return normalized


def _normalize_proxy_runtime_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    default_clearance = DEFAULT_PROXY_RUNTIME["clearance"]
    clearance_source = source.get("clearance") if isinstance(source.get("clearance"), dict) else {}

    egress_mode = str(source.get("egress_mode") or DEFAULT_PROXY_RUNTIME["egress_mode"]).strip().lower()
    if egress_mode not in {"direct", "single_proxy"}:
        egress_mode = str(DEFAULT_PROXY_RUNTIME["egress_mode"])

    clearance_mode = str(clearance_source.get("mode") or default_clearance["mode"]).strip().lower()
    if clearance_mode not in {"none", "manual", "flaresolverr"}:
        clearance_mode = str(default_clearance["mode"])

    user_agent = str(clearance_source.get("user_agent") or default_clearance["user_agent"]).strip()
    browser = str(clearance_source.get("browser") or default_clearance["browser"]).strip()

    existing_clearance_cookies = str(source.get("_existing_cf_cookies") or "").strip()
    existing_cf_clearance = str(source.get("_existing_cf_clearance") or "").strip()
    cf_cookies = str(clearance_source.get("cf_cookies") or "").strip()
    cf_clearance = str(clearance_source.get("cf_clearance") or "").strip()
    if not cf_cookies and _normalize_bool(clearance_source.get("has_cf_cookies"), False):
        cf_cookies = existing_clearance_cookies
    if not cf_clearance and _normalize_bool(clearance_source.get("has_cf_clearance"), False):
        cf_clearance = existing_cf_clearance

    return {
        "enabled": _normalize_bool(source.get("enabled"), bool(DEFAULT_PROXY_RUNTIME["enabled"])),
        "egress_mode": egress_mode,
        "proxy_url": str(source.get("proxy_url") or "").strip(),
        "resource_proxy_url": str(source.get("resource_proxy_url") or "").strip(),
        "skip_ssl_verify": _normalize_bool(
            source.get("skip_ssl_verify"),
            bool(DEFAULT_PROXY_RUNTIME["skip_ssl_verify"]),
        ),
        "reset_session_status_codes": _normalize_status_codes(source.get("reset_session_status_codes")),
        "clearance": {
            "enabled": _normalize_bool(clearance_source.get("enabled"), bool(default_clearance["enabled"])),
            "mode": clearance_mode,
            "cf_cookies": cf_cookies,
            "cf_clearance": cf_clearance,
            "user_agent": user_agent or str(default_clearance["user_agent"]),
            "browser": browser or str(default_clearance["browser"]),
            "flaresolverr_url": str(clearance_source.get("flaresolverr_url") or "").strip(),
            "timeout_sec": _normalize_positive_int(
                clearance_source.get("timeout_sec"),
                int(default_clearance["timeout_sec"]),
                1,
            ),
            "refresh_interval": _normalize_positive_int(
                clearance_source.get("refresh_interval"),
                int(default_clearance["refresh_interval"]),
                60,
            ),
            "warm_up_on_start": _normalize_bool(
                clearance_source.get("warm_up_on_start"),
                bool(default_clearance["warm_up_on_start"]),
            ),
        },
    }


def _normalize_third_party_apps_settings(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    canvas_source = source.get("infinite_canvas") if isinstance(source.get("infinite_canvas"), dict) else {}
    return {
        "infinite_canvas": {
            "enabled": _normalize_bool(canvas_source.get("enabled"), False),
            "url": str(canvas_source.get("url") or DEFAULT_THIRD_PARTY_APPS["infinite_canvas"]["url"]).strip(),
        },
    }


def _env_or(
    source: dict[str, object], key: str, env_name: str, default: str = "", *, use_env: bool = True
) -> str:
    """Resolve a setting, preferring the environment.

    ``use_env=False`` is for the write path: these normalizers run both when
    reading settings (where the env should win) and when persisting them (where
    it must not, or a single "save settings" bakes every .env secret into
    config.json — and from there into the backup archive).
    """
    if use_env:
        env_value = str(os.getenv(env_name) or "").strip()
        if env_value:
            return env_value
    if source.get(key) is not None and str(source.get(key) or "").strip():
        return str(source.get(key) or "").strip()
    return default


def _env_int_source(env_name: str, source: dict[str, object], key: str, *, use_env: bool = True) -> object:
    if use_env:
        raw = os.getenv(env_name)
        if str(raw or "").strip():
            return raw
    return source.get(key)


# Single source of truth for which fields get masked on the way out and
# restored on the way in. Keeping these apart is how waifu2x ended up with a
# solve_url that GET never masked.
_WAIFU2X_SECRET_KEYS = ("capsolver_key", "twocaptcha_key", "yescaptcha_key", "ses_id", "solve_url")
_DOUBAO_SECRET_KEYS = (
    "cookies",
    "a_bogus",
    "capsolver_key",
    "twocaptcha_key",
    "yescaptcha_key",
    "solve_url",
)
_ZHITU360_SECRET_KEYS = ("cookies", "capsolver_key", "twocaptcha_key", "yescaptcha_key", "solve_url")


def _normalize_waifu2x_settings(value: object, *, use_env: bool = True) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    base = _env_or(source, "base_url", "WAIFU2X_BASE_URL", str(DEFAULT_WAIFU2X["base_url"]), use_env=use_env)
    timeout_source = _env_int_source("WAIFU2X_TIMEOUT_SEC", source, "timeout_sec", use_env=use_env)
    return {
        "base_url": (base or str(DEFAULT_WAIFU2X["base_url"])).rstrip("/"),
        "timeout_sec": _normalize_positive_int(timeout_source, int(DEFAULT_WAIFU2X["timeout_sec"]), 10),
        "capsolver_key": _env_or(source, "capsolver_key", "WAIFU2X_CAPSOLVER_KEY", use_env=use_env),
        "twocaptcha_key": _env_or(source, "twocaptcha_key", "WAIFU2X_TWOCAPTCHA_KEY", use_env=use_env),
        "yescaptcha_key": _env_or(source, "yescaptcha_key", "WAIFU2X_YESCAPTCHA_KEY", use_env=use_env),
        "ses_id": _env_or(source, "ses_id", "WAIFU2X_SES_ID", use_env=use_env),
        "solve_url": _env_or(
            source, "solve_url", "WAIFU2X_SOLVE_URL", (os.getenv("CAPTCHA_SOLVE_URL") or "") if use_env else "",
            use_env=use_env,
        ),
        "user_agent": _env_or(
            source,
            "user_agent",
            "WAIFU2X_USER_AGENT",
            str(DEFAULT_WAIFU2X["user_agent"]),
            use_env=use_env,
        ),
    }


def _captcha_keys(source: dict, prefix: str, *, use_env: bool = True) -> dict[str, str]:
    def _fallback(name: str) -> str:
        return (os.getenv(name) or "") if use_env else ""

    return {
        "capsolver_key": _env_or(
            source, "capsolver_key", f"{prefix}_CAPSOLVER_KEY", _fallback("CAPSOLVER_KEY"), use_env=use_env
        ),
        "twocaptcha_key": _env_or(
            source, "twocaptcha_key", f"{prefix}_TWOCAPTCHA_KEY", _fallback("TWOCAPTCHA_KEY"), use_env=use_env
        ),
        "yescaptcha_key": _env_or(
            source, "yescaptcha_key", f"{prefix}_YESCAPTCHA_KEY", _fallback("YESCAPTCHA_KEY"), use_env=use_env
        ),
        "solve_url": _env_or(
            source, "solve_url", f"{prefix}_SOLVE_URL", _fallback("CAPTCHA_SOLVE_URL"), use_env=use_env
        ),
    }


def _normalize_doubao_settings(value: object, *, use_env: bool = True) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    timeout_source = _env_int_source("DOUBAO_TIMEOUT_SEC", source, "timeout_sec", use_env=use_env)
    out: dict[str, object] = {
        "base_url": (
            _env_or(source, "base_url", "DOUBAO_BASE_URL", str(DEFAULT_DOUBAO["base_url"]), use_env=use_env)
            or DEFAULT_DOUBAO["base_url"]
        ).rstrip("/"),
        "timeout_sec": _normalize_positive_int(timeout_source, int(DEFAULT_DOUBAO["timeout_sec"]), 10),
        "aid": _env_or(source, "aid", "DOUBAO_AID", str(DEFAULT_DOUBAO["aid"]), use_env=use_env),
        "cookies": _env_or(source, "cookies", "DOUBAO_COOKIES", use_env=use_env),
        "a_bogus": _env_or(source, "a_bogus", "DOUBAO_A_BOGUS", use_env=use_env),
        "captcha_site_key": _env_or(source, "captcha_site_key", "DOUBAO_CAPTCHA_SITE_KEY", use_env=use_env),
        "captcha_task_type": _env_or(source, "captcha_task_type", "DOUBAO_CAPTCHA_TASK_TYPE", use_env=use_env),
        "user_agent": _env_or(
            source, "user_agent", "DOUBAO_USER_AGENT", str(DEFAULT_DOUBAO["user_agent"]), use_env=use_env
        ),
    }
    out.update(_captcha_keys(source, "DOUBAO", use_env=use_env))
    return out


def _normalize_zhitu360_settings(value: object, *, use_env: bool = True) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    timeout_source = _env_int_source("ZHITU360_TIMEOUT_SEC", source, "timeout_sec", use_env=use_env)
    poll_source = _env_int_source("ZHITU360_POLL_INTERVAL_SEC", source, "poll_interval_sec", use_env=use_env)
    out: dict[str, object] = {
        "base_url": (
            _env_or(source, "base_url", "ZHITU360_BASE_URL", str(DEFAULT_ZHITU360["base_url"]), use_env=use_env)
            or DEFAULT_ZHITU360["base_url"]
        ).rstrip("/"),
        "timeout_sec": _normalize_positive_int(timeout_source, int(DEFAULT_ZHITU360["timeout_sec"]), 10),
        "poll_interval_sec": _normalize_positive_int(poll_source, int(DEFAULT_ZHITU360["poll_interval_sec"]), 1),
        "cookies": _env_or(source, "cookies", "ZHITU360_COOKIES", use_env=use_env),
        "api_user": _env_or(
            source, "api_user", "ZHITU360_API_USER", str(DEFAULT_ZHITU360["api_user"]), use_env=use_env
        ),
        "feature": _env_or(source, "feature", "ZHITU360_FEATURE", str(DEFAULT_ZHITU360["feature"]), use_env=use_env),
        "srcg": _env_or(source, "srcg", "ZHITU360_SRCG", str(DEFAULT_ZHITU360["srcg"]), use_env=use_env),
        "captcha_site_key": _env_or(source, "captcha_site_key", "ZHITU360_CAPTCHA_SITE_KEY", use_env=use_env),
        # 360 does not use Turnstile; without this the solver falls back to the
        # per-provider default and hands the platform the wrong task type.
        "captcha_task_type": _env_or(
            source, "captcha_task_type", "ZHITU360_CAPTCHA_TASK_TYPE", use_env=use_env
        ),
        "user_agent": _env_or(
            source, "user_agent", "ZHITU360_USER_AGENT", str(DEFAULT_ZHITU360["user_agent"]), use_env=use_env
        ),
    }
    out.update(_captcha_keys(source, "ZHITU360", use_env=use_env))
    return out


def _validate_image_storage_settings(settings: dict[str, object]) -> None:
    if not _normalize_bool(settings.get("enabled"), False):
        return
    if not str(settings.get("webdav_url") or "").strip():
        raise ValueError("启用 WebDAV 图片存储后必须填写 WebDAV URL")
    if not str(settings.get("webdav_password") or "").strip():
        raise ValueError("启用 WebDAV 图片存储后必须填写 WebDAV 密码")


@dataclass(frozen=True)
class LoadedSettings:
    auth_key: str
    refresh_account_interval_minute: int


def _normalize_auth_key(value: object) -> str:
    return str(value or "").strip()


def _is_invalid_auth_key(value: object) -> bool:
    return _normalize_auth_key(value) == ""


def _read_json_object(path: Path, *, name: str) -> dict[str, object]:
    if not path.exists():
        return {}
    if path.is_dir():
        print(
            f"Warning: {name} at '{path}' is a directory, ignoring it and falling back to other configuration sources.",
            file=sys.stderr,
        )
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_settings() -> LoadedSettings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw_config = _read_json_object(CONFIG_FILE, name="config.json")
    auth_key = _normalize_auth_key(os.getenv("CHATGPT2API_AUTH_KEY") or raw_config.get("auth-key"))
    if _is_invalid_auth_key(auth_key):
        raise ValueError(
            "❌ auth-key 未设置！\n"
            "请在环境变量 CHATGPT2API_AUTH_KEY 中设置，或者在 config.json 中填写 auth-key。"
        )

    try:
        refresh_interval = int(raw_config.get("refresh_account_interval_minute", 5))
    except (TypeError, ValueError):
        refresh_interval = 5

    return LoadedSettings(
        auth_key=auth_key,
        refresh_account_interval_minute=refresh_interval,
    )


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.data = self._load()
        self._storage_backend: StorageBackend | None = None
        if _is_invalid_auth_key(self.auth_key):
            raise ValueError(
                "❌ auth-key 未设置！\n"
                "请按以下任意一种方式解决：\n"
                "1. 在 Render 的 Environment 变量中添加：\n"
                "   CHATGPT2API_AUTH_KEY = your_real_auth_key\n"
                "2. 或者在 config.json 中填写：\n"
                '   "auth-key": "your_real_auth_key"'
            )

    def _load(self) -> dict[str, object]:
        return _read_json_object(self.path, name="config.json")

    def _save(self) -> None:
        from utils.atomic import atomic_write_json

        atomic_write_json(self.path, self.data)

    @property
    def auth_key(self) -> str:
        return _normalize_auth_key(os.getenv("CHATGPT2API_AUTH_KEY") or self.data.get("auth-key"))

    @property
    def accounts_file(self) -> Path:
        return DATA_DIR / "accounts.json"

    @property
    def grok_accounts_file(self) -> Path:
        """Isolated Grok/xAI pool file — never share with ChatGPT accounts.json."""
        return DATA_DIR / "grok_accounts.json"

    def get_grok_settings(self) -> dict[str, object]:
        raw = self.data.get("grok")
        return dict(raw) if isinstance(raw, dict) else {}

    def get_waifu2x_settings(self) -> dict[str, object]:
        return _normalize_waifu2x_settings(self.data.get("waifu2x"))

    def get_doubao_settings(self) -> dict[str, object]:
        return _normalize_doubao_settings(self.data.get("doubao"))

    def get_zhitu360_settings(self) -> dict[str, object]:
        return _normalize_zhitu360_settings(self.data.get("zhitu360"))

    @property
    def refresh_account_interval_minute(self) -> int:
        try:
            return int(self.data.get("refresh_account_interval_minute", 5))
        except (TypeError, ValueError):
            return 5

    @property
    def image_retention_days(self) -> int:
        try:
            return max(1, int(self.data.get("image_retention_days", 30)))
        except (TypeError, ValueError):
            return 30

    @property
    def image_poll_timeout_secs(self) -> int:
        try:
            return max(1, int(self.data.get("image_poll_timeout_secs", 120)))
        except (TypeError, ValueError):
            return 120

    @property
    def image_sse_idle_timeout_secs(self) -> float:
        """Max seconds with no SSE body data before aborting a hung image stream."""
        try:
            return max(5.0, float(self.data.get("image_sse_idle_timeout_secs", 90.0)))
        except (TypeError, ValueError):
            return 90.0

    @property
    def image_sse_total_timeout_secs(self) -> float:
        """Hard wall-clock budget for a single image SSE stream."""
        try:
            return max(30.0, float(self.data.get("image_sse_total_timeout_secs", 420.0)))
        except (TypeError, ValueError):
            return 420.0

    @property
    def image_task_timeout_secs(self) -> float:
        """Hard wall-clock for a whole image task thread (SSE + poll + download)."""
        try:
            return max(1.0, float(self.data.get("image_task_timeout_secs", 600.0)))
        except (TypeError, ValueError):
            return 600.0

    @property
    def image_poll_interval_secs(self) -> float:
        try:
            return max(0.5, float(self.data.get("image_poll_interval_secs", 5.0)))
        except (TypeError, ValueError):
            return 5.0

    @property
    def image_poll_initial_wait_secs(self) -> float:
        """Image generation upstream takes ~30s; polling immediately wastes requests
        and trips a transient 429. Default 4s lets the conversation document commit
        before the first poll without waiting a full 6–10s."""
        try:
            return max(0.0, float(self.data.get("image_poll_initial_wait_secs", 4.0)))
        except (TypeError, ValueError):
            return 4.0

    @property
    def image_account_concurrency(self) -> int:
        try:
            return max(1, int(self.data.get("image_account_concurrency", 3)))
        except (TypeError, ValueError):
            return 3

    @property
    def image_account_failover_retries(self) -> int:
        """Maximum account changes for one image before returning an error."""
        try:
            return max(1, min(20, int(self.data.get("image_account_failover_retries", 4))))
        except (TypeError, ValueError):
            return 4

    @property
    def image_poll_failover_retries(self) -> int:
        try:
            return max(0, min(20, int(self.data.get("image_poll_failover_retries", 4))))
        except (TypeError, ValueError):
            return 4

    @property
    def image_text_failover_retries(self) -> int:
        try:
            return max(0, min(20, int(self.data.get("image_text_failover_retries", 3))))
        except (TypeError, ValueError):
            return 3

    @property
    def image_transient_failure_cooldown_secs(self) -> int:
        """Task-local cooldown for a transiently failed credential; 0 disables it."""
        try:
            return max(0, min(3600, int(self.data.get("image_transient_failure_cooldown_secs", 60))))
        except (TypeError, ValueError):
            return 60

    @property
    def image_parallel_generation(self) -> bool:
        value = self.data.get("image_parallel_generation", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_settle_enabled(self) -> bool:
        """图片二次确认机制：找到 file_ids 后等待一段时间再次确认。"""
        value = self.data.get("image_settle_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_check_before_hit_enabled(self) -> bool:
        """先check再hit：通过轮询确认 file_ids 存在后再返回，而非仅依赖 SSE 事件。"""
        value = self.data.get("image_check_before_hit_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_remove_conversation_after_result(self) -> bool:
        """出图成功后异步隐藏 ChatGPT 本地对话记录。"""
        value = self.data.get("image_remove_conversation_after_result", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def image_settle_secs(self) -> float:
        """二次确认等待时间（秒）。"""
        try:
            return max(0.5, float(self.data.get("image_settle_secs", 2.0)))
        except (TypeError, ValueError):
            return 2.0

    @property
    def egress_blacklist_failure_threshold(self) -> int:
        """连接错误熔断阈值：窗口内达到该次数才把出口拉黑（0/1 = 旧的一次即拉黑）。"""
        try:
            return max(1, int(self.data.get("egress_blacklist_failure_threshold", 3)))
        except (TypeError, ValueError):
            return 3

    @property
    def egress_blacklist_window_secs(self) -> int:
        """失败计数窗口（秒）。"""
        try:
            return max(1, int(self.data.get("egress_blacklist_window_secs", 60)))
        except (TypeError, ValueError):
            return 60

    @property
    def max_request_body_mb(self) -> int:
        """单个请求体上限（MB）：含 multipart 上传与 JSON body，0 表示不限制。"""
        try:
            return max(0, int(self.data.get("max_request_body_mb", 256)))
        except (TypeError, ValueError):
            return 256

    @property
    def log_retention_days(self) -> int:
        """logs.jsonl 保留天数；0 表示不清理。list()/delete() 会读全文件，不清理会无限膨胀。"""
        try:
            return max(0, int(self.data.get("log_retention_days", 30)))
        except (TypeError, ValueError):
            return 30

    @property
    def auto_remove_invalid_accounts(self) -> bool:
        value = self.data.get("auto_remove_invalid_accounts", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def auto_remove_rate_limited_accounts(self) -> bool:
        value = self.data.get("auto_remove_rate_limited_accounts", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def auto_relogin_after_refresh(self) -> bool:
        value = self.data.get("auto_relogin_after_refresh", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def log_levels(self) -> list[str]:
        levels = self.data.get("log_levels")
        if not isinstance(levels, list):
            return []
        allowed = {"debug", "info", "warning", "error"}
        return [level for item in levels if (level := str(item or "").strip().lower()) in allowed]

    @property
    def sensitive_words(self) -> list[str]:
        words = self.data.get("sensitive_words")
        return [word for item in words if (word := str(item or "").strip())] if isinstance(words, list) else []

    @property
    def ai_review(self) -> dict[str, object]:
        value = self.data.get("ai_review")
        return value if isinstance(value, dict) else {}

    @property
    def global_system_prompt(self) -> str:
        return str(self.data.get("global_system_prompt") or "").strip()

    @property
    def images_dir(self) -> Path:
        path = DATA_DIR / "images"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def image_thumbnails_dir(self) -> Path:
        path = DATA_DIR / "image_thumbnails"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cleanup_old_images(self) -> int:
        cutoff = time.time() - self.image_retention_days * 86400
        removed = 0
        for path in self.images_dir.rglob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        for path in sorted((p for p in self.images_dir.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                path.rmdir()
            except OSError:
                pass
        return removed

    @property
    def base_url(self) -> str:
        return str(
            os.getenv("CHATGPT2API_BASE_URL")
            or self.data.get("base_url")
            or ""
        ).strip().rstrip("/")

    @property
    def app_version(self) -> str:
        try:
            value = VERSION_FILE.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return "0.0.0"
        return value or "0.0.0"

    def get(self) -> dict[str, object]:
        data = dict(self.data)
        data["refresh_account_interval_minute"] = self.refresh_account_interval_minute
        data["image_retention_days"] = self.image_retention_days
        data["image_poll_timeout_secs"] = self.image_poll_timeout_secs
        data["image_sse_idle_timeout_secs"] = self.image_sse_idle_timeout_secs
        data["image_sse_total_timeout_secs"] = self.image_sse_total_timeout_secs
        data["image_task_timeout_secs"] = self.image_task_timeout_secs
        data["image_poll_interval_secs"] = self.image_poll_interval_secs
        data["image_poll_initial_wait_secs"] = self.image_poll_initial_wait_secs
        data["image_account_concurrency"] = self.image_account_concurrency
        data["image_account_failover_retries"] = self.image_account_failover_retries
        data["image_poll_failover_retries"] = self.image_poll_failover_retries
        data["image_text_failover_retries"] = self.image_text_failover_retries
        data["image_transient_failure_cooldown_secs"] = self.image_transient_failure_cooldown_secs
        data["image_parallel_generation"] = self.image_parallel_generation
        data["image_remove_conversation_after_result"] = self.image_remove_conversation_after_result
        data["auto_remove_invalid_accounts"] = self.auto_remove_invalid_accounts
        data["auto_remove_rate_limited_accounts"] = self.auto_remove_rate_limited_accounts
        data["auto_relogin_after_refresh"] = self.auto_relogin_after_refresh
        data["egress_blacklist_failure_threshold"] = self.egress_blacklist_failure_threshold
        data["egress_blacklist_window_secs"] = self.egress_blacklist_window_secs
        data["max_request_body_mb"] = self.max_request_body_mb
        data["log_retention_days"] = self.log_retention_days
        data["log_levels"] = self.log_levels
        data["sensitive_words"] = self.sensitive_words
        data["ai_review"] = self._sanitize_ai_review(self.ai_review)
        data["global_system_prompt"] = self.global_system_prompt
        data["backup"] = self._sanitize_backup_settings(self.get_backup_settings())
        data["image_storage"] = self._sanitize_image_storage_settings(self.get_image_storage_settings())
        data["chat_completion_cache"] = self.get_chat_completion_cache_settings()
        data["proxy_runtime"] = self.get_public_proxy_runtime_settings()
        data["third_party_apps"] = self.get_third_party_apps_settings()
        data["waifu2x"] = self._sanitize_waifu2x_settings(self.get_waifu2x_settings())
        data["doubao"] = self._sanitize_secret_settings(self.get_doubao_settings(), _DOUBAO_SECRET_KEYS)
        data["zhitu360"] = self._sanitize_secret_settings(self.get_zhitu360_settings(), _ZHITU360_SECRET_KEYS)
        # The legacy top-level ``proxy`` holds a full ``scheme://user:pass@host``
        # URL. proxy_runtime's equivalents are already redacted below; this one
        # was going out verbatim.
        # Redact in place only — no trimming. get() deliberately reports the
        # stored value verbatim; get_proxy_settings() is what normalizes.
        proxy_value = data.get("proxy")
        if isinstance(proxy_value, str) and proxy_value.strip():
            from services.proxy_service import _redact_url_credentials

            data["proxy"] = _redact_url_credentials(proxy_value)
        data.pop("auth-key", None)
        return data

    def get_proxy_settings(self) -> str:
        return str(self.data.get("proxy") or "").strip()

    def get_proxy_runtime_settings(self) -> dict[str, object]:
        return _normalize_proxy_runtime_settings(self.data.get("proxy_runtime"))

    def get_public_proxy_runtime_settings(self) -> dict[str, object]:
        from services.proxy_service import _redact_url_credentials

        runtime = copy.deepcopy(self.get_proxy_runtime_settings())
        # Redact credentials in proxy URLs (http://user:pass@host → http://[REDACTED]@host)
        for url_field in ("proxy_url", "resource_proxy_url"):
            val = str(runtime.get(url_field) or "").strip()
            if val:
                runtime[url_field] = _redact_url_credentials(val)
        clearance = runtime.get("clearance") if isinstance(runtime.get("clearance"), dict) else {}
        if isinstance(clearance, dict):
            cf_cookies = str(clearance.get("cf_cookies") or "").strip()
            cf_clearance = str(clearance.get("cf_clearance") or "").strip()
            clearance["cf_cookies"] = ""
            clearance["cf_clearance"] = ""
            clearance["has_cf_cookies"] = bool(cf_cookies)
            clearance["has_cf_clearance"] = bool(cf_clearance)
            # Redact FlareSolverr URL credentials too
            fs_url = str(clearance.get("flaresolverr_url") or "").strip()
            if fs_url:
                clearance["flaresolverr_url"] = _redact_url_credentials(fs_url)
        return runtime

    def get_third_party_apps_settings(self) -> dict[str, object]:
        return _normalize_third_party_apps_settings(self.data.get("third_party_apps"))

    def update(self, data: dict[str, object]) -> dict[str, object]:
        next_data = dict(self.data)
        next_data.update(dict(data or {}))
        if "backup" in next_data:
            incoming_backup = next_data.get("backup")
            if isinstance(incoming_backup, dict):
                current_backup = self.get_backup_settings()
                # Preserve existing secrets when the client sent "********"
                # (the masked placeholder returned by GET /api/settings).
                if incoming_backup.get("secret_access_key") == "********":
                    incoming_backup["secret_access_key"] = current_backup.get("secret_access_key")
                if incoming_backup.get("passphrase") == "********":
                    incoming_backup["passphrase"] = current_backup.get("passphrase")
            next_data["backup"] = _normalize_backup_settings(next_data.get("backup"))
        if "image_storage" in next_data:
            incoming_storage = next_data.get("image_storage")
            if isinstance(incoming_storage, dict):
                current_storage = self.get_image_storage_settings()
                if incoming_storage.get("webdav_password") == "********":
                    incoming_storage["webdav_password"] = current_storage.get("webdav_password")
            next_data["image_storage"] = _normalize_image_storage_settings(next_data.get("image_storage"))
            _validate_image_storage_settings(next_data["image_storage"])
        if "chat_completion_cache" in next_data:
            next_data["chat_completion_cache"] = _normalize_chat_completion_cache_settings(
                next_data.get("chat_completion_cache")
            )
        if "third_party_apps" in next_data:
            next_data["third_party_apps"] = _normalize_third_party_apps_settings(next_data.get("third_party_apps"))
        # These three normalize with use_env=False on the write path. Reading
        # them still prefers the environment; persisting must not, or saving
        # settings once bakes every .env secret into config.json (and from
        # there into the backup archive). Masked values are likewise restored
        # from the *stored* config rather than the env-merged getter.
        if "waifu2x" in next_data:
            incoming_waifu = next_data.get("waifu2x")
            if isinstance(incoming_waifu, dict):
                stored_waifu = _normalize_waifu2x_settings(self.data.get("waifu2x"), use_env=False)
                incoming_waifu = dict(incoming_waifu)
                for key in _WAIFU2X_SECRET_KEYS:
                    if incoming_waifu.get(key) == "********":
                        incoming_waifu[key] = stored_waifu.get(key) or ""
                    incoming_waifu.pop(f"has_{key}", None)
                next_data["waifu2x"] = _normalize_waifu2x_settings(incoming_waifu, use_env=False)
            else:
                next_data.pop("waifu2x", None)
        if "doubao" in next_data:
            incoming = next_data.get("doubao")
            if isinstance(incoming, dict):
                stored = _normalize_doubao_settings(self.data.get("doubao"), use_env=False)
                incoming = dict(incoming)
                for key in _DOUBAO_SECRET_KEYS:
                    if incoming.get(key) == "********":
                        incoming[key] = stored.get(key) or ""
                    incoming.pop(f"has_{key}", None)
                next_data["doubao"] = _normalize_doubao_settings(incoming, use_env=False)
            else:
                next_data.pop("doubao", None)
        if "zhitu360" in next_data:
            incoming = next_data.get("zhitu360")
            if isinstance(incoming, dict):
                stored = _normalize_zhitu360_settings(self.data.get("zhitu360"), use_env=False)
                incoming = dict(incoming)
                for key in _ZHITU360_SECRET_KEYS:
                    if incoming.get(key) == "********":
                        incoming[key] = stored.get(key) or ""
                    incoming.pop(f"has_{key}", None)
                next_data["zhitu360"] = _normalize_zhitu360_settings(incoming, use_env=False)
            else:
                next_data.pop("zhitu360", None)
        if "ai_review" in next_data:
            incoming_review = next_data.get("ai_review")
            if isinstance(incoming_review, dict):
                incoming_review = dict(incoming_review)
                if incoming_review.get("api_key") == "********":
                    incoming_review["api_key"] = self.ai_review.get("api_key") or ""
                incoming_review.pop("has_api_key", None)
                next_data["ai_review"] = incoming_review
        if "proxy" in next_data:
            next_data["proxy"] = self._restore_redacted_url(next_data.get("proxy"), self.data.get("proxy"))
        if "proxy_runtime" in next_data:
            incoming_runtime = next_data.get("proxy_runtime")
            if isinstance(incoming_runtime, dict):
                current_runtime = self.get_proxy_runtime_settings()
                incoming_runtime = dict(incoming_runtime)
                # These are redacted on the way out; put the stored value back
                # when the client echoed the placeholder.
                for url_field in ("proxy_url", "resource_proxy_url"):
                    incoming_runtime[url_field] = self._restore_redacted_url(
                        incoming_runtime.get(url_field), current_runtime.get(url_field)
                    )
                incoming_clearance = incoming_runtime.get("clearance")
                previous_clearance = current_runtime.get("clearance")
                if isinstance(previous_clearance, dict):
                    if isinstance(incoming_clearance, dict):
                        incoming_clearance = dict(incoming_clearance)
                        incoming_clearance["flaresolverr_url"] = self._restore_redacted_url(
                            incoming_clearance.get("flaresolverr_url"),
                            previous_clearance.get("flaresolverr_url"),
                        )
                        incoming_runtime["clearance"] = incoming_clearance
                    incoming_runtime["_existing_cf_cookies"] = previous_clearance.get("cf_cookies")
                    incoming_runtime["_existing_cf_clearance"] = previous_clearance.get("cf_clearance")
            next_data["proxy_runtime"] = _normalize_proxy_runtime_settings(incoming_runtime)
        next_data.pop("backup_state", None)
        self.data = next_data
        self._save()
        return self.get()

    def get_backup_settings(self) -> dict[str, object]:
        return _normalize_backup_settings(self.data.get("backup"))

    @staticmethod
    def _sanitize_backup_settings(settings: dict[str, object]) -> dict[str, object]:
        """Mask sensitive backup fields for API responses."""
        out = dict(settings) if isinstance(settings, dict) else {}
        if out.get("secret_access_key"):
            out["secret_access_key"] = "********"
        if out.get("passphrase"):
            out["passphrase"] = "********"
        return out

    def get_image_storage_settings(self) -> dict[str, object]:
        return _normalize_image_storage_settings(self.data.get("image_storage"))

    @staticmethod
    def _sanitize_image_storage_settings(settings: dict[str, object]) -> dict[str, object]:
        """Mask sensitive image storage fields for API responses."""
        out = dict(settings) if isinstance(settings, dict) else {}
        if out.get("webdav_password"):
            out["webdav_password"] = "********"
        return out

    @staticmethod
    def _sanitize_ai_review(settings: dict[str, object]) -> dict[str, object]:
        """Mask the AI review provider key for API responses."""
        out = dict(settings) if isinstance(settings, dict) else {}
        present = bool(str(out.get("api_key") or "").strip())
        out["has_api_key"] = present
        out["api_key"] = "********" if present else ""
        return out

    @staticmethod
    def _restore_redacted_url(incoming: object, current: object) -> str:
        """Keep the stored URL when the client echoed back a redacted one.

        GET /api/settings returns ``scheme://[REDACTED]@host`` for credentialed
        URLs, and the settings page round-trips its whole config object on save.
        Without this, one save overwrites the real credentials with the
        placeholder GET deliberately substituted.
        """
        value = str(incoming or "").strip()
        if value and "[REDACTED]@" in value:
            return str(current or "").strip()
        return value

    @staticmethod
    def _sanitize_secret_settings(settings: dict[str, object], keys: tuple[str, ...]) -> dict[str, object]:
        out = dict(settings) if isinstance(settings, dict) else {}
        for key in keys:
            present = bool(str(out.get(key) or "").strip())
            out[f"has_{key}"] = present
            out[key] = "********" if present else ""
        return out

    @staticmethod
    def _sanitize_waifu2x_settings(settings: dict[str, object]) -> dict[str, object]:
        return ConfigStore._sanitize_secret_settings(settings, _WAIFU2X_SECRET_KEYS)

    def get_chat_completion_cache_settings(self) -> dict[str, object]:
        return _normalize_chat_completion_cache_settings(self.data.get("chat_completion_cache"))

    def get_storage_backend(self) -> StorageBackend:
        """获取存储后端实例（单例）"""
        if self._storage_backend is None:
            from services.storage.factory import create_storage_backend
            self._storage_backend = create_storage_backend(DATA_DIR)
        return self._storage_backend


# Files under data/ that hold credentials. Everything written through
# utils.atomic is already 0600; these are the ones a plain write_text() created
# before, plus anything an operator dropped in by hand. ./data is bind-mounted
# in every compose file, so 0644 here means world-readable on the host.
_PRIVATE_DATA_FILES = (
    "accounts.json",
    "auth_keys.json",
    "g2a_config.json",
    "gpt_register.env",
    "gpt_register_config.json",
    "gpt_register_jobs.json",
    "grok_accounts.json",
    "logs.jsonl",
    "register_engines.db",
)
_PRIVATE_DATA_GLOBS = ("gpt_register_logs/*.json",)


def harden_data_permissions() -> list[str]:
    """chmod 0600 the credential-bearing files under data/ and config.json.

    Returns the paths that actually changed, so startup can say so once instead
    of silently fixing it on every boot.
    """
    from utils.atomic import secure_file_mode

    changed: list[str] = []
    candidates = [CONFIG_FILE, *(DATA_DIR / name for name in _PRIVATE_DATA_FILES)]
    for pattern in _PRIVATE_DATA_GLOBS:
        candidates.extend(DATA_DIR.glob(pattern))
    for path in candidates:
        try:
            if path.is_file() and secure_file_mode(path):
                changed.append(str(path))
        except OSError:
            continue
    return changed


def load_backup_state() -> dict[str, object]:
    return _normalize_backup_state(_read_json_object(BACKUP_STATE_FILE, name="backup_state.json"))


def save_backup_state(state: dict[str, object]) -> dict[str, object]:
    from utils.atomic import atomic_write_json

    normalized = _normalize_backup_state(state)
    atomic_write_json(BACKUP_STATE_FILE, normalized)
    return normalized


config = ConfigStore(CONFIG_FILE)
