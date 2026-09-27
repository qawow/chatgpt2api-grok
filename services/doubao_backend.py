"""Unofficial Doubao (豆包) image client wrapping www.doubao.com/chat/completion.

Protocol (from chat.fa93312a.js + live probes):

    POST https://www.doubao.com/chat/completion?aid=497858&...
    JSON body with messages[].content_type=2001 and skill ImageGeneration=4
    SSE / JSON stream; login required (code 710012001 without cookies).

webmssdk (a_bogus) signs /chat/completion. Pass ``a_bogus`` if the upstream
rejects unsigned requests. Human verification is **not** solved here: pass
``captcha`` or configure the shared captcha_solver webhook / SaaS keys.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException

from services.captcha_solver import CaptchaError, obtain_captcha_token
from utils.curl_tls import create_cffi_session

DEFAULT_BASE_URL = "https://www.doubao.com"
DEFAULT_AID = "497858"
SKILL_IMAGE_GENERATION = 4
CONTENT_TYPE_TEXT = 2001
PAGE_URL = "https://www.doubao.com/chat/create-image"

LOGIN_EXPIRED = {710012001, 710012002}
_IMAGE_URL_RE = re.compile(r"https?://[^\s\"'\\<>]+\.(?:png|jpe?g|webp|gif)", re.I)
_CAPTCHA_HINTS = ("captcha", "verify", "人机", "验证码", "webmssdk", "risk")


class DoubaoError(HTTPException):
    """Upstream or protocol error mapped to an HTTP status."""


@dataclass
class DoubaoImage:
    url: str = ""
    b64_json: str = ""


@dataclass
class DoubaoResult:
    images: list[DoubaoImage] = field(default_factory=list)
    conversation_id: str = ""
    message_id: str = ""


def _settings() -> dict[str, Any]:
    from services.config import config

    return config.get_doubao_settings()


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
    return session, cfg


def _completion_url(cfg: dict[str, Any], extra: dict[str, str] | None = None) -> str:
    base = str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    params = {
        "aid": str(cfg.get("aid") or DEFAULT_AID),
        "device_platform": "web",
        "language": "zh",
        "pc_version": "2.28.0",
        "pkg_type": "package_type_bytedance_com",
        "real_aid": str(cfg.get("aid") or DEFAULT_AID),
        "region": "CN",
        "samantha_web": "1",
        "sys_region": "CN",
        "use-olympus-account": "1",
        "version_code": "20800",
    }
    a_bogus = str(cfg.get("a_bogus") or "").strip()
    if extra:
        params.update({k: v for k, v in extra.items() if v})
    if a_bogus:
        params["a_bogus"] = a_bogus
    return f"{base}/chat/completion?{urlencode(params)}"


def _message_payload(prompt: str) -> dict[str, Any]:
    local_id = f"local_{uuid.uuid4().hex}"
    return {
        "messages": [
            {
                "content": json.dumps({"text": prompt}, ensure_ascii=False),
                "content_type": CONTENT_TYPE_TEXT,
                "attachments": [],
            }
        ],
        "completion_option": {
            "is_regen": False,
            "with_suggest": False,
            "need_create_conversation": True,
            "launch_stage": 1,
        },
        "skill": {
            "skill_type": SKILL_IMAGE_GENERATION,
            "skill_id": str(SKILL_IMAGE_GENERATION),
        },
        "local_message_id": local_id,
    }


def _looks_like_captcha(payload: Any) -> bool:
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    lowered = text.lower()
    return any(hint in lowered or hint in text for hint in _CAPTCHA_HINTS)


def _raise_from_payload(payload: dict[str, Any]) -> None:
    code = payload.get("code")
    if isinstance(code, str) and code.isdigit():
        code = int(code)
    msg = str(payload.get("msg") or payload.get("message") or payload.get("error") or payload)
    if isinstance(payload.get("error"), dict):
        msg = str(payload["error"].get("message") or msg)
        inner = payload["error"].get("code")
        if inner is not None and code is None:
            code = inner
    if code in LOGIN_EXPIRED or "登录" in msg:
        raise DoubaoError(
            status_code=401,
            detail={"error": msg, "code": code, "hint": "set doubao.cookies from www.doubao.com"},
        )
    if _looks_like_captcha(payload):
        raise DoubaoError(
            status_code=403,
            detail={
                "error": msg,
                "code": code,
                "captcha_required": True,
                "hint": "pass captcha token, or configure solve_url / CAPSOLVER_KEY",
            },
        )
    if code not in (None, 0, "0"):
        raise DoubaoError(status_code=502, detail={"error": msg, "code": code})


def _is_captcha_error(exc: DoubaoError) -> bool:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    return bool(detail.get("captcha_required"))


def _walk_images(node: Any, found: list[str]) -> None:
    if isinstance(node, str):
        if node.startswith("data:image") and ";base64," in node:
            found.append(node)
            return
        if node.startswith("http") and any(
            marker in node for marker in ("imagex", "byteimg", "doubao", "bytedance", "ibyteimg")
        ):
            found.append(node)
            return
        for match in _IMAGE_URL_RE.findall(node):
            found.append(match)
        return
    if isinstance(node, dict):
        for key in ("url", "image_url", "imageUrl", "origin_url", "thumb_url", "key"):
            value = node.get(key)
            if isinstance(value, str) and value.startswith("http"):
                found.append(value)
        for value in node.values():
            _walk_images(value, found)
        return
    if isinstance(node, list):
        for item in node:
            _walk_images(item, found)


def _parse_sse(text: str) -> DoubaoResult:
    result = DoubaoResult()
    urls: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            _walk_images(data, urls)
            continue
        if isinstance(payload, dict):
            if payload.get("code") not in (None, 0, "0"):
                _raise_from_payload(payload)
            conv = payload.get("conversation_id") or payload.get("conversationId")
            if conv:
                result.conversation_id = str(conv)
            msg = payload.get("message") if isinstance(payload.get("message"), dict) else {}
            if msg.get("message_id"):
                result.message_id = str(msg.get("message_id"))
        _walk_images(payload, urls)
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        if url.startswith("data:image") and ";base64," in url:
            result.images.append(DoubaoImage(b64_json=url.split(";base64,", 1)[1]))
        else:
            result.images.append(DoubaoImage(url=url))
    return result


def _json_or_none(response: Any) -> Any:
    try:
        payload = response.json()
    except Exception:
        return None
    return payload


def _consume(response: Any, body_text: str, payload: Any) -> DoubaoResult:
    """Map one upstream answer to images, raising DoubaoError on failure.

    ``/chat/completion`` answers with SSE, so the interesting errors (captcha
    walls included) only surface after the stream is parsed — ``response.json()``
    fails on the happy path as well as on most error paths.
    """
    if isinstance(payload, dict) and payload.get("code") not in (None, 0, "0"):
        _raise_from_payload(payload)
    if response.status_code in {401, 403} and isinstance(payload, dict):
        _raise_from_payload(payload)
    if response.status_code >= 400 and payload is None:
        detail: dict[str, Any] = {
            "error": f"doubao HTTP {response.status_code}",
            "body": body_text[:500],
        }
        if _looks_like_captcha(body_text):
            detail["captcha_required"] = True
            raise DoubaoError(status_code=403, detail=detail)
        raise DoubaoError(status_code=502, detail=detail)

    result = _parse_sse(body_text)
    if not result.images and isinstance(payload, dict):
        urls: list[str] = []
        _walk_images(payload, urls)
        for url in urls:
            result.images.append(DoubaoImage(url=url) if url.startswith("http") else DoubaoImage(b64_json=url))
    if not result.images:
        if _looks_like_captcha(body_text):
            raise DoubaoError(
                status_code=403,
                detail={
                    "error": "豆包要求人机验证",
                    "captcha_required": True,
                    "body": body_text[:500],
                },
            )
        raise DoubaoError(
            status_code=502,
            detail={"error": "豆包未返回图片。检查 Cookie 是否有效，或上游是否要求 a_bogus / 人机验证。"},
        )
    return result


def probe_login(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Cheap probe: unsigned empty completion should 401 without cookies."""
    session, cfg = _session(settings)
    timeout = max(10, int(cfg.get("timeout_sec") or 180))
    response = session.post(
        _completion_url(cfg),
        json={"messages": []},
        headers={
            "Content-Type": "application/json",
            "Origin": str(cfg.get("base_url") or DEFAULT_BASE_URL),
            "Referer": PAGE_URL,
            "agw-js-conv": "str",
        },
        timeout=min(30, timeout),
    )
    try:
        payload = response.json()
    except Exception:
        payload = {"raw": response.text[:400]}
    code = payload.get("code") if isinstance(payload, dict) else None
    logged_in = not (code in LOGIN_EXPIRED or response.status_code in {401, 403})
    if isinstance(payload, dict) and code in LOGIN_EXPIRED:
        logged_in = False
    return {
        "upstream": str(cfg.get("base_url") or DEFAULT_BASE_URL),
        "http_status": response.status_code,
        "logged_in": bool(str(cfg.get("cookies") or "").strip()) and logged_in,
        "code": code,
        "message": (payload.get("msg") or payload.get("message")) if isinstance(payload, dict) else None,
        "captcha_passthrough": True,
        "skill_image_generation": SKILL_IMAGE_GENERATION,
    }


