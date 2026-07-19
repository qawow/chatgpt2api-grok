"""Grok/xAI account pool admin API — isolated from /api/accounts."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.support import require_admin
from services.grok_account_service import grok_account_service


class GrokAccountCreateRequest(BaseModel):
    accounts: list[dict[str, Any]] = Field(default_factory=list)


class GrokAccountDeleteRequest(BaseModel):
    tokens: list[str] = Field(default_factory=list)


class GrokAccountRefreshRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)


class GrokAccountUpdateRequest(BaseModel):
    access_token: str = ""
    status: str | None = None
    disabled: bool | None = None
    proxy: str | None = None
    base_url: str | None = None


class GrokAccountImportFilesRequest(BaseModel):
    """Body-embedded cliproxy JSON file contents (no host filesystem scan)."""
    files: list[dict[str, Any]] = Field(default_factory=list)
    # each item: {"name": "a.json", "content": "{...}" | {...}}


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/grok/accounts")
    async def list_grok_accounts(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": grok_account_service.list_accounts(), "provider": "grok"}

    @router.post("/api/grok/accounts")
    async def create_grok_accounts(
        body: GrokAccountCreateRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        accounts = [item for item in body.accounts if isinstance(item, dict)]
        if not accounts:
            raise HTTPException(status_code=400, detail={"error": "accounts is required"})
        result = await run_in_threadpool(grok_account_service.add_account_items, accounts)
        return result

    @router.delete("/api/grok/accounts")
    async def delete_grok_accounts(
        body: GrokAccountDeleteRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        tokens = [str(t or "").strip() for t in body.tokens if str(t or "").strip()]
        if not tokens:
            raise HTTPException(status_code=400, detail={"error": "tokens is required"})
        return grok_account_service.delete_accounts(tokens)

    @router.post("/api/grok/accounts/refresh")
    async def refresh_grok_accounts(
        body: GrokAccountRefreshRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        tokens = [str(t or "").strip() for t in body.access_tokens if str(t or "").strip()]
        result = await run_in_threadpool(
            grok_account_service.refresh_accounts,
            tokens or None,
        )
        return result

    @router.post("/api/grok/accounts/update")
    async def update_grok_account(
        body: GrokAccountUpdateRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        token = str(body.access_token or "").strip()
        if not token:
            raise HTTPException(status_code=400, detail={"error": "access_token is required"})
        updates = {
            key: value
            for key, value in {
                "status": body.status,
                "disabled": body.disabled,
                "proxy": body.proxy,
                "base_url": body.base_url,
            }.items()
            if value is not None
        }
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "no updates provided"})
        item = grok_account_service.update_account(token, updates)
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        return {"item": item, "items": grok_account_service.list_accounts()}

    @router.post("/api/grok/accounts/import-files")
    async def import_grok_files(
        body: GrokAccountImportFilesRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        accounts: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for entry in body.files or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "unknown.json")
            content = entry.get("content")
            try:
                if isinstance(content, str):
                    parsed = json.loads(content)
                elif isinstance(content, dict):
                    parsed = content
                else:
                    raise ValueError("content must be JSON object or string")
                if not isinstance(parsed, dict):
                    raise ValueError("JSON root must be object")
                accounts.append(parsed)
            except Exception as exc:
                errors.append({"name": name, "error": str(exc)[:200]})
        if not accounts:
            raise HTTPException(
                status_code=400,
                detail={"error": "no valid account JSON in files", "errors": errors},
            )
        result = await run_in_threadpool(grok_account_service.add_account_items, accounts)
        return {**result, "parse_errors": errors}

    return router
