"""Admin API for grokcli2api-go (Futureppo) server connections."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.support import require_admin
from services.g2a_service import (
    G2AClientError,
    g2a_bridge,
    g2a_config,
    sanitize_g2a_server,
    sanitize_g2a_servers,
)


class G2AServerCreateRequest(BaseModel):
    name: str = ""
    base_url: str = ""
    admin_key: str = ""
    api_key: str = ""
    note: str = ""
    proxy: str = ""
    prefer_for_image: bool = True


class G2AServerUpdateRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    admin_key: str | None = None
    api_key: str | None = None
    note: str | None = None
    enabled: bool | None = None
    proxy: str | None = None
    prefer_for_image: bool | None = None


class G2APushRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)


def _remote_http_status(exc: G2AClientError) -> int:
    """Map remote client errors to our API status.

    Remote 405/502/etc should not be echoed as our route method errors.
    """
    if exc.status == 404 and "server not found" in str(exc).lower():
        return 404
    return 502


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/g2a/servers")
    async def list_servers(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"servers": sanitize_g2a_servers(g2a_config.list_servers())}

    @router.post("/api/g2a/servers")
    async def create_server(body: G2AServerCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            server = g2a_config.add_server(
                name=body.name,
                base_url=body.base_url,
                admin_key=body.admin_key,
                api_key=body.api_key,
                note=body.note,
                proxy=body.proxy,
                prefer_for_image=body.prefer_for_image,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {
            "server": sanitize_g2a_server(server),
            "servers": sanitize_g2a_servers(g2a_config.list_servers()),
        }

    @router.post("/api/g2a/servers/{server_id}")
    async def update_server(
        server_id: str,
        body: G2AServerUpdateRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        updates = body.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "no updates provided"})
        server = g2a_config.update_server(server_id, updates)
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        return {
            "server": sanitize_g2a_server(server),
            "servers": sanitize_g2a_servers(g2a_config.list_servers()),
        }

    @router.delete("/api/g2a/servers/{server_id}")
    async def delete_server(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not g2a_config.delete_server(server_id):
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        return {"servers": sanitize_g2a_servers(g2a_config.list_servers())}

    @router.post("/api/g2a/servers/{server_id}/ping")
    async def ping_server(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            result = await run_in_threadpool(g2a_bridge.ping, server_id)
        except G2AClientError as exc:
            raise HTTPException(status_code=_remote_http_status(exc), detail={"error": str(exc)}) from exc
        return {
            **result,
            "servers": sanitize_g2a_servers(g2a_config.list_servers()),
        }

    @router.get("/api/g2a/servers/{server_id}/credentials")
    async def list_remote_credentials(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            result = await run_in_threadpool(g2a_bridge.list_remote, server_id)
        except G2AClientError as exc:
            raise HTTPException(status_code=_remote_http_status(exc), detail={"error": str(exc)}) from exc
        return {
            "server_id": server_id,
            "items": result.get("items") or [],
            "servers": sanitize_g2a_servers(g2a_config.list_servers()),
        }

    @router.get("/api/g2a/pool")
    async def list_remote_pool(authorization: str | None = Header(default=None), server_id: str = ""):
        """Desensitized remote Grok pool status for 号池管理.

        Rows use synthetic access_token ``g2a:{server_id}:{credential_id}``.
        Tokens are never available from remote admin list.
        """
        require_admin(authorization)
        sid = (server_id or "").strip() or None
        try:
            result = await run_in_threadpool(g2a_bridge.list_remote_pool_status, sid)
        except G2AClientError as exc:
            raise HTTPException(status_code=_remote_http_status(exc), detail={"error": str(exc)}) from exc
        return {
            **result,
            "config_servers": sanitize_g2a_servers(g2a_config.list_servers()),
            "has_image_proxy": g2a_bridge.has_image_proxy(),
        }

    @router.post("/api/g2a/servers/{server_id}/push")
    async def push_local_to_remote(
        server_id: str,
        body: G2APushRequest,
        authorization: str | None = Header(default=None),
    ):
        """Push local Grok pool accounts into remote grokcli2api-go."""
        require_admin(authorization)
        tokens = [str(t or "").strip() for t in body.access_tokens if str(t or "").strip()]
        try:
            result = await run_in_threadpool(
                g2a_bridge.push_local_accounts,
                server_id,
                access_tokens=tokens or None,
            )
        except G2AClientError as exc:
            raise HTTPException(status_code=_remote_http_status(exc), detail={"error": str(exc)}) from exc
        return {
            **result,
            "servers": sanitize_g2a_servers(g2a_config.list_servers()),
        }

    @router.delete("/api/g2a/servers/{server_id}/credentials/{credential_id}")
    async def delete_remote_credential(
        server_id: str,
        credential_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            result = await run_in_threadpool(g2a_bridge.delete_remote, server_id, credential_id)
        except G2AClientError as exc:
            raise HTTPException(status_code=_remote_http_status(exc), detail={"error": str(exc)}) from exc
        return result

    return router
