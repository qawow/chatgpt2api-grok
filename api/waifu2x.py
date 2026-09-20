from __future__ import annotations

import base64
import binascii
import json
import time
from typing import Any
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from starlette.datastructures import UploadFile

from api.support import require_identity
from services.log_service import LoggedCall
from services.waifu2x_backend import (
    ConvertResult,
    Waifu2xError,
    fetch_captcha_state,
    parse_format,
    parse_noise,
    parse_scale,
    parse_style,
    upscale,
)
from services.waifu2x_turnstile import captcha_required, turnstile_site_key

_DATA_URL_RE_PREFIX = "data:"


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/v1/waifu2x/status")
    async def waifu2x_status(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        try:
            state = await run_in_threadpool(fetch_captcha_state)
        except Waifu2xError:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc
        return {
            "upstream": "https://www.waifu2x.net/api",
            "turnstile_enabled": captcha_required(state),
            "turnstile_site_key": turnstile_site_key(state),
            "logged_in": bool(state.get("logged_in")),
            "meter": state.get("meter") or 0,
            "meter_max": state.get("meter_max") or 0,
            "limits": {
                "max_bytes": 5 * 1024 * 1024,
                "noise_pixels": 3000 * 3000,
                "scale_pixels": 1500 * 1500,
            },
            "styles": ["art", "art_scan", "photo"],
            "noise": {"none": -1, "low": 0, "medium": 1, "high": 2, "highest": 3},
            "scale": {"none": -1, "1.6x": 1, "2x": 2},
            "format": {"png": 0, "webp": 1},
        }

    @router.get("/v1/waifu2x")
    async def waifu2x_help(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        return {
            "endpoint": "POST /v1/waifu2x",
            "alias": "POST /v1/images/upscale",
            "description": "Unofficial wrapper of the public www.waifu2x.net convert form.",
            "body": {
                "file": "multipart image (or JSON image / url)",
                "style": "art | art_scan | photo",
                "noise": "none | low | medium | high | highest",
                "scale": "none | 1x | 1.6x | 2x  (1 = 1.6x, matching the website)",
                "format": "png | webp",
                "response_format": "binary | b64_json",
                "turnstile": "optional Cloudflare Turnstile token from the website widget",
                "url": "optional public http(s) image URL fetched by waifu2x.net",
            },
        }

    @router.post("/v1/waifu2x")
    @router.post("/v1/images/upscale")
    async def waifu2x_upscale(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        try:
            payload = await _read_request(request)
        except HTTPException:
            raise
        except Waifu2xError:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

        call = LoggedCall(
            identity,
            "/v1/waifu2x",
            "waifu2x",
            "waifu2x超分",
            request_text=f"{payload.get('filename') or payload.get('url') or 'image'} "
            f"style={payload.get('style')} noise={payload.get('noise')} scale={payload.get('scale')}",
        )
        try:
            result: ConvertResult = await run_in_threadpool(_run_upscale, payload)
        except Waifu2xError as exc:
            call.log("调用失败", status="failed", error=str(exc.detail))
            raise
        except HTTPException as exc:
            call.log("调用失败", status="failed", error=str(exc.detail))
            raise
        except Exception as exc:
            call.log("调用失败", status="failed", error=str(exc))
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

        call.log(
            "调用完成",
            {
                "filename": result.filename,
                "bytes": len(result.content),
                "style": result.options.style,
                "noise": result.options.noise_label,
                "scale": result.options.scale_label,
            },
        )
        if payload["response_format"] == "binary":
            return Response(
                content=result.content,
                media_type=result.content_type,
                headers={
                    "Content-Disposition": f'inline; filename="{result.filename}"',
                    "X-Waifu2x-Style": result.options.style,
                    "X-Waifu2x-Noise": result.options.noise_label,
                    "X-Waifu2x-Scale": result.options.scale_label,
                },
            )
        return {
            "created": int(time.time()),
            "model": "waifu2x",
            "style": result.options.style,
            "noise": result.options.noise_label,
            "scale": result.options.scale_label,
            "data": [
                {
                    "b64_json": base64.b64encode(result.content).decode("ascii"),
                    "filename": result.filename,
                    "content_type": result.content_type,
                }
            ],
        }

    return router


def _run_upscale(payload: dict[str, Any]) -> ConvertResult:
    return upscale(
        image=payload.get("image"),
        filename=payload.get("filename") or "image.png",
        url=payload.get("url") or "",
        style=payload.get("style"),
        noise=payload.get("noise"),
        scale=payload.get("scale"),
        image_format=payload.get("format"),
        turnstile=payload.get("turnstile") or "",
    )


async def _read_request(request: Request) -> dict[str, Any]:
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid JSON"}) from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail={"error": "JSON body must be an object"})
        return await _payload_from_fields(body, uploads={})

    form = await request.form()
    fields: dict[str, Any] = {}
    uploads: dict[str, UploadFile] = {}
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            uploads[key] = value
        else:
            fields[key] = value
    return await _payload_from_fields(fields, uploads=uploads, default_response="binary")


async def _payload_from_fields(
    fields: dict[str, Any],
    *,
    uploads: dict[str, UploadFile],
    default_response: str = "b64_json",
) -> dict[str, Any]:
    image_bytes = b""
    filename = str(fields.get("filename") or "").strip()
    upload = uploads.get("file") or uploads.get("image")
    if upload is not None:
        image_bytes = await _read_upload_bytes(upload)
        filename = filename or (upload.filename or "image.png")

    raw_image = fields.get("image") or fields.get("image_b64") or fields.get("b64_json")
    url = str(fields.get("url") or fields.get("image_url") or "").strip()
    if not image_bytes and raw_image and not isinstance(raw_image, UploadFile):
        text = str(raw_image).strip()
        if text.startswith(("http://", "https://")) and not url:
            url = text
        else:
            decoded, decoded_name = _decode_image_field(text)
            if decoded:
                image_bytes = decoded
                filename = filename or decoded_name
    if url.lower().startswith(_DATA_URL_RE_PREFIX) and not image_bytes:
        decoded, decoded_name = _decode_image_field(url)
        image_bytes = decoded
        filename = filename or decoded_name
        url = ""

    response_format = str(fields.get("response_format") or default_response).strip().lower()
    if response_format in {"image", "raw", "bytes"}:
        response_format = "binary"
    if response_format not in {"binary", "b64_json"}:
        raise HTTPException(status_code=400, detail={"error": "response_format must be binary or b64_json"})

    return {
        "image": image_bytes or None,
        "filename": filename or "image.png",
        "url": url,
        "style": parse_style(fields.get("style") or "art"),
        "noise": parse_noise(fields.get("noise") if fields.get("noise") not in (None, "") else 1),
        "scale": parse_scale(fields.get("scale") if fields.get("scale") not in (None, "") else 2),
        "format": parse_format(fields.get("format") or "png"),
        "turnstile": str(fields.get("turnstile") or fields.get("cf-turnstile-response") or "").strip(),
        "response_format": response_format,
    }


async def _read_upload_bytes(upload: UploadFile) -> bytes:
    try:
        await upload.seek(0)
    except Exception:
        pass
    data = await upload.read()
    try:
        await upload.close()
    except Exception:
        pass
    if isinstance(data, str):
        return data.encode("utf-8")
    return bytes(data or b"")


def _decode_image_field(value: str) -> tuple[bytes, str]:
    text = str(value or "").strip()
    if not text:
        return b"", "image.png"
    if text.startswith(_DATA_URL_RE_PREFIX):
        header, _, payload = text.partition(",")
        mime = "image/png"
        if ";" in header:
            mime = header[5:].split(";", 1)[0] or mime
        name = "image.png"
        if "jpeg" in mime or "jpg" in mime:
            name = "image.jpg"
        elif "webp" in mime:
            name = "image.webp"
        try:
            return base64.b64decode(payload, validate=False), name
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid data-url image"}) from exc
    if text.startswith("{") and "url" in text:
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and obj.get("url"):
            return _decode_image_field(str(obj.get("url")))
    if text.startswith(("http://", "https://")):
        return b"", "image.png"
    try:
        padded = text + ("=" * (-len(text) % 4))
        decoded = base64.b64decode(padded, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"error": "image is not valid base64"}) from exc
    if len(decoded) < 8:
        raise HTTPException(status_code=400, detail={"error": "image is not valid base64"})
    return decoded, "image.png"
