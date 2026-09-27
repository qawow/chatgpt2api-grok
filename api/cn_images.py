"""Unofficial Doubao + 360智图 image APIs."""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from api.support import filter_or_log, require_identity, resolve_image_base_url
from services.doubao_backend import DoubaoError, generate as doubao_generate, probe_login as doubao_probe
from services.log_service import LoggedCall
from services.protocol.cn_image_generations import openai_image_items
from utils.image_models import DEFAULT_ZHITU360_MODEL, DOUBAO_IMAGE_MODEL, is_doubao_model  # noqa: F401 (re-export)
from services.zhitu360_backend import (
    MODELS,
    RATIOS,
    Zhitu360Error,
    fetch_config,
    generate as zhitu_generate,
    probe as zhitu_probe,
    query_task,
    size_to_ratio,
)


class DoubaoRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    prompt: str | None = None
    input: str | None = None
    cookies: str | None = None
    captcha: str | None = None
    captcha_token: str | None = None
    a_bogus: str | None = None
    response_format: str = "url"

    @property
    def text(self) -> str:
        return str(self.prompt or self.input or "").strip()


class ZhituRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    prompt: str | None = None
    input: str | None = None
    model: str = DEFAULT_ZHITU360_MODEL
    ratio: str | None = None
    size: str | None = None
    style: str = "auto"
    n: int | None = Field(default=None, ge=1, le=4)
    photoNums: int | None = Field(default=None, ge=1, le=4)  # noqa: N815 - upstream field name
    wait: bool = True
    cookies: str | None = None
    captcha: str | None = None
    captcha_token: str | None = None

    @property
    def text(self) -> str:
        return str(self.prompt or self.input or "").strip()

    @property
    def count(self) -> int:
        return int(self.n or self.photoNums or 1)


class ZhituQueryRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    record_id: str | None = None
    id: str | None = None
    cookies: str | None = None

    @property
    def rid(self) -> str:
        return str(self.record_id or self.id or "").strip()


