"""Turnstile token providers for www.waifu2x.net.

Tokens are one-shot and bound to the sitekey. This module only talks to the
official Turnstile widget flow (caller-supplied token), Patreon power skip,
or third-party captcha solving APIs the operator configured. It does not
implement a Turnstile exploit.

The solver transport itself lives in :mod:`services.captcha_solver` so the
webhook / Capsolver / 2Captcha / YesCaptcha plumbing exists exactly once; this
module keeps only the waifu2x-specific bits (sitekey, meter short-circuit and
the operator-facing error text).
"""
from __future__ import annotations

from typing import Any, Callable

from services.captcha_solver import CaptchaError, obtain_captcha_token

TURNSTILE_SITEKEY = "0x4AAAAAABqlY7DKXMzoS81U"
PAGE_URL = "https://www.waifu2x.net/"

_MISSING_TOKEN_HINT = (
    "www.waifu2x.net 需要 Cloudflare Turnstile。请在请求里传 turnstile，"
    "或配置 WAIFU2X_CAPSOLVER_KEY / WAIFU2X_TWOCAPTCHA_KEY / WAIFU2X_YESCAPTCHA_KEY，"
    "或使用 Patreon 登录后的 ses_id。"
)


class TurnstileError(CaptchaError):
    """Raised when a Turnstile token cannot be obtained."""


def captcha_required(state: dict[str, Any] | None) -> bool:
    """Match ui.js: supporters with remaining meter skip the widget."""
    data = state or {}
    has_power = bool(data.get("logged_in")) and int(data.get("meter") or 0) > 0
    if has_power:
        return False
    return bool(data.get("turnstile_enabled") or data.get("recaptcha_enabled"))


def turnstile_site_key(state: dict[str, Any] | None) -> str:
    key = str((state or {}).get("turnstile_site_key") or "").strip()
    return key or TURNSTILE_SITEKEY


def obtain_turnstile_token(
    *,
    site_key: str,
    settings: dict[str, Any],
    explicit: str = "",
    session_factory: Callable[..., Any] | None = None,
) -> str:
    """Return a token, preferring an explicit one, then configured solvers."""
    try:
        return obtain_captcha_token(
            website_url=PAGE_URL,
            website_key=str(site_key or "").strip() or TURNSTILE_SITEKEY,
            settings=settings,
            explicit=explicit,
            session_factory=session_factory,
            error_hint=_MISSING_TOKEN_HINT,
        )
    except TurnstileError:
        raise
    except CaptchaError as exc:
        raise TurnstileError(str(exc)) from exc
