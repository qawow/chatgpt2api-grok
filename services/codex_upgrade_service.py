"""Upgrade session_only ChatGPT accounts via protocol Codex CLI OTP.

Replaces the browser OAuth "补 refresh" flow for free/register accounts:
re-run Codex client_id OAuth + mailbox OTP on the existing email, then
write refresh/id tokens and drop the old session-only row.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from services.gpt_register_service import gpt_register_config, normalize_settings
from services.log_service import LOG_TYPE_ACCOUNT, log_service
from utils.helper import anonymize_token


def _clean(value: object) -> str:
    return str(value or "").strip()


def _settings_for_upgrade(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    base = normalize_settings(gpt_register_config.get())
    if overrides:
        merged = {**base, **overrides}
        return normalize_settings(merged)
    return base


def apply_codex_tokens_to_pool(
    *,
    result: dict[str, Any],
    replace_access_token: str = "",
    email: str = "",
    password: str = "",
    proxy: str = "",
    plan_type: str = "free",
    source_type: str = "codex_upgrade",
) -> dict[str, Any]:
    """Persist successful Codex tokens into account_service; replace old session row."""
    from services.account_service import account_service

    access = _clean(result.get("access_token"))
    refresh = _clean(result.get("refresh_token"))
    id_token = _clean(result.get("id_token"))
    if not access or not refresh:
        return {
            "ok": False,
            "added": 0,
            "replaced": 0,
            "error": "incomplete tokens",
            "items": [],
        }

    payload: dict[str, Any] = {
        "access_token": access,
        "refresh_token": refresh,
        "id_token": id_token,
        "email": _clean(email) or _clean(result.get("email")),
        "password": _clean(password),
        "account_id": _clean(result.get("account_id")),
        "type": _clean(plan_type) or "free",
        "source_type": _clean(source_type) or "codex_upgrade",
        "export_type": "codex",
        "status": "正常",
        "session_only": False,
        "fragile": False,
    }
    if proxy:
        payload["proxy"] = proxy

    add_result = account_service.add_account_items([payload])
    replaced = 0
    old_token = _clean(replace_access_token)
    if old_token and old_token != access:
        try:
            del_result = account_service.delete_accounts([old_token])
            replaced = int(del_result.get("removed") or 0)
        except Exception as exc:
            print(f"[codex-upgrade] replace old token failed: {exc}", flush=True)

    try:
        account_service.fetch_remote_info(
            access,
            event="codex_upgrade",
            defer_invalid_removal=True,
        )
    except Exception as exc:
        try:
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "Codex 补齐后刷新额度失败",
                {
                    "token": anonymize_token(access),
                    "email": payload.get("email") or "",
                    "error": str(exc)[:300],
                },
            )
        except Exception:
            pass

    return {
        "ok": True,
        "added": int(add_result.get("added") or 0),
        "replaced": replaced,
        "access_token": access,
        "email": payload.get("email") or "",
        "items": add_result.get("items") or [],
        "error": None,
    }


def upgrade_session_account_via_codex(
    *,
    email: str,
    replace_access_token: str = "",
    password: str = "",
    settings: dict[str, Any] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run Codex OTP for an existing email and write tokens into the pool.

    Soft-fails (ok=False) on add_phone / OTP miss / network errors — caller keeps
    the session_only row.
    """
    from gpt_free_register.codex_upgrade import obtain_codex_tokens_for_email

    addr = _clean(email).lower()
    if not addr or "@" not in addr:
        return {
            "ok": False,
            "email": addr,
            "reason": "invalid_email",
            "error": "email is required",
            "added": 0,
            "replaced": 0,
            "logs": [],
        }

    cfg = _settings_for_upgrade(settings)
    proxy = _clean(cfg.get("proxy")) if cfg.get("bind_register_proxy") else _clean(cfg.get("proxy"))

    result = obtain_codex_tokens_for_email(email=addr, settings=cfg, log=log)
    if not result.get("ok"):
        reason = _clean(result.get("reason")) or "failed"
        error = _clean(result.get("error")) or "Codex 补齐失败"
        try:
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "Codex 补 refresh 失败（保留 session_only）",
                {
                    "email": addr,
                    "reason": reason,
                    "error": error[:300],
                    "token": anonymize_token(replace_access_token),
                },
            )
        except Exception:
            pass
        return {
            "ok": False,
            "email": addr,
            "reason": reason,
            "error": error,
            "added": 0,
            "replaced": 0,
            "logs": list(result.get("logs") or [])[-80:],
        }

    applied = apply_codex_tokens_to_pool(
        result=result,
        replace_access_token=replace_access_token,
        email=addr,
        password=password,
        proxy=proxy if cfg.get("bind_register_proxy") else "",
        plan_type=_clean(cfg.get("plan_type")) or "free",
        source_type="codex_upgrade",
    )
    try:
        log_service.add(
            LOG_TYPE_ACCOUNT,
            "Codex 补 refresh 成功",
            {
                "email": addr,
                "replaced": applied.get("replaced") or 0,
                "token": anonymize_token(applied.get("access_token") or ""),
            },
        )
    except Exception:
        pass
    return {
        **applied,
        "reason": None,
        "logs": list(result.get("logs") or [])[-80:],
    }


def schedule_codex_upgrade(
    *,
    email: str,
    replace_access_token: str = "",
    password: str = "",
    settings: dict[str, Any] | None = None,
    name_hint: str = "",
) -> None:
    """Fire-and-forget background Codex upgrade (used after register import)."""
    addr = _clean(email).lower()
    if not addr:
        return
    hint = _clean(name_hint) or addr[:16]

    def _run() -> None:
        try:
            upgrade_session_account_via_codex(
                email=addr,
                replace_access_token=replace_access_token,
                password=password,
                settings=settings,
                log=lambda m: print(f"[codex-upgrade:{hint}] {m}", flush=True),
            )
        except Exception as exc:
            print(f"[codex-upgrade:{hint}] background failed: {exc}", flush=True)
            try:
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "Codex 后台补齐异常",
                    {"email": addr, "error": str(exc)[:300]},
                )
            except Exception:
                pass

    try:
        threading.Thread(
            target=_run,
            name=f"codex-upgrade-{hint[:20]}",
            daemon=True,
        ).start()
    except Exception as exc:
        print(f"[codex-upgrade] failed to spawn thread: {exc}", flush=True)
        # last resort: sync (may slow register worker)
        try:
            _run()
        except Exception:
            pass