def _openai_images(images: list[Any], response_format: str = "url", base_url: str | None = None) -> dict[str, Any]:
    return {"created": int(time.time()), "data": openai_image_items(images, response_format, base_url)}


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/v1/doubao/status")
    async def doubao_status(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        try:
            return await run_in_threadpool(doubao_probe)
        except DoubaoError:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

    @router.get("/v1/doubao")
    async def doubao_help(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        return {
            "endpoint": "POST /v1/doubao",
            "alias": "POST /v1/images/generations  (model=doubao*)",
            "upstream": "https://www.doubao.com/chat/completion",
            "skill": "ImageGeneration=4",
            "auth": "doubao.cookies (www.doubao.com login). Optional captcha / a_bogus / solve_url.",
            "body": {
                "prompt": "required",
                "cookies": "optional Cookie header override",
                "captcha": "optional external captcha token",
                "a_bogus": "optional webmssdk signature",
                "response_format": "url | b64_json",
            },
        }

    @router.post("/v1/doubao")
    async def doubao_generate_api(
        body: DoubaoRequest,
        request: Request,
        authorization: str | None = Header(default=None),
        cookie: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        prompt = body.text
        call = LoggedCall(identity, "/v1/doubao", DOUBAO_IMAGE_MODEL, "豆包文生图", request_text=prompt)
        await filter_or_log(call, prompt)
        try:
            result = await run_in_threadpool(
                doubao_generate,
                prompt,
                cookies=str(body.cookies or cookie or ""),
                captcha=str(body.captcha or body.captcha_token or ""),
                a_bogus=str(body.a_bogus or ""),
            )
        except DoubaoError as exc:
            call.log("调用失败", status="failed", error=str(exc.detail))
            raise
        except Exception as exc:
            call.log("调用失败", status="failed", error=str(exc))
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc
        fmt = str(body.response_format or "url").strip().lower()
        if fmt == "b64_json" and not any(img.b64_json for img in result.images):
            raise HTTPException(status_code=502, detail={"error": "upstream returned URLs only"})
        payload = _openai_images(result.images, fmt, resolve_image_base_url(request))
        payload["conversation_id"] = result.conversation_id
        payload["message_id"] = result.message_id
        # Pass the data list so the call log records image URLs (the log page
        # renders detail.urls; a bare count showed nothing).
        call.log("调用完成", {"data": payload["data"], "conversation_id": result.conversation_id})
        return payload

    @router.get("/v1/zhitu360/status")
    async def zhitu_status(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        try:
            return await run_in_threadpool(zhitu_probe)
        except Zhitu360Error:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

    @router.get("/v1/zhitu360/models")
    async def zhitu_models(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        try:
            data = await run_in_threadpool(fetch_config)
        except Zhitu360Error:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc
        return data

    @router.get("/v1/zhitu360")
    async def zhitu_help(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        return {
            "endpoint": "POST /v1/zhitu360",
            "alias": "POST /v1/images/generations  (model=zhitu360|jimeng*|hunyuan|tongyi|wanx21plus)",
            "upstream": "https://image.360.com/api/v1/zhitu/text/to/image/create",
            "query": "POST /v1/zhitu360/query  {record_id}",
            "auth": "zhitu360.cookies (QHPass). Paid beans/membership for create (errno 20603).",
            "models": list(MODELS),
            "ratios": list(RATIOS),
            "body": {
                "prompt": "required",
                "model": "jimeng | jimeng40 | jimeng45 | hunyuan | tongyi | wanx21plus",
                "ratio": "1:1 (default) | 9:16 | 16:9 | ...",
                "style": "auto",
                "n": "1-4 photoNums",
                "wait": "true to poll query (default)",
                "cookies": "optional override",
                "captcha": "optional external captcha token",
            },
        }

    @router.post("/v1/zhitu360/query")
    async def zhitu_query_api(
        body: ZhituQueryRequest,
        authorization: str | None = Header(default=None),
        cookie: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        record_id = body.rid
        call = LoggedCall(identity, "/v1/zhitu360/query", "zhitu360", "360智图查询", request_text=record_id)
        try:
            result = await run_in_threadpool(
                query_task,
                record_id,
                cookies=str(body.cookies or cookie or ""),
            )
        except Zhitu360Error as exc:
            call.log("调用失败", status="failed", error=str(exc.detail))
            raise
        except Exception as exc:
            call.log("调用失败", status="failed", error=str(exc))
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc
        call.log("调用完成", {"record_id": result.record_id, "status": result.status})
        return {
            "record_id": result.record_id,
            "status": result.status,
            "status_name": result.status_name,
            "data": _openai_images(result.images)["data"],
        }

    @router.post("/v1/zhitu360")
    async def zhitu_generate_api(
        body: ZhituRequest,
        authorization: str | None = Header(default=None),
        cookie: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        prompt = body.text
        model = str(body.model or DEFAULT_ZHITU360_MODEL)
        call = LoggedCall(identity, "/v1/zhitu360", model, "360智图文生图", request_text=prompt)
        await filter_or_log(call, prompt)
        try:
            result = await run_in_threadpool(
                zhitu_generate,
                prompt,
                model=model,
                ratio=size_to_ratio(body.ratio or body.size or "1:1"),
                style=str(body.style or "auto"),
                n=body.count,
                cookies=str(body.cookies or cookie or ""),
                captcha=str(body.captcha or body.captcha_token or ""),
                wait=bool(body.wait),
            )
        except Zhitu360Error as exc:
            call.log("调用失败", status="failed", error=str(exc.detail))
            raise
        except Exception as exc:
            call.log("调用失败", status="failed", error=str(exc))
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc
        payload = _openai_images(result.images)
        payload["record_id"] = result.record_id
        payload["status"] = result.status
        payload["status_name"] = result.status_name
        call.log("调用完成", {"record_id": result.record_id, "status": result.status, "images": len(result.images)})
        return payload

    return router
