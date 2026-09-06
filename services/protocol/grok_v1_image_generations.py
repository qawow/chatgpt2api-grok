"""OpenAI-compatible image generations for Grok (local pool only).

Never falls through to the ChatGPT account pool.
"""
from __future__ import annotations

import time
from typing import Any

from services.grok_account_service import grok_account_service
from services.grok_backend_api import GrokBackendError, b64_to_bytes, generate_image
from utils.grok_models import GROK_TEXT_MODELS_DISABLED, is_grok_text_model, resolve_grok_image_model


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
    if is_grok_text_model(body.get("model")):
        raise ValueError(GROK_TEXT_MODELS_DISABLED)
    payload = dict(body)
    payload["model"] = resolve_grok_image_model(body.get("model"))
    return _handle_via_local_pool(payload)


def handle_edit(body: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("Grok 本地池不支持图生图")
