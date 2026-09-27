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

    def _generate(acct: dict[str, Any], want: int) -> int:
        result = generate_image(
            acct,
            prompt=prompt,
            model=model,
            n=want,
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
        return added

    while remaining > 0:
        account = grok_account_service.get_next_account(exclude_tokens=exclude)
        if account is None:
            if data_items:
                break
            raise RuntimeError(last_error or "no available grok accounts in pool")
        token = str(account.get("access_token") or "")
        exclude.add(token)
        try:
            added = _generate(account, remaining)
            grok_account_service.mark_result(token, True)
            remaining = max(0, remaining - max(added, 1))
            continue
        except GrokBackendError as exc:
            last_error = str(exc)
            status = getattr(exc, "status", None)
            if status == 422:
                # The account is fine — the model declined to draw this prompt.
                # Recording it as a failure put the account in error cooldown,
                # and moving on made every other account decline the same prompt
                # too, cooling down the whole pool. Hand the reason back instead.
                if data_items:
                    break
                raise RuntimeError(last_error) from exc
            if status not in {401, 403}:
                grok_account_service.mark_result(token, False, error=last_error[:300], status=status)
                continue
            failure: Exception = exc
        except Exception as exc:
            last_error = str(exc)
            grok_account_service.mark_result(token, False, error=last_error[:300])
            continue

        # 401/403: refresh once and retry — but only when the token actually
        # rotated. ensure_fresh_account returns the *same* account when there is
        # no refresh_token or the refresh itself was rejected, and retrying that
        # just burns another full upstream timeout on a known-dead token.
        retry_token = ""
        try:
            refreshed = grok_account_service.ensure_fresh_account(account, force=True)
            retry_token = str((refreshed or {}).get("access_token") or "")
            if refreshed and retry_token and retry_token != token:
                exclude.add(retry_token)
                added = _generate(refreshed, remaining)
                meta_attempts[-1] = {**meta_attempts[-1], "retried_after_refresh": True}
                grok_account_service.mark_result(retry_token, True)
                remaining = max(0, remaining - max(added, 1))
                continue
        except GrokBackendError as retry_exc:
            failure = retry_exc
        except Exception as retry_exc:
            failure = retry_exc
        last_error = str(failure)
        grok_account_service.mark_result(
            retry_token or token,
            False,
            error=last_error[:300],
            status=getattr(failure, "status", None),
        )

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
