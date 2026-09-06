from __future__ import annotations

from typing import Any

from services.account_service import account_service
from services.grok_account_service import grok_account_service
from utils.grok_models import GROK_IMAGE_MODELS
from utils.helper import CODEX_IMAGE_MODEL


def reset_models_cache() -> None:
    """Kept for tests; public catalog is local image models only."""
    return


def _model_entry(model_id: str, owned_by: str = "chatgpt2api") -> dict[str, Any]:
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": owned_by,
        "permission": [],
        "root": model_id,
        "parent": None,
    }


def list_models() -> dict[str, Any]:
    """Public catalog is image-only. Text models (gpt-5*, grok-4.5, auto) are not exposed."""
    data: list[dict[str, Any]] = []
    seen: set[str] = set()
    dynamic_models: set[str] = set()
    accounts = account_service.list_accounts()
    web_image_accounts = [account for account in accounts if isinstance(account, dict)]
    codex_types = {
        normalized
        for account in accounts
        if isinstance(account, dict)
           and account_service._normalize_source_type(account.get("source_type")) == "codex"
           and (normalized := account_service._normalize_account_type(account.get("type")))
    }

    if web_image_accounts:
        dynamic_models.add("gpt-image-2")
    if codex_types & {"Plus", "Team", "Pro"}:
        dynamic_models.add(CODEX_IMAGE_MODEL)
    if "Plus" in codex_types:
        dynamic_models.add(f"plus-{CODEX_IMAGE_MODEL}")
    if "Team" in codex_types:
        dynamic_models.add(f"team-{CODEX_IMAGE_MODEL}")
    if "Pro" in codex_types:
        dynamic_models.add(f"pro-{CODEX_IMAGE_MODEL}")

    for model in sorted(dynamic_models):
        if model not in seen:
            data.append(_model_entry(model))
            seen.add(model)

    if grok_account_service.count() > 0:
        for model in sorted(GROK_IMAGE_MODELS):
            if model not in seen:
                data.append(_model_entry(model, owned_by="grok"))
                seen.add(model)
    return {"object": "list", "data": data}
