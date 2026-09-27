from __future__ import annotations

from typing import Any

from services.account_service import account_service
from services.config import config
from services.grok_account_service import grok_account_service
from utils.image_models import (
    CODEX_IMAGE_MODEL,
    DOUBAO_IMAGE_MODEL,
    GROK_CANONICAL_IMAGE_MODELS,
    OWNED_BY,
    PROVIDER_CHATGPT,
    PROVIDER_DOUBAO,
    PROVIDER_GROK,
    PROVIDER_ZHITU360,
    WEB_IMAGE_MODEL,
    ZHITU360_IMAGE_MODELS,
)

_CODEX_PLANS = {"Plus": "plus", "Team": "team", "Pro": "pro"}


def reset_models_cache() -> None:
    """Kept for tests; public catalog is local image models only."""
    return


def _model_entry(model_id: str, provider: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": OWNED_BY[provider],
        "permission": [],
        "root": model_id,
        "parent": None,
    }


def chatgpt_image_models() -> list[str]:
    accounts = [account for account in account_service.list_accounts() if isinstance(account, dict)]
    if not accounts:
        return []
    models = [WEB_IMAGE_MODEL]
    codex_types = {
        normalized
        for account in accounts
        if account_service._normalize_source_type(account.get("source_type")) == "codex"
        and (normalized := account_service._normalize_account_type(account.get("type")))
    }
    paid_plans = [plan for plan in _CODEX_PLANS if plan in codex_types]
    if paid_plans:
        models.append(CODEX_IMAGE_MODEL)
        models.extend(f"{_CODEX_PLANS[plan]}-{CODEX_IMAGE_MODEL}" for plan in paid_plans)
    return models


def grok_image_models() -> list[str]:
    return list(GROK_CANONICAL_IMAGE_MODELS) if grok_account_service.count() > 0 else []


def _has_cookies(settings: dict[str, Any]) -> bool:
    return bool(str(settings.get("cookies") or "").strip())


def doubao_image_models() -> list[str]:
    return [DOUBAO_IMAGE_MODEL] if _has_cookies(config.get_doubao_settings()) else []


def zhitu360_image_models() -> list[str]:
    return list(ZHITU360_IMAGE_MODELS) if _has_cookies(config.get_zhitu360_settings()) else []


def list_models() -> dict[str, Any]:
    """Image-generation models only, canonical ids only.

    Text/chat models (gpt-5*, grok-4.5, auto) are never listed, and neither are
    aliases or legacy names (gpt-image-2, grok-imagine, grok-2-image-1212,
    即梦…) — those are still accepted on requests. A provider's models appear
    only when it can serve them: a non-empty pool, or configured cookies for the
    cookie-based backends (which also accept per-request cookies, so an unlisted
    id can still work when the client supplies them).
    """
    data: list[dict[str, Any]] = []
    seen: set[str] = set()
    for provider, models in (
        (PROVIDER_CHATGPT, chatgpt_image_models()),
        (PROVIDER_GROK, grok_image_models()),
        (PROVIDER_DOUBAO, doubao_image_models()),
        (PROVIDER_ZHITU360, zhitu360_image_models()),
    ):
        for model in models:
            if model not in seen:
                seen.add(model)
                data.append(_model_entry(model, provider))
    return {"object": "list", "data": data}


def list_grok_models() -> dict[str, Any]:
    return {"object": "list", "data": [_model_entry(model, PROVIDER_GROK) for model in grok_image_models()]}
