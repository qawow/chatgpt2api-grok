"""OpenAI-compatible image generations for Grok.

Order:
1. If any grokcli2api-go connection is configured for image proxy → remote
   ``POST {base}/v1/responses`` + ``tools=[{type:image_generation}]``
   (0.4.x primary; falls back to ``/v1/images/generations`` if present).
2. Else local ``data/grok_accounts.json`` free Build path.

Never falls through to the ChatGPT account pool.
"""
from __future__ import annotations

import time
from typing import Any

from services.g2a_service import G2AClientError, g2a_bridge
from services.grok_account_service import grok_account_service
from services.grok_backend_api import GrokBackendError, b64_to_bytes, generate_image
from utils.grok_models import is_grok_image_model, resolve_grok_image_model


def _persist_urls(data_items: list[dict[str, Any]], *, base_url: str | None, response_format: str) -> list[dict[str, Any]]:
    try:
        from services.image_storage_service import image_storage_service
    except Exception:
        image_storage_service = None  # type: ignore[assignment]

    out: list[dict[str, Any]] = []
    for item in data_items:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        b64 = entry.get("b64_json")
        if b64 and image_storage_service is not None:
            try:
                stored = image_storage_service.save(b64_to_bytes(str(b64)), base_url=base_url)
                entry["url"] = stored.url
                if response_format == "url":
                    entry.pop("b64_json", None)
            except Exception:
                pass
        out.append(entry)
    return out


def _handle_via_g2a(body: dict[str, Any]) -> dict[str, Any] | None:
    """Return image result from G2A if proxy servers exist; else None."""
    if not g2a_bridge.has_image_proxy():
        return None
    prompt = str(body.get("prompt") or "").strip()
    model = resolve_grok_image_model(body.get("model"))
    n = max(1, min(int(body.get("n") or 1), 4))
    size = body.get("size")
    response_format = str(body.get("response_format") or "b64_json").strip() or "b64_json"
    base_url = str(body.get("base_url") or "").strip() or None
    server_id = str(body.get("g2a_server_id") or body.get("server_id") or "").strip() or None

    result = g2a_bridge.generate_image(
        prompt=prompt,
        model=model,
        n=n,
        size=str(size) if size else None,
        response_format=response_format,
        server_id=server_id,
    )
    data_items = _persist_urls(list(result.get("data") or []), base_url=base_url, response_format=response_format)
    if not data_items:
        raise RuntimeError("g2a image proxy returned empty data")
    out: dict[str, Any] = {
        "created": int(result.get("created") or time.time()),
        "data": data_items,
    }
    meta = result.get("_meta")
    if isinstance(meta, dict):
        out["_grok_meta"] = {"upstream": "g2a", **meta}
    else:
        out["_grok_meta"] = {"upstream": "g2a"}
    return out


def _handle_via_local_pool(body: dict[str, Any]) -> dict[str, Any]:
    prompt = str(body.get("prompt") or "").strip()
    model = resolve_grok_image_model(body.get("model"))
    n = max(1, min(int(body.get("n") or 1), 4))
    size = body.get("size")
    response_format = str(body.get("response_format") or "b64_json").strip() or "b64_json"
    base_url = str(body.get("base_url") or "").strip() or None

    data_items: list[dict[str, Any]] = []
    meta_attempts: list[Any] = []
    exclude: set[str] = set()
    last_error: str | None = None
    remaining = n

    while remaining > 0:
        account = grok_account_service.get_next_account(exclude_tokens=exclude)
        if account is None:
            if data_items:
                break
            raise RuntimeError("no available grok accounts in pool")
        token = str(account.get("access_token") or "")
        exclude.add(token)
        try:
            result = generate_image(
                account,
                prompt=prompt,
                model=model,
                n=remaining,
                size=str(size) if size else None,
                response_format=response_format,
            )
            meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
            meta_attempts.append(meta)
            added = 0
            for item in result.get("data") or []:
                if isinstance(item, dict):
                    data_items.append(item)
                    added += 1
            grok_account_service.mark_result(token, True)
            remaining = max(0, remaining - max(added, 1))
        except GrokBackendError as exc:
            # One forced refresh + retry on auth failures (token may have just expired mid-request).
            if getattr(exc, "status", None) in {401, 403}:
                try:
                    refreshed = grok_account_service.ensure_fresh_account(account, force=True)
                    new_token = str((refreshed or {}).get("access_token") or "")
                    if refreshed and new_token and new_token != token:
                        exclude.add(new_token)
                    if refreshed:
                        result = generate_image(
                            refreshed,
                            prompt=prompt,
                            model=model,
                            n=remaining,
                            size=str(size) if size else None,
                            response_format=response_format,
                        )
                        meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
                        meta_attempts.append({**meta, "retried_after_refresh": True})
                        added = 0
                        for item in result.get("data") or []:
                            if isinstance(item, dict):
                                data_items.append(item)
                                added += 1
                        grok_account_service.mark_result(new_token or token, True)
                        remaining = max(0, remaining - max(added, 1))
                        continue
                except Exception as retry_exc:
                    last_error = str(retry_exc)
                    # Mark the NEW token (if rotated) as failed so it enters the
                    # error cooldown. Previously this marked the old token which
                    # replace_token had already removed from the pool, so the
                    # failure was silently dropped and the new token stayed
                    # selectable, failing again on the next request.
                    grok_account_service.mark_result(
                        new_token or token, False, error=str(retry_exc)[:300]
                    )
                    continue
            last_error = str(exc)
            grok_account_service.mark_result(token, False, error=str(exc)[:300])
            continue
        except Exception as exc:
            last_error = str(exc)
            grok_account_service.mark_result(token, False, error=str(exc)[:300])
            continue

    if not data_items:
        raise RuntimeError(
            last_error
            or "grok image generation failed for all accounts (Build channel may not expose images)"
        )

    data_items = _persist_urls(data_items, base_url=base_url, response_format=response_format)
    out: dict[str, Any] = {
        "created": int(time.time()),
        "data": data_items,
    }
    if meta_attempts:
        out["_grok_meta"] = {"upstream": "local", "attempts": meta_attempts}
    else:
        out["_grok_meta"] = {"upstream": "local"}
    return out


