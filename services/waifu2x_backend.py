"""Unofficial client for the public www.waifu2x.net web form.

Protocol (from ui.js + nunif/waifu2x/web/server.py):

    POST https://www.waifu2x.net/api
    multipart/form-data
        file | url
        style   art | art_scan | photo
        noise   -1 none, 0 low, 1 medium, 2 high, 3 highest
        scale   -1 none, 1 = 1.6x, 2 = 2x
        format  0 png, 1 webp
        turnstile / recap
"""
from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from fastapi import HTTPException

from services.waifu2x_turnstile import (
    TURNSTILE_SITEKEY,
    captcha_required,
    obtain_turnstile_token,
    turnstile_site_key,
    TurnstileError,
)
from utils.curl_tls import create_cffi_session
from utils.ssrf import UnsafeUrlError, assert_safe_url

DEFAULT_BASE_URL = "https://www.waifu2x.net"
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_NOISE_PIXELS = 3000 * 3000
MAX_SCALE_PIXELS = int((math.sqrt(MAX_NOISE_PIXELS) / 2) ** 2)

STYLES = ("art", "art_scan", "photo")
NOISE_ALIASES = {
    "none": -1,
    "off": -1,
    "disable": -1,
    "-1": -1,
    "low": 0,
    "0": 0,
    "medium": 1,
    "mid": 1,
    "1": 1,
    "high": 2,
    "2": 2,
    "highest": 3,
    "max": 3,
    "3": 3,
}
SCALE_ALIASES = {
    "none": -1,
    "off": -1,
    "1x": -1,
    "1.0": -1,
    "-1": -1,
    "1.6": 1,
    "1.6x": 1,
    "1": 1,
    "2": 2,
    "2.0": 2,
    "2x": 2,
}
FORMAT_ALIASES = {
    "png": 0,
    "0": 0,
    "webp": 1,
    "webP": 1,
    "1": 1,
}