def generate(
    prompt: str,
    *,
    cookies: str = "",
    captcha: str = "",
    a_bogus: str = "",
    settings: dict[str, Any] | None = None,
) -> DoubaoResult:
    text = str(prompt or "").strip()
    if not text:
        raise DoubaoError(status_code=400, detail={"error": "prompt is required"})

    cfg = dict(settings or _settings())
    if cookies.strip():
        cfg["cookies"] = cookies.strip()
    if a_bogus.strip():
        cfg["a_bogus"] = a_bogus.strip()
    if not str(cfg.get("cookies") or "").strip():
        raise DoubaoError(
            status_code=401,
            detail={"error": "豆包网页接口需要登录 Cookie（sessionid / ttwid 等）", "captcha_required": False},
        )

    session, cfg = _session(cfg)
    timeout = max(30, int(cfg.get("timeout_sec") or 180))
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
        "Origin": str(cfg.get("base_url") or DEFAULT_BASE_URL),
        "Referer": PAGE_URL,
        "agw-js-conv": "str",
    }
    if captcha.strip():
        headers["x-captcha-token"] = captcha.strip()

    def _post() -> Any:
        return session.post(
            _completion_url(cfg),
            json=_message_payload(text),
            headers=headers,
            timeout=timeout,
        )

    response = _post()
    body_text = response.text or ""
    try:
        return _consume(response, body_text, _json_or_none(response))
    except DoubaoError as exc:
        # Only a captcha wall is retryable, and only once: everything else
        # (login expired, quota, upstream 5xx) would just fail the same way.
        if captcha.strip() or not _is_captcha_error(exc):
            raise
        first = exc

    try:
        token = obtain_captcha_token(
            website_url=PAGE_URL,
            website_key=str(cfg.get("captcha_site_key") or "").strip(),
            settings=cfg,
            explicit="",
            task_type=str(cfg.get("captcha_task_type") or ""),
        )
    except CaptchaError as exc:
        raise DoubaoError(
            status_code=403,
            detail={"error": str(exc), "captcha_required": True, "upstream": first.detail},
        ) from exc

    headers["x-captcha-token"] = token
    response = _post()
    return _consume(response, response.text or "", _json_or_none(response))