def handle(body: dict[str, Any]) -> dict[str, Any]:
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")

    force_local = bool(body.get("force_local") or body.get("prefer_local"))
    force_g2a = bool(body.get("force_g2a") or body.get("prefer_g2a"))

    # Prefer remote grokcli2api-go when configured (user keeps pool there).
    if not force_local:
        try:
            remote = _handle_via_g2a(body)
            if remote is not None:
                return remote
        except G2AClientError as exc:
            # If user forced G2A, surface the error; else fall back to local.
            if force_g2a or not grok_account_service.count():
                raise RuntimeError(f"g2a image proxy failed: {exc}") from exc
            # fall through to local
        except Exception as exc:
            if force_g2a or not grok_account_service.count():
                raise
            # fall through to local with note in eventual local meta if needed
            _ = exc

    if force_g2a and not g2a_bridge.has_image_proxy():
        raise RuntimeError("force_g2a set but no G2A image proxy server configured")

    return _handle_via_local_pool(body)


def handle_edit(body: dict[str, Any]) -> dict[str, Any]:
    """Proxy Grok image edits to Codex2API when a remote gateway is configured."""
    import base64

    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    if not g2a_bridge.has_image_proxy():
        raise RuntimeError(
            "Grok 本地池不支持图生图；请在设置里接入 Codex2API / OpenAI 兼容网关后再试"
        )
    raw_images = body.get("images") or []
    images: list[dict[str, str]] = []
    for item in raw_images:
        if not isinstance(item, tuple) or not item:
            continue
        data = item[0]
        mime = str(item[2] if len(item) > 2 else "image/png") or "image/png"
        if not isinstance(data, (bytes, bytearray)) or not data:
            continue
        images.append(
            {"image_url": f"data:{mime};base64,{base64.b64encode(bytes(data)).decode('ascii')}"}
        )
    if not images:
        raise ValueError("image is required")
    model = str(body.get("model") or "gpt-image-2")
    if is_grok_image_model(model):
        model = "gpt-image-2"
    result = g2a_bridge.edit_image(
        prompt=prompt,
        images=images,
        model=model,
        n=max(1, min(int(body.get("n") or 1), 4)),
        size=str(body.get("size") or "") or None,
        response_format=str(body.get("response_format") or "b64_json"),
        server_id=str(body.get("g2a_server_id") or body.get("server_id") or "").strip() or None,
    )
    data_items = _persist_urls(
        list(result.get("data") or []),
        base_url=str(body.get("base_url") or "").strip() or None,
        response_format=str(body.get("response_format") or "b64_json"),
    )
    if not data_items:
        raise RuntimeError("remote image edit returned empty data")
    out: dict[str, Any] = {
        "created": int(result.get("created") or time.time()),
        "data": data_items,
    }
    meta = result.get("_meta")
    if isinstance(meta, dict):
        out["_grok_meta"] = {"upstream": "g2a", **meta}
    return out