_DISPOSITION_RE = re.compile(
    r"filename\*?=(?:UTF-8''|utf-8'')?(\"?)([^\";]+)\1",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title\b[^>]*>([\s\S]*?)</title>", re.IGNORECASE)
_PRE_RE = re.compile(r"<body\b[^>]*>[\s\S]*?<pre\b[^>]*>([\s\S]*?)</pre>", re.IGNORECASE)


class Waifu2xError(HTTPException):
    """Upstream or protocol error mapped to an HTTP status."""


@dataclass(frozen=True)
class Waifu2xOptions:
    style: str
    noise: int
    scale: int
    format: int

    @property
    def format_name(self) -> str:
        return "webp" if self.format == 1 else "png"

    @property
    def scale_label(self) -> str:
        if self.scale == 1:
            return "1.6x"
        if self.scale == 2:
            return "2x"
        return "1x"

    @property
    def noise_label(self) -> str:
        return { -1: "none", 0: "low", 1: "medium", 2: "high", 3: "highest" }.get(self.noise, str(self.noise))


@dataclass
class ConvertResult:
    content: bytes
    content_type: str
    filename: str
    options: Waifu2xOptions


def parse_style(value: object, default: str = "art") -> str:
    text = str(value if value is not None else default).strip().lower() or default
    if text in {"scan", "manga", "art-scan", "artscan"}:
        text = "art_scan"
    if text not in STYLES:
        raise Waifu2xError(status_code=400, detail={"error": f"style must be one of {', '.join(STYLES)}"})
    return text


def parse_noise(value: object, default: int = 1) -> int:
    if value is None or value == "":
        return default
    key = str(value).strip().lower()
    if key in NOISE_ALIASES:
        return NOISE_ALIASES[key]
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise Waifu2xError(status_code=400, detail={"error": "noise must be none/low/medium/high/highest or -1..3"}) from exc
    if parsed not in {-1, 0, 1, 2, 3}:
        raise Waifu2xError(status_code=400, detail={"error": "noise must be -1..3"})
    return parsed


def parse_scale(value: object, default: int = 2) -> int:
    if value is None or value == "":
        return default
    key = str(value).strip().lower()
    if key in SCALE_ALIASES:
        return SCALE_ALIASES[key]
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise Waifu2xError(status_code=400, detail={"error": "scale must be none/1x/1.6x/2x or -1/1/2"}) from exc
    if parsed in {-1, 1, 2}:
        return int(parsed)
    if parsed == 1.6:
        return 1
    raise Waifu2xError(status_code=400, detail={"error": "scale must be none/1x/1.6x/2x or -1/1/2"})


def parse_format(value: object, default: int = 0) -> int:
    if value is None or value == "":
        return default
    key = str(value).strip()
    alias = FORMAT_ALIASES.get(key.lower())
    if alias is not None:
        return alias
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise Waifu2xError(status_code=400, detail={"error": "format must be png or webp"}) from exc
    if parsed not in {0, 1}:
        raise Waifu2xError(status_code=400, detail={"error": "format must be 0 (png) or 1 (webp)"})
    return parsed


def parse_options(
    *,
    style: object = "art",
    noise: object = 1,
    scale: object = 2,
    image_format: object = 0,
) -> Waifu2xOptions:
    options = Waifu2xOptions(
        style=parse_style(style),
        noise=parse_noise(noise),
        scale=parse_scale(scale),
        format=parse_format(image_format),
    )
    if options.scale == -1 and options.noise == -1:
        raise Waifu2xError(status_code=400, detail={"error": "choose at least noise reduction or upscaling"})
    return options


def parse_error_html(text: str, status: int | None = None) -> str:
    title_match = _TITLE_RE.search(text or "")
    pre_match = _PRE_RE.search(text or "")
    title = title_match.group(1).strip() if title_match else ""
    body_pre = pre_match.group(1).strip() if pre_match else ""
    parts = [part for part in (title, body_pre) if part]
    if parts:
        return "\n".join(parts)
    stripped = (text or "").strip()
    if stripped:
        return stripped[:1000]
    if status:
        return f"HTTP Error ({status})"
    return "unknown waifu2x error"


def extract_filename(disposition: str | None, default: str) -> str:
    if not disposition:
        return default
    match = _DISPOSITION_RE.search(disposition)
    if not match:
        return default
    return unquote(match.group(2).strip()) or default


def _settings() -> dict[str, Any]:
    from services.config import config

    return config.get_waifu2x_settings()


def _session(settings: dict[str, Any] | None = None):
    from services.proxy_service import proxy_settings

    cfg = settings or _settings()
    kwargs = proxy_settings.build_session_kwargs(impersonate="chrome142", verify=True, upstream=True)
    return create_cffi_session(**kwargs), cfg


def _headers(cfg: dict[str, Any]) -> dict[str, str]:
    base = str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    ua = str(cfg.get("user_agent") or "").strip()
    headers = {
        "Origin": base,
        "Referer": base + "/",
        "Accept": "*/*",
    }
    if ua:
        headers["User-Agent"] = ua
    ses_id = str(cfg.get("ses_id") or "").strip()
    if ses_id:
        headers["Cookie"] = f"ses_id={ses_id}"
    return headers


def fetch_captcha_state(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    session, cfg = _session(settings)
    base = str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    headers = _headers(cfg)
    timeout = max(10, int(cfg.get("timeout_sec") or 180))
    last_error = "empty response"
    for path in ("/recaptcha_state.json", "/recaptcha_state_static.json"):
        try:
            response = session.get(base + path, headers=headers, timeout=min(30, timeout))
        except Exception as exc:
            last_error = str(exc)
            continue
        if response.status_code != 200:
            last_error = f"HTTP {response.status_code}"
            continue
        try:
            data = response.json()
        except Exception as exc:
            last_error = str(exc)
            continue
        if isinstance(data, dict):
            data.setdefault("turnstile_site_key", TURNSTILE_SITEKEY)
            return data
    raise Waifu2xError(status_code=502, detail={"error": f"failed to read waifu2x captcha state: {last_error}"})


def _validate_image(image: bytes, options: Waifu2xOptions) -> None:
    if not image:
        return
    if len(image) > MAX_BODY_BYTES:
        raise Waifu2xError(status_code=413, detail={"error": f"image exceeds {MAX_BODY_BYTES // (1024 * 1024)}MB limit"})
    try:
        from PIL import Image
    except Exception:
        return
    try:
        with Image.open(io.BytesIO(image)) as im:
            width, height = im.size
    except Exception as exc:
        raise Waifu2xError(status_code=400, detail={"error": f"cannot decode image: {exc}"}) from exc
    pixels = int(width) * int(height)
    limit = MAX_SCALE_PIXELS if options.scale != -1 else MAX_NOISE_PIXELS
    if pixels > limit:
        kind = "upscaling" if options.scale != -1 else "noise reduction"
        raise Waifu2xError(
            status_code=413,
            detail={"error": f"{kind} limit is {limit} pixels (got {width}x{height})"},
        )


def _maybe_convert_png(content: bytes, content_type: str, options: Waifu2xOptions) -> tuple[bytes, str]:
    if options.format != 0:
        return content, content_type
    if "webp" not in (content_type or "").lower() and not content[:4] == b"RIFF":
        return content, content_type
    try:
        from PIL import Image
    except Exception:
        return content, content_type
    try:
        with Image.open(io.BytesIO(content)) as im:
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return buf.getvalue(), "image/png"
    except Exception:
        return content, content_type


def upscale(
    *,
    image: bytes | None = None,
    filename: str = "image.png",
    url: str = "",
    style: object = "art",
    noise: object = 1,
    scale: object = 2,
    image_format: object = 0,
    turnstile: str = "",
    settings: dict[str, Any] | None = None,
) -> ConvertResult:
    options = parse_options(style=style, noise=noise, scale=scale, image_format=image_format)
    source_url = str(url or "").strip()
    payload = bytes(image or b"")
    if payload:
        _validate_image(payload, options)
    elif source_url:
        try:
            assert_safe_url(source_url)
        except UnsafeUrlError as exc:
            raise Waifu2xError(status_code=400, detail={"error": str(exc)}) from exc
    else:
        raise Waifu2xError(status_code=400, detail={"error": "file, image, or url is required"})

    session, cfg = _session(settings)
    base = str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    headers = _headers(cfg)
    timeout = max(30, int(cfg.get("timeout_sec") or 180))
    state = fetch_captcha_state(cfg)
    form = {
        "style": options.style,
        "noise": str(options.noise),
        "scale": str(options.scale),
        "format": str(options.format),
        "url": source_url if not payload else "",
        "recap": "",
        "turnstile": "",
    }

    token = str(turnstile or "").strip()
    if captcha_required(state) and not token:
        try:
            token = obtain_turnstile_token(
                site_key=turnstile_site_key(state),
                settings=cfg,
                explicit="",
            )
        except TurnstileError as exc:
            raise Waifu2xError(status_code=403, detail={"error": str(exc)}) from exc
    form["turnstile"] = token

    files = None
    if payload:
        files = {"file": (filename or "image.png", payload, _guess_mime(filename, payload))}

    def _send() -> Any:
        return session.post(
            base + "/api",
            data=form,
            files=files,
            headers=headers,
            timeout=timeout,
        )

    response = _send()
    # The state JSON lies about whether the widget is armed (and about the
    # field name it is armed under), so trust the upstream rejection instead:
    # solve once, resend once, never loop.
    if _turnstile_rejected(response):
        try:
            form["turnstile"] = obtain_turnstile_token(
                site_key=turnstile_site_key(state),
                settings=cfg,
                explicit="",
            )
        except TurnstileError as exc:
            if not token:
                raise Waifu2xError(status_code=403, detail={"error": str(exc)}) from exc
        else:
            response = _send()
    return _parse_api_response(response, options, filename)


def _turnstile_rejected(response: Any) -> bool:
    """True when the upstream refused the request for a missing/stale token.

    ``/api`` has no other credential, so a bare 401 always means the Turnstile
    field was rejected; a 403 only counts when the body says so.
    """
    status = int(getattr(response, "status_code", 0) or 0)
    if status not in {401, 403}:
        return False
    _, _, text = _response_body(response)
    lowered = text.lower()
    if status == 401:
        return True
    return "turnstile" in lowered or "recaptcha" in lowered or "captcha" in lowered


def _guess_mime(filename: str, payload: bytes) -> str:
    name = (filename or "").lower()
    if payload[:8] == b"\x89PNG\r\n\x1a\n" or name.endswith(".png"):
        return "image/png"
    if payload[:2] == b"\xff\xd8" or name.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if payload[:4] == b"RIFF" or name.endswith(".webp"):
        return "image/webp"
    if payload[:6] in {b"GIF87a", b"GIF89a"} or name.endswith(".gif"):
        return "image/gif"
    return "application/octet-stream"


def _response_body(response: Any) -> tuple[str, bytes, str]:
    """Return (content_type, raw bytes, decoded text) for a waifu2x answer."""
    content_type = str((getattr(response, "headers", {}) or {}).get("content-type") or "")
    body = getattr(response, "content", None) or getattr(response, "text", b"")
    if isinstance(body, str):
        return content_type, body.encode("utf-8", errors="replace"), body
    raw = bytes(body or b"")
    text = ""
    if "text/" in content_type or "json" in content_type or raw[:1] in {b"<", b"{"}:
        text = raw.decode("utf-8", errors="replace")
    return content_type, raw, text


def _map_error_status(status: int, message: str) -> int:
    lowered = message.lower()
    if "turnstile" in lowered or "recaptcha" in lowered:
        return 403
    if status in {400, 401, 403, 413, 429, 500, 502, 503, 504}:
        return status
    if 400 <= status < 500:
        return status
    # An error page served with 200/3xx (or no status at all) must not be
    # handed back as a success code — clients would treat it as an image.
    return 502


def _parse_api_response(response: Any, options: Waifu2xOptions, filename: str) -> ConvertResult:
    status = int(getattr(response, "status_code", 0) or 0)
    content_type, raw, text = _response_body(response)

    if status != 200 or not raw or raw[:1] == b"<":
        message = parse_error_html(text, status) if text else f"HTTP Error ({status})"
        raise Waifu2xError(status_code=_map_error_status(status, message), detail={"error": message})

    if raw[:8] != b"\x89PNG\r\n\x1a\n" and raw[:4] != b"RIFF" and raw[:2] != b"\xff\xd8":
        message = parse_error_html(text or raw.decode("utf-8", errors="replace"), status)
        raise Waifu2xError(status_code=502, detail={"error": message})

    out, out_type = _maybe_convert_png(raw, content_type, options)
    default_name = _default_filename(filename, options, out_type)
    name = extract_filename((getattr(response, "headers", {}) or {}).get("content-disposition"), default_name)
    if options.format == 0 and not name.lower().endswith(".png"):
        name = re.sub(r"\.webp$", ".png", name, flags=re.IGNORECASE)
        if not name.lower().endswith(".png"):
            name += ".png"
    return ConvertResult(content=out, content_type=out_type or "image/png", filename=name, options=options)


def _default_filename(filename: str, options: Waifu2xOptions, content_type: str) -> str:
    base = re.sub(r"\.[A-Za-z0-9]+$", "", filename or "image") or "image"
    ext = "webp" if "webp" in (content_type or "").lower() and options.format == 1 else options.format_name
    if options.scale == -1:
        mode = f"{options.style}_noise{options.noise}" if options.noise != -1 else "none"
    elif options.noise == -1:
        mode = f"{options.style}_scale"
    else:
        mode = f"{options.style}_noise{options.noise}_scale"
    return f"{base}_waifu2x_{mode}.{ext}"
