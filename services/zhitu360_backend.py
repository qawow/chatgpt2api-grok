"""Unofficial 360智图 client wrapping image.360.com /api/v1/zhitu.

Protocol (from saas_main.js !5e01f801 + live probes):

    GET  /api/v1/zhitu/text/to/image/v2/config   (public)
    POST /api/v1/zhitu/text/to/image/create      JSON
         prompt, promptText, feature, api_user, ratio, style, model, photoNums
    POST /api/v1/zhitu/text/to/image/query       JSON {record_id}

    task_result.status: 0 create, 1 queue, 2 generating, 3 success, 4 fail,
                        5 timeout, 6 hit_risk

    errno 20601 未登录; 20603 付费/豆不足; create without fields → 参数检查失败.

Login is QHPass cookies. Captcha is not solved here: pass ``captcha`` or
configure captcha_solver (solve_url / Capsolver / 2Captcha / YesCaptcha).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from services.captcha_solver import CaptchaError, obtain_captcha_token
from utils.curl_tls import create_cffi_session
from utils.image_models import (
    DEFAULT_ZHITU360_MODEL,
    ZHITU360_IMAGE_MODELS,
    ZHITU360_MODEL_ALIASES,
    is_zhitu_model as _is_zhitu_model,
)

DEFAULT_BASE_URL = "https://image.360.com"
PAGE_URL = "https://image.360.com/"
FEATURE_TEXT2IMAGE = "tools_text2image"
API_USER_DEFAULT = "chacha"
SRCG_DEFAULT = "360_pic"

STATUS_NAMES = {
    -1: "none",
    0: "create_task",
    1: "in_queue",
    2: "generating",
    3: "success",
    4: "fail",
    5: "timeout",
    6: "hit_risk",
}
PENDING_STATUSES = {0, 1, 2}
SUCCESS_STATUS = 3
RISK_STATUS = 6
CAPTCHA_ERRNOS = {20604, 20605}

# Model ids live in utils/image_models.py; kept under the old names for callers.
MODELS = ZHITU360_IMAGE_MODELS
MODEL_ALIASES = ZHITU360_MODEL_ALIASES
RATIOS = ("9:16", "3:4", "1:1", "4:3", "16:9", "3:2", "2:3", "auto")


class Zhitu360Error(HTTPException):
    """Upstream or protocol error mapped to an HTTP status."""


@dataclass
class ZhituImage:
    url: str = ""


@dataclass
class ZhituResult:
    record_id: str = ""
    status: int = 0
    status_name: str = ""
    images: list[ZhituImage] = field(default_factory=list)


def _settings() -> dict[str, Any]:
    from services.config import config

    return config.get_zhitu360_settings()


def _session(settings: dict[str, Any] | None = None):
    from services.proxy_service import proxy_settings

    cfg = settings or _settings()
    kwargs = proxy_settings.build_session_kwargs(impersonate="chrome142", verify=True, upstream=True)
    session = create_cffi_session(**kwargs)
    ua = str(cfg.get("user_agent") or "").strip()
    if ua:
        session.headers["User-Agent"] = ua
    cookies = str(cfg.get("cookies") or "").strip()
    if cookies:
        session.headers["Cookie"] = cookies
    origin = str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    session.headers["Origin"] = origin
    session.headers["Referer"] = origin + "/"
    return session, cfg


def is_zhitu_model(value: object) -> bool:
    return _is_zhitu_model(value)


def size_to_ratio(value: object, default: str = "1:1") -> str:
    text = str(value if value is not None else default).strip() or default
    lowered = text.lower().replace(" ", "")
    mapping = {
        "1024x1024": "1:1",
        "512x512": "1:1",
        "1024x1792": "9:16",
        "768x1344": "9:16",
        "1792x1024": "16:9",
        "1344x768": "16:9",
        "square": "1:1",
    }
    if lowered in mapping:
        return mapping[lowered]
    return parse_ratio(text, default)


def parse_model(value: object, default: str = DEFAULT_ZHITU360_MODEL) -> str:
    text = str(value if value is not None else default).strip() or default
    lowered = text.lower()
    if lowered in MODEL_ALIASES:
        return MODEL_ALIASES[lowered]
    if text in MODEL_ALIASES:
        return MODEL_ALIASES[text]
    if lowered in MODELS:
        return lowered
    raise Zhitu360Error(
        status_code=400,
        detail={"error": f"model must be one of {', '.join(MODELS)}", "got": text},
    )


def parse_ratio(value: object, default: str = "1:1") -> str:
    text = str(value if value is not None else default).strip() or default
    if text in {"square", "1"}:
        text = "1:1"
    if text not in RATIOS:
        raise Zhitu360Error(
            status_code=400,
            detail={"error": f"ratio must be one of {', '.join(RATIOS)}"},
        )
    return text


def _api(cfg: dict[str, Any], path: str) -> str:
    return str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/") + path


def _errno_and_message(payload: dict[str, Any]) -> tuple[int | None, str]:
    """Return (errno, message) or (None, "") when the payload reports success."""
    errno = payload.get("errno")
    if errno in (None, 0, "0"):
        return None, ""
    try:
        code = int(errno)
    except (TypeError, ValueError):
        code = -1
    return code, str(payload.get("message") or payload.get("msg") or payload)


def _is_captcha_payload(payload: dict[str, Any]) -> bool:
    """Single source of truth for "this response wants human verification"."""
    code, msg = _errno_and_message(payload)
    if code is None:
        return False
    return code in CAPTCHA_ERRNOS or "验证" in msg or "人机" in msg


def _raise_errno(payload: dict[str, Any]) -> None:
    code, msg = _errno_and_message(payload)
    if code is None:
        return
    if code == 20601:
        raise Zhitu360Error(
            status_code=401,
            detail={"error": msg, "errno": code, "hint": "set zhitu360.cookies from image.360.com QHPass login"},
        )
    if code == 20603:
        raise Zhitu360Error(
            status_code=402,
            detail={"error": msg, "errno": code, "hint": "会员或下载豆不足"},
        )
    if _is_captcha_payload(payload):
        raise Zhitu360Error(
            status_code=403,
            detail={"error": msg, "errno": code, "captcha_required": True},
        )
    raise Zhitu360Error(status_code=502, detail={"error": msg, "errno": code, "data": payload.get("data")})


def fetch_config(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    session, cfg = _session(settings)
    timeout = max(10, int(cfg.get("timeout_sec") or 180))
    response = session.get(
        _api(cfg, "/api/v1/zhitu/text/to/image/v2/config"),
        timeout=min(30, timeout),
    )
    try:
        payload = response.json()
    except Exception as exc:
        raise Zhitu360Error(status_code=502, detail={"error": f"config JSON: {exc}"}) from exc
    if not isinstance(payload, dict):
        raise Zhitu360Error(status_code=502, detail={"error": "config not object"})
    _raise_errno(payload)
    return payload.get("data") if isinstance(payload.get("data"), dict) else payload


def fetch_user_status(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    session, cfg = _session(settings)
    timeout = max(10, int(cfg.get("timeout_sec") or 180))
    response = session.get(_api(cfg, "/v1/sale/user_status"), timeout=min(20, timeout))
    try:
        payload = response.json()
    except Exception:
        payload = {"errno": -1, "message": response.text[:200]}
    return payload if isinstance(payload, dict) else {"raw": payload}


def probe(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = settings or _settings()
    models: list[dict[str, Any]] = []
    try:
        data = fetch_config(cfg)
        items = data.get("items") if isinstance(data, dict) else []
        for item in items or []:
            if isinstance(item, dict):
                models.append(
                    {
                        "name": item.get("name"),
                        "value": item.get("value"),
                        "is_default": item.get("is_default"),
                    }
                )
    except Zhitu360Error as exc:
        return {
            "upstream": str(cfg.get("base_url") or DEFAULT_BASE_URL),
            "ok": False,
            "error": exc.detail,
            "models": list(MODELS),
        }
    user = fetch_user_status(cfg)
    errno = user.get("errno")
    return {
        "upstream": str(cfg.get("base_url") or DEFAULT_BASE_URL),
        "ok": True,
        "logged_in": errno not in (20601, "20601"),
        "user_status": user,
        "models": models or [{"value": m} for m in MODELS],
        "ratios": list(RATIOS),
        "feature": FEATURE_TEXT2IMAGE,
        "captcha_passthrough": True,
    }


def create_task(
    prompt: str,
    *,
    model: str = DEFAULT_ZHITU360_MODEL,
    ratio: str = "1:1",
    style: str = "auto",
    n: int = 1,
    cookies: str = "",
    captcha: str = "",
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = str(prompt or "").strip()
    if not text:
        raise Zhitu360Error(status_code=400, detail={"error": "prompt is required"})
    model_id = parse_model(model)
    ratio_id = parse_ratio(ratio)
    count = max(1, min(4, int(n or 1)))

    cfg = dict(settings or _settings())
    if cookies.strip():
        cfg["cookies"] = cookies.strip()
    session, cfg = _session(cfg)
    timeout = max(30, int(cfg.get("timeout_sec") or 180))
    body = {
        "prompt": text,
        "promptText": text,
        "feature": str(cfg.get("feature") or FEATURE_TEXT2IMAGE),
        "api_user": str(cfg.get("api_user") or API_USER_DEFAULT),
        "ratio": ratio_id,
        "style": str(style or "auto"),
        "model": model_id,
        "photoNums": count,
        "srcg": str(cfg.get("srcg") or SRCG_DEFAULT),
    }
    if captcha.strip():
        body["captcha"] = captcha.strip()
        session.headers["x-captcha-token"] = captcha.strip()

    def _post() -> dict[str, Any]:
        response = session.post(
            _api(cfg, "/api/v1/zhitu/text/to/image/create"),
            json=body,
            timeout=timeout,
        )
        try:
            payload = response.json()
        except Exception as exc:
            raise Zhitu360Error(status_code=502, detail={"error": f"create JSON: {exc}"}) from exc
        if not isinstance(payload, dict):
            raise Zhitu360Error(status_code=502, detail={"error": "create not object"})
        return payload

    payload = _post()
    if _is_captcha_payload(payload) and not captcha.strip():
        try:
            token = obtain_captcha_token(
                website_url=PAGE_URL,
                website_key=str(cfg.get("captcha_site_key") or "").strip(),
                settings=cfg,
                extra={"srcg": str(cfg.get("srcg") or SRCG_DEFAULT)},
            )
            body["captcha"] = token
            session.headers["x-captcha-token"] = token
            payload = _post()
        except CaptchaError as exc:
            raise Zhitu360Error(
                status_code=403,
                detail={"error": str(exc), "captcha_required": True, "upstream": payload},
            ) from exc
    _raise_errno(payload)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    record_id = str(data.get("record_id") or data.get("recordId") or "").strip()
    if not record_id:
        raise Zhitu360Error(status_code=502, detail={"error": "create missing record_id", "data": data})
    return data


def query_task(record_id: str, *, settings: dict[str, Any] | None = None, cookies: str = "") -> ZhituResult:
    rid = str(record_id or "").strip()
    if not rid:
        raise Zhitu360Error(status_code=400, detail={"error": "record_id is required"})
    cfg = dict(settings or _settings())
    if cookies.strip():
        cfg["cookies"] = cookies.strip()
    session, cfg = _session(cfg)
    timeout = max(15, int(cfg.get("timeout_sec") or 180))
    response = session.post(
        _api(cfg, "/api/v1/zhitu/text/to/image/query"),
        json={"record_id": rid},
        timeout=timeout,
    )
    try:
        payload = response.json()
    except Exception as exc:
        raise Zhitu360Error(status_code=502, detail={"error": f"query JSON: {exc}"}) from exc
    if not isinstance(payload, dict):
        raise Zhitu360Error(status_code=502, detail={"error": "query not object"})
    _raise_errno(payload)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    task = data.get("task_result") if isinstance(data.get("task_result"), dict) else data
    status = int(task.get("status") if task.get("status") is not None else -1)
    result = ZhituResult(
        record_id=str(task.get("record_id") or rid),
        status=status,
        status_name=STATUS_NAMES.get(status, str(status)),
    )
    rows = task.get("result_list") if isinstance(task.get("result_list"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or row.get("img") or row.get("imgUrl") or "").strip()
        if url:
            result.images.append(ZhituImage(url=url))
    if not result.images:
        url = str(task.get("url") or "").strip()
        if url:
            result.images.append(ZhituImage(url=url))
    if status == RISK_STATUS:
        raise Zhitu360Error(
            status_code=403,
            detail={"error": "hit_risk", "captcha_required": True, "record_id": rid},
        )
    return result


def generate(
    prompt: str,
    *,
    model: str = DEFAULT_ZHITU360_MODEL,
    ratio: str = "1:1",
    style: str = "auto",
    n: int = 1,
    cookies: str = "",
    captcha: str = "",
    settings: dict[str, Any] | None = None,
    wait: bool = True,
) -> ZhituResult:
    cfg = dict(settings or _settings())
    data = create_task(
        prompt,
        model=model,
        ratio=ratio,
        style=style,
        n=n,
        cookies=cookies,
        captcha=captcha,
        settings=cfg,
    )
    record_id = str(data.get("record_id") or "")
    if not wait:
        return ZhituResult(record_id=record_id, status=0, status_name="create_task")
    timeout = max(30, int(cfg.get("timeout_sec") or 180))
    interval = max(1, int(cfg.get("poll_interval_sec") or 2))
    deadline = time.time() + timeout
    last = ZhituResult(record_id=record_id)
    while time.time() < deadline:
        last = query_task(record_id, settings=cfg, cookies=cookies)
        if last.status not in PENDING_STATUSES:
            if last.status != SUCCESS_STATUS:
                raise Zhitu360Error(
                    status_code=502,
                    detail={
                        "error": f"zhitu status {last.status_name}",
                        "record_id": record_id,
                        "status": last.status,
                    },
                )
            if not last.images:
                raise Zhitu360Error(
                    status_code=502,
                    detail={"error": "success without images", "record_id": record_id},
                )
            return last
        time.sleep(interval)
    raise Zhitu360Error(
        status_code=504,
        detail={"error": "zhitu poll timeout", "record_id": record_id, "last_status": last.status_name},
    )
