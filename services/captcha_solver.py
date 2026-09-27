"""External captcha solving (打码) used by unofficial web wrappers.

Tokens are obtained from:
1. An explicit token the caller already solved
2. A self-hosted solve webhook (``solve_url``)
3. Capsolver / 2Captcha / YesCaptcha SaaS APIs

This module does not implement captcha bypasses. Task ``type`` is chosen by
the caller to match the widget (Turnstile, reCAPTCHA, Tencent, TikTok, ...).
"""
from __future__ import annotations

import time
from typing import Any, Callable

from utils.curl_tls import create_cffi_session

_SOLVER_ENDPOINTS = {
    "capsolver": (
        "https://api.capsolver.com/createTask",
        "https://api.capsolver.com/getTaskResult",
    ),
    "twocaptcha": (
        "https://api.2captcha.com/createTask",
        "https://api.2captcha.com/getTaskResult",
    ),
    "yescaptcha": (
        "https://api.yescaptcha.com/createTask",
        "https://api.yescaptcha.com/getTaskResult",
    ),
}

_DEFAULT_TASK_TYPE = {
    "capsolver": "AntiTurnstileTaskProxyLess",
    "twocaptcha": "TurnstileTaskProxyless",
    "yescaptcha": "TurnstileTaskProxyless",
}

_DEFAULT_HINT = (
    "需要人机验证 token。请在请求里传 captcha，或配置 solve_url / "
    "CAPSOLVER_KEY / TWOCAPTCHA_KEY / YESCAPTCHA_KEY。"
)


class CaptchaError(RuntimeError):
    """Raised when a captcha token cannot be obtained."""


def default_session_factory(**overrides: Any):
    """Solver sessions go through the same egress as the upstream backends.

    Without this the打码 platform is reached directly, which fails on hosts
    whose only route out is the configured proxy.
    """
    from services.proxy_service import proxy_settings

    kwargs = proxy_settings.build_session_kwargs(impersonate="chrome142", verify=True, upstream=True)
    kwargs.update(overrides)
    return create_cffi_session(**kwargs)


def obtain_captcha_token(
    *,
    website_url: str,
    website_key: str,
    settings: dict[str, Any],
    explicit: str = "",
    task_type: str = "",
    extra: dict[str, Any] | None = None,
    session_factory: Callable[..., Any] | None = None,
    error_hint: str = "",
) -> str:
    """Return a token, preferring an explicit one, then webhook, then SaaS."""
    token = str(explicit or "").strip()
    if token:
        return token

    factory = session_factory or default_session_factory
    timeout = max(30, int(settings.get("timeout_sec") or 180))
    errors: list[str] = []

    solve_url = str(settings.get("solve_url") or "").strip()
    if solve_url:
        try:
            return _solve_via_webhook(
                solve_url,
                website_url=website_url,
                website_key=website_key,
                task_type=task_type,
                extra=extra or {},
                timeout=timeout,
                session_factory=factory,
            )
        except Exception as exc:
            errors.append(f"solve_url: {exc}")

    solvers: list[tuple[str, str]] = []
    for name, key_name in (
        ("capsolver", "capsolver_key"),
        ("twocaptcha", "twocaptcha_key"),
        ("yescaptcha", "yescaptcha_key"),
    ):
        api_key = str(settings.get(key_name) or "").strip()
        if api_key:
            solvers.append((name, api_key))

    for name, api_key in solvers:
        try:
            return _solve_via_saas(
                name,
                api_key,
                website_url=website_url,
                website_key=website_key,
                task_type=task_type or _DEFAULT_TASK_TYPE[name],
                extra=extra or {},
                timeout=timeout,
                session_factory=factory,
            )
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    raise CaptchaError(
        (str(error_hint or "").strip() or _DEFAULT_HINT)
        + ((" 打码失败: " + " | ".join(errors)) if errors else "")
    )


def _solve_via_webhook(
    solve_url: str,
    *,
    website_url: str,
    website_key: str,
    task_type: str,
    extra: dict[str, Any],
    timeout: int,
    session_factory: Callable[..., Any],
) -> str:
    session = session_factory()
    response = session.post(
        solve_url,
        json={
            "type": task_type or "captcha",
            "websiteURL": website_url,
            "websiteKey": website_key,
            "extra": extra,
        },
        timeout=min(120, timeout),
    )
    payload = _json_body(response)
    token = _extract_token(payload)
    if not token:
        raise CaptchaError(f"solve_url did not return a token: {payload}")
    return token


def _solve_via_saas(
    name: str,
    api_key: str,
    *,
    website_url: str,
    website_key: str,
    task_type: str,
    extra: dict[str, Any],
    timeout: int,
    session_factory: Callable[..., Any],
) -> str:
    create_url, result_url = _SOLVER_ENDPOINTS[name]
    task: dict[str, Any] = {
        "type": task_type,
        "websiteURL": website_url,
        "websiteKey": website_key,
    }
    task.update(extra)
    session = session_factory()
    created = session.post(
        create_url,
        json={"clientKey": api_key, "task": task},
        timeout=min(60, timeout),
    )
    payload = _json_body(created)
    if int(payload.get("errorId") or 0) != 0:
        raise CaptchaError(str(payload.get("errorDescription") or payload.get("errorCode") or payload))
    task_id = str(payload.get("taskId") or "").strip()
    if not task_id:
        token = _extract_token(payload)
        if token:
            return token
        raise CaptchaError(f"{name} did not return a taskId")

    deadline = time.time() + timeout
    first = True
    while time.time() < deadline:
        if not first:
            time.sleep(2)
        first = False
        polled = session.post(
            result_url,
            json={"clientKey": api_key, "taskId": task_id},
            timeout=min(60, timeout),
        )
        body = _json_body(polled)
        if int(body.get("errorId") or 0) != 0:
            raise CaptchaError(str(body.get("errorDescription") or body.get("errorCode") or body))
        status = str(body.get("status") or "").lower()
        if status in {"ready", "success"}:
            token = _extract_token(body)
            if not token:
                raise CaptchaError(f"{name} ready without token")
            return token
        if status in {"failed", "error"}:
            raise CaptchaError(str(body.get("errorDescription") or body))
    raise CaptchaError(f"{name} timed out waiting for captcha token")


def _json_body(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception as exc:
        raise CaptchaError(f"solver HTTP {getattr(response, 'status_code', '?')}: {exc}") from exc
    if not isinstance(data, dict):
        raise CaptchaError(f"solver returned non-object JSON: {type(data).__name__}")
    return data


def _extract_token(payload: dict[str, Any]) -> str:
    solution = payload.get("solution")
    if isinstance(solution, dict):
        for key in ("token", "cf_clearance", "gRecaptchaResponse", "captcha", "ticket"):
            value = str(solution.get(key) or "").strip()
            if value:
                return value
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("token", "captcha", "ticket"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
    for key in ("token", "captcha", "gRecaptchaResponse", "ticket"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""
