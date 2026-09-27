"""OpenAI-shaped image generations for the cookie-based CN backends.

Shared by ``POST /v1/images/generations`` and the web UI's task route
(``/api/image-tasks/generations``) so both accept the same models and return
the same shape. They used to live as closures inside api/ai.py, which is why
the web UI rejected doubao/jimeng models that the OpenAI endpoint served.
"""
from __future__ import annotations

import base64
import time
from typing import Any

from utils.image_models import DEFAULT_ZHITU360_MODEL, is_doubao_model, is_zhitu_model


def openai_image_items(
    images: list[Any],
    response_format: str = "url",
    base_url: str | None = None,
) -> list[dict[str, str]]:
    """One OpenAI data entry per upstream image.

    This used to emit every URL, then every base64 blob, as separate entries — so
    an image the upstream returned in both forms came back twice ("one request,
    two identical images"). Base64 images are also saved to local storage here,
    which gives them a stable URL for the gallery and the call log instead of an
    expiring CDN link.
    """
    fmt = str(response_format or "url").strip().lower()
    items: list[dict[str, str]] = []
    for image in images:
        upstream_url = str(getattr(image, "url", "") or "").strip()
        b64_json = str(getattr(image, "b64_json", "") or "").strip()
        url = upstream_url
        if b64_json:
            try:
                from services.image_storage_service import image_storage_service

                url = image_storage_service.save(base64.b64decode(b64_json), base_url).url
            except Exception:
                url = upstream_url
        if not url and not b64_json:
            continue
        entry: dict[str, str] = {}
        if fmt == "b64_json" and b64_json:
            entry["b64_json"] = b64_json
        if url:
            entry["url"] = url
        elif b64_json:
            entry["b64_json"] = b64_json
        items.append(entry)
    return items


def is_cn_image_model(model: object) -> bool:
    return is_doubao_model(model) or is_zhitu_model(model)


def handle_doubao(body: dict[str, Any]) -> dict[str, Any]:
    from services.doubao_backend import generate

    result = generate(
        str(body.get("prompt") or ""),
        cookies=str(body.get("cookies") or ""),
        captcha=str(body.get("captcha") or ""),
        a_bogus=str(body.get("a_bogus") or ""),
    )
    return {
        "created": int(time.time()),
        "data": openai_image_items(result.images, str(body.get("response_format") or "url"), body.get("base_url")),
        "conversation_id": result.conversation_id,
    }


def handle_zhitu(body: dict[str, Any]) -> dict[str, Any]:
    from services.zhitu360_backend import generate, size_to_ratio

    result = generate(
        str(body.get("prompt") or ""),
        model=str(body.get("model") or DEFAULT_ZHITU360_MODEL),
        ratio=size_to_ratio(body.get("ratio") or body.get("size") or "1:1"),
        style=str(body.get("style") or "auto"),
        n=int(body.get("n") or 1),
        cookies=str(body.get("cookies") or ""),
        captcha=str(body.get("captcha") or ""),
    )
    return {
        "created": int(time.time()),
        # 360 returns CDN URLs only; there is no base64 to honor b64_json with.
        "data": openai_image_items(result.images, "url", body.get("base_url")),
        "record_id": result.record_id,
        "status": result.status,
        "status_name": result.status_name,
    }


def handle(body: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a doubao*/zhitu model; callers check is_cn_image_model first."""
    if is_doubao_model(body.get("model")):
        return handle_doubao(body)
    if is_zhitu_model(body.get("model")):
        return handle_zhitu(body)
    raise ValueError(f"not a CN image model: {body.get('model')!r}")
