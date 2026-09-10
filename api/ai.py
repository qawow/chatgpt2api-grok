from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from api.image_inputs import parse_image_edit_request, read_image_sources
from api.support import require_identity, resolve_image_base_url
from services.content_filter import check_request, request_shape, request_text
from services.log_service import LoggedCall
from services.protocol import (
    grok_v1_image_generations,
    openai_v1_chat_complete,
    openai_v1_image_edit,
    openai_v1_image_generations,
    openai_v1_models,
    openai_v1_response,
)
from utils.grok_models import (
    DEFAULT_GROK_IMAGE_MODEL,
    GROK_IMAGE_MODELS,
    GROK_TEXT_MODELS_DISABLED,
    is_grok_image_model,
    is_grok_text_model,
    resolve_grok_image_model,
)
from utils.helper import WEB_IMAGE_MODEL


class ImageGenerationRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    model: str = WEB_IMAGE_MODEL
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = None
    quality: str = "auto"
    response_format: str = "b64_json"
    history_disabled: bool = True
    stream: bool | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    prompt: str | None = None
    n: int | None = None
    stream: bool | None = None
    modalities: list[str] | None = None
    messages: list[dict[str, object]] | None = None


class ResponseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    input: object | None = None
    tools: list[dict[str, object]] | None = None
    tool_choice: object | None = None
    stream: bool | None = None


async def filter_or_log(call: LoggedCall, text: str) -> None:
    try:
        await run_in_threadpool(check_request, text)
    except HTTPException as exc:
        call.log("调用失败", status="failed", error=str(exc.detail))
        raise


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    async def list_models(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        try:
            return await run_in_threadpool(openai_v1_models.list_models)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

    @router.post("/v1/images/generations")
    async def generate_images(
            body: ImageGenerationRequest,
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        payload["base_url"] = resolve_image_base_url(request)
        # grok-4.5 is chat, not image — never fall through to ChatGPT.
        if is_grok_text_model(body.model):
            raise HTTPException(status_code=400, detail={"error": GROK_TEXT_MODELS_DISABLED})
        # grok-*image* / grok-imagine → Grok pool only.
        if is_grok_image_model(body.model):
            payload["model"] = resolve_grok_image_model(body.model)
            call = LoggedCall(
                identity,
                "/v1/images/generations",
                payload["model"],
                "Grok文生图",
                request_text=body.prompt,
            )
            await filter_or_log(call, body.prompt)
            return await call.run(grok_v1_image_generations.handle, payload)
        call = LoggedCall(identity, "/v1/images/generations", body.model, "文生图", request_text=body.prompt)
        await filter_or_log(call, body.prompt)
        return await call.run(openai_v1_image_generations.handle, payload)

    @router.post("/v1/grok/images/generations")
    async def generate_grok_images(
            body: ImageGenerationRequest,
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        """Force Grok pool (OpenAI Images shape). Default model grok-2-image."""
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        payload["model"] = resolve_grok_image_model(body.model or DEFAULT_GROK_IMAGE_MODEL)
        payload["base_url"] = resolve_image_base_url(request)
        call = LoggedCall(
            identity,
            "/v1/grok/images/generations",
            payload["model"],
            "Grok文生图",
            request_text=body.prompt,
        )
        await filter_or_log(call, body.prompt)
        return await call.run(grok_v1_image_generations.handle, payload)

    @router.post("/v1/grok/chat/completions")
    async def create_grok_chat_completion(
            body: ChatCompletionRequest,
            authorization: str | None = Header(default=None),
    ):
        """Grok text chat is disabled; image models go through /v1/images or image chat."""
        require_identity(authorization)
        raise HTTPException(
            status_code=400,
            detail={
                "error": GROK_TEXT_MODELS_DISABLED,
            },
        )

    @router.get("/v1/grok/models")
    async def list_grok_models(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        from services.grok_account_service import grok_account_service

        has_accounts = grok_account_service.count() > 0
        data = []
        if has_accounts:
            for model in sorted(GROK_IMAGE_MODELS):
                data.append(
                    {
                        "id": model,
                        "object": "model",
                        "created": 0,
                        "owned_by": "grok",
                        "permission": [],
                        "root": model,
                        "parent": None,
                    }
                )
        return {"object": "list", "data": data}

    @router.post("/v1/images/edits")
    async def edit_images(
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload, image_sources, mask_sources = await parse_image_edit_request(request)
        prompt = str(payload["prompt"])
        model = str(payload["model"])
        call = LoggedCall(identity, "/v1/images/edits", model, "图生图", request_text=prompt)
        await filter_or_log(call, prompt)
        if is_grok_image_model(model):
            raise HTTPException(
                status_code=400,
                detail={"error": "Grok 本地池不支持图生图"},
            )
        payload["images"] = await read_image_sources(image_sources)
        if mask_sources:
            payload["mask"] = await read_image_sources(mask_sources)
        payload["base_url"] = resolve_image_base_url(request)
        return await call.run(openai_v1_image_edit.handle, payload)

    @router.post("/v1/chat/completions")
    async def create_chat_completion(body: ChatCompletionRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("prompt"), payload.get("messages"))
        call = LoggedCall(
            identity,
            "/v1/chat/completions",
            model,
            "文本生成",
            request_text=request_preview,
            request_shape=request_shape(payload.get("messages")),
        )
        await filter_or_log(call, request_preview)
        return await call.run(openai_v1_chat_complete.handle, payload)

    @router.post("/v1/responses")
    async def create_response(body: ResponseCreateRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("input"), payload.get("instructions"))
        call = LoggedCall(
            identity,
            "/v1/responses",
            model,
            "Responses",
            request_text=request_preview,
            request_shape=request_shape(payload.get("input")),
        )
        await filter_or_log(call, request_preview)
        return await call.run(openai_v1_response.handle, payload)

    @router.post("/v1/messages")
    async def create_message(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        raise HTTPException(status_code=400, detail={"error": "text models are disabled"})

    return router
