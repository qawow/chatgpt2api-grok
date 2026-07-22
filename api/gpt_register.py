"""Admin API: batch GPT free registration settings + jobs."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.support import require_admin
from services.gpt_register_service import (
    gpt_register_config,
    gpt_register_service,
    public_settings,
)


class GptRegisterSettingsUpdate(BaseModel):
    engines_dir: str | None = None
    run_mode: str | None = None
    python_bin: str | None = None
    count: int | None = Field(default=None, ge=1, le=50)
    concurrency: int | None = Field(default=None, ge=1, le=5)
    interval_secs: float | None = Field(default=None, ge=0, le=600)
    timeout_secs: int | None = Field(default=None, ge=60, le=3600)
    executor: str | None = None
    mail_provider: str | None = None
    captcha: str | None = None
    proxy: str | None = None
    bind_register_proxy: bool | None = None
    plan_type: str | None = None
    source_type: str | None = None
    cfd1_domain: str | None = None
    push_enabled: bool | None = None
    push_mode: str | None = None
    chatgpt2api_base_url: str | None = None
    chatgpt2api_auth_key: str | None = None
    dry_run: bool | None = None
    # latency knobs — must be declared or FastAPI/Pydantic silently drops them
    # (frontend was sending skip_codex:false but it never reached config store)
    skip_codex: bool | None = None
    register_no_delay: bool | None = None
    so_collect_ms: str | None = None


class GptRegisterStartRequest(GptRegisterSettingsUpdate):
    """Optional per-run overrides; omitted fields use saved settings."""

    pass


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/gpt-register/settings")
    async def get_settings(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"settings": public_settings(gpt_register_config.get())}

    @router.post("/api/gpt-register/settings")
    async def save_settings(
        body: GptRegisterSettingsUpdate,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        patch = body.model_dump(exclude_none=True)
        if not patch:
            raise HTTPException(status_code=400, detail={"error": "no settings provided"})
        settings = gpt_register_config.update(patch)
        return {"settings": public_settings(settings)}

    @router.get("/api/gpt-register/jobs")
    async def list_jobs(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"jobs": gpt_register_service.list_jobs()}

    @router.get("/api/gpt-register/jobs/{job_id}")
    async def get_job(job_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        job = gpt_register_service.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail={"error": "job not found"})
        return {"job": job}

    @router.post("/api/gpt-register/start")
    async def start_job(
        body: GptRegisterStartRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        overrides = body.model_dump(exclude_none=True)
        try:
            job = await run_in_threadpool(
                gpt_register_service.start_job,
                overrides or None,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"job": job}

    @router.post("/api/gpt-register/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        job = gpt_register_service.cancel_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail={"error": "job not found"})
        return {"job": job}

    return router
