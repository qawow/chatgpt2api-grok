"""In-process ChatGPT free registration runner.

Uses the vendored engines tree under ``gpt_free_register/engines`` so the host
no longer needs ``/root/any-register-engines``.

Only the protocol + mailbox path is supported here (the same path the settings
page uses). Browser / OAuth flows remain available via engines CLI if needed.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

PACKAGE_DIR = Path(__file__).resolve().parent
ENGINES_DIR = PACKAGE_DIR / "engines"

_BOOT_LOCK = threading.RLock()
_BOOTED = False


def default_engines_dir() -> str:
    return str(ENGINES_DIR)


def _clean(value: object) -> str:
    return str(value or "").strip()


def _ensure_engines_on_path(engines_dir: Path) -> None:
    root = str(engines_dir.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)


def _load_env_files(engines_dir: Path) -> None:
    """Load .env without requiring secrets inside the git tree.

    Order (first wins for each key — load_dotenv does not override):
    1. process environment (already set)
    2. data/gpt_register.env (chatgpt2api data dir)
    3. engines/.env (optional local override, gitignored)
    4. engines/.env.example is never auto-loaded as live secrets
    """
    from core.proxy_env import load_dotenv

    # Prefer chatgpt2api data dir when available.
    try:
        from services.config import DATA_DIR

        data_env = Path(DATA_DIR) / "gpt_register.env"
    except Exception:
        data_env = Path.cwd() / "data" / "gpt_register.env"

    # load data env first by temporarily unsetting nothing; load_dotenv skips set keys
    # so we load less-specific first then more-specific? Actually load_dotenv only sets
    # if key NOT in os.environ. So load specific first by calling it on data_env then engines.
    if data_env.is_file():
        load_dotenv(data_env)
    engines_env = engines_dir / ".env"
    if engines_env.is_file():
        load_dotenv(engines_env)
    # also pick up any ambient project .env via default search
    load_dotenv()


def _bootstrap(engines_dir: Path) -> None:
    global _BOOTED
    with _BOOT_LOCK:
        _ensure_engines_on_path(engines_dir)
        _load_env_files(engines_dir)
        if _BOOTED:
            return
        # Minimal bootstrap without pulling every platform: register chatgpt only.
        try:
            import platforms.chatgpt.plugin  # noqa: F401
        except Exception as exc:
            raise RuntimeError(f"无法加载内置 ChatGPT 注册机: {exc}") from exc
        try:
            # mailbox provider side-effects (register cloudflare_d1 aliases)
            import providers.mailbox.cloudflare_d1  # noqa: F401
        except Exception:
            pass
        _BOOTED = True


def _create_cfd1_mailbox(extra: dict[str, Any], proxy: str | None):
    """Direct CFD1 factory — skips DB-backed provider definitions."""
    from core.base_mailbox import MAILBOX_FACTORY_REGISTRY

    factory = MAILBOX_FACTORY_REGISTRY.get("cloudflare_d1_api") or MAILBOX_FACTORY_REGISTRY.get("cloudflare_d1")
    if factory is None:
        raise RuntimeError("内置 engines 未注册 cloudflare_d1 邮箱工厂")
    return factory(extra, proxy)


def _create_mailbox(provider: str, extra: dict[str, Any], proxy: str | None):
    key = _clean(provider) or "cloudflare_d1_api"
    # Prefer direct registry to avoid needing seeded register_engines.db.
    try:
        from core.base_mailbox import MAILBOX_FACTORY_REGISTRY

        if key in MAILBOX_FACTORY_REGISTRY:
            return MAILBOX_FACTORY_REGISTRY[key](extra, proxy)
        # driver aliases
        aliases = {
            "cfd1": "cloudflare_d1_api",
            "cloudflare_d1": "cloudflare_d1_api",
        }
        alias = aliases.get(key)
        if alias and alias in MAILBOX_FACTORY_REGISTRY:
            return MAILBOX_FACTORY_REGISTRY[alias](extra, proxy)
    except Exception:
        pass
    # Fallback to full create_mailbox (needs provider DB)
    from core.base_mailbox import create_mailbox

    return create_mailbox(key, extra=extra, proxy=proxy)


def register_chatgpt_once(
    *,
    settings: dict[str, Any] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run one ChatGPT free protocol registration.

    Returns a dict compatible with the previous CLI JSON shape::

        {
          "platform": "chatgpt",
          "email": "...",
          "password": "...",
          "user_id": "...",
          "token": "...",
          "status": "...",
          "extra": {...},
        }
    """
    cfg = dict(settings or {})
    engines_dir = Path(_clean(cfg.get("engines_dir")) or default_engines_dir())
    if not engines_dir.is_dir():
        raise RuntimeError(f"注册机目录不存在: {engines_dir}")
    if not (engines_dir / "platforms" / "chatgpt" / "plugin.py").is_file():
        raise RuntimeError(f"内置注册机不完整，缺少 platforms/chatgpt: {engines_dir}")

    _bootstrap(engines_dir)

    # Apply per-job env overrides after dotenv load (these DO override).
    if _clean(cfg.get("cfd1_domain")):
        os.environ["CFD1_DOMAIN"] = _clean(cfg.get("cfd1_domain"))
    for env_key, cfg_key in (
        ("CFD1_API_TOKEN", "cfd1_api_token"),
        ("CFD1_ACCOUNT_ID", "cfd1_account_id"),
        ("CFD1_DATABASE_ID", "cfd1_database_id"),
        ("CFD1_LOCAL_PART_PREFIX", "cfd1_local_part_prefix"),
        ("CFD1_LOCAL_PART_LENGTH", "cfd1_local_part_length"),
        ("REGISTER_PROXY", "proxy"),
    ):
        val = _clean(cfg.get(cfg_key))
        if val:
            os.environ[env_key] = val

    from core.base_platform import RegisterConfig
    from core.proxy_env import mask_proxy, resolve_proxy
    from core.registry import get as get_platform

    log_fn = log or print
    proxy = resolve_proxy(cfg.get("proxy") if _clean(cfg.get("proxy")) else None)
    if proxy:
        log_fn(f"[proxy] {mask_proxy(proxy)}")
    else:
        log_fn("[proxy] (none)")

    mail_provider = _clean(cfg.get("mail_provider")) or "cloudflare_d1_api"
    captcha = _clean(cfg.get("captcha")) or "auto"
    executor = _clean(cfg.get("executor")) or "protocol"
    if executor not in {"protocol", "headless", "headed"}:
        executor = "protocol"

    extra: dict[str, Any] = {
        "mail_provider": mail_provider,
        "identity_provider": "mailbox",
        "captcha_solver": captcha,
    }
    # pass through optional CFD1 overrides into mailbox factory extra
    for key in (
        "cfd1_api_token",
        "cfd1_account_id",
        "cfd1_database_id",
        "cfd1_domain",
        "cfd1_local_part_prefix",
        "cfd1_local_part_length",
        "cfd1_api_base",
        "cfd1_table",
    ):
        if _clean(cfg.get(key)):
            extra[key] = _clean(cfg.get(key))

    config = RegisterConfig(
        executor_type=executor,
        captcha_solver=captcha,
        proxy=proxy,
        extra=extra,
    )

    mailbox = _create_mailbox(mail_provider, extra, proxy)
    platform_cls = get_platform("chatgpt")
    platform = platform_cls(config=config, mailbox=mailbox)
    platform.set_logger(log_fn)

    try:
        account = platform.register(email=None, password=None)
    except Exception as exc:
        return {
            "platform": "chatgpt",
            "email": None,
            "password": None,
            "user_id": None,
            "token": None,
            "status": "failed",
            "extra": {"error": str(exc)[:500], "trace": traceback.format_exc()[-800:]},
            "error": str(exc)[:500],
        }

    extra_out = dict(getattr(account, "extra", None) or {})
    token = _clean(getattr(account, "token", None)) or _clean(extra_out.get("access_token"))
    return {
        "platform": getattr(account, "platform", None) or "chatgpt",
        "email": getattr(account, "email", None),
        "password": getattr(account, "password", None),
        "user_id": getattr(account, "user_id", None),
        "token": token or None,
        "status": str(getattr(getattr(account, "status", None), "value", getattr(account, "status", "")) or ""),
        "region": getattr(account, "region", None),
        "trial_end_time": getattr(account, "trial_end_time", None),
        "extra": extra_out,
    }


def register_chatgpt_once_json(**kwargs: Any) -> str:
    return json.dumps(register_chatgpt_once(**kwargs), ensure_ascii=False, indent=2, default=str)
