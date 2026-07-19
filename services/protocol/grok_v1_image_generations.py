"""OpenAI-compatible image generations backed by the Grok pool only."""
from __future__ import annotations

import base64
import time
from typing import Any

from services.grok_account_service import grok_account_service
from services.grok_backend_api import GrokBackendError, b64_to_bytes, generate_image
from utils.grok_models import resolve_grok_image_model


def handle(body: dict[str, Any]) -> dict[str, Any]:
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    model = resolve_grok_image_model(body.get("model"))
    n = max(1, min(int(body.get("n") or 1), 4))
    size = body.get("size")
    response_format = str(body.get("response_format") or "b64_json").strip() or "b64_json"
    base_url = str(body.get("base_url") or "").strip() or None

    # Lazy import: image_storage pulls curl_cffi / full app stack
    try:
        from services.image_storage_service import image_storage_service
    except Exception:
        image_storage_service = None  # type: ignore[assignment]

    data_items: list[dict[str, Any]] = []
    meta_attempts: list[Any] = []
    exclude: set[str] = set()
    last_error: str | None = None

    for _ in range(n):
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
                n=1,
                size=str(size) if size else None,
                response_format=response_format,
            )
            meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
            meta_attempts.append(meta)
            for item in result.get("data") or []:
                if not isinstance(item, dict):
                    continue
                entry = dict(item)
                # Persist bytes when we have b64 so clients can also get url
                b64 = entry.get("b64_json")
                if b64 and image_storage_service is not None:
                    try:
                        stored = image_storage_service.save(b64_to_bytes(str(b64)), base_url=base_url)
                        entry["url"] = stored.url
                        if response_format == "url":
                            entry.pop("b64_json", None)
                    except Exception:
                        # keep b64 even if storage fails
                        pass
                data_items.append(entry)
            grok_account_service.mark_result(token, True)
        except GrokBackendError as exc:
            last_error = str(exc)
            grok_account_service.mark_result(token, False, error=str(exc)[:300])
            # auth/hard failures: try next account; 502 image-unsupported will repeat — still try 1-2 accounts
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

    out: dict[str, Any] = {
        "created": int(time.time()),
        "data": data_items,
    }
    if meta_attempts:
        out["_grok_meta"] = {"attempts": meta_attempts}
    return out
