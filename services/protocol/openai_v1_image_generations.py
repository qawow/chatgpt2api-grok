from __future__ import annotations

from typing import Any, Iterator

from services.protocol.conversation import (
    ConversationRequest,
    collect_image_outputs,
    count_text_tokens,
    stream_image_chunks,
    stream_image_outputs_with_pool,
)
from utils.image_tokens import count_image_output_items_tokens, image_usage
from utils.image_models import WEB_IMAGE_MODEL

# Orchestration hooks the task layer injects into the payload. They are callables
# and token sets, never client input — an HTTP entrypoint must strip them before
# handing a request body to handle(), or a caller can pass e.g. a string
# progress_callback (TypeError mid-generation) or steer pool selection through
# _excluded_tokens.
INTERNAL_PAYLOAD_KEYS = (
    "progress_callback",
    "checkpoint_callback",
    "_excluded_tokens",
    "_is_cancelled",
    "_task_control",
)


def strip_internal_keys(body: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``body`` with the orchestration hooks removed."""
    return {key: value for key, value in body.items() if key not in INTERNAL_PAYLOAD_KEYS}


def handle(body: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
    prompt = str(body.get("prompt") or "")
    model = str(body.get("model") or WEB_IMAGE_MODEL)
    n = int(body.get("n") or 1)
    size = body.get("size")
    quality = str(body.get("quality") or "auto")
    response_format = str(body.get("response_format") or "b64_json")
    base_url = str(body.get("base_url") or "") or None
    progress_callback = body.get("progress_callback")
    outputs = stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        n=n,
        size=size,
        quality=quality,
        response_format=response_format,
        base_url=base_url,
        message_as_error=True,
        progress_callback=progress_callback,
        checkpoint_callback=body.get("checkpoint_callback"),
        excluded_tokens=set(body.get("_excluded_tokens") or ()),
        is_cancelled=body.get("_is_cancelled"),
        task_control=body.get("_task_control"),
    ))
    if body.get("stream"):
        return stream_image_chunks(outputs)
    result = collect_image_outputs(outputs)
    result["usage"] = image_usage(
        input_text_tokens=count_text_tokens(prompt, model),
        output_tokens=count_image_output_items_tokens(result.get("data"), size, quality),
    )
    return result
