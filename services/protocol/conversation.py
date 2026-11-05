from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, NamedTuple

from curl_cffi import requests

from services.account_service import AccountService, account_service
from services.image_task_control import ImageTaskControl
from services.config import config
from services.image_storage_service import image_storage_service
from services.openai_backend_api import (
    ArkoseRequiredError,
    ImageContentPolicyError,
    ImagePollTimeoutError,
    OpenAIBackendAPI,
    is_resumable_image_stream_error,
    ToolCooldownError,
    ToolQuotaError,
)
from services.proxy_service import proxy_settings
from utils.helper import (
    IMAGE_MODELS,
    SseStreamTimeoutError,
    SseProtocolError,
    UpstreamHTTPError,
    extract_image_from_message_content,
    is_codex_image_model,
    is_supported_image_model,
    split_image_model,
)
from utils.image_tokens import count_image_content_tokens
from utils.log import logger
from utils.tiktoken_encoding import encoding_for_model


class ImageGenerationError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = 502,
        error_type: str = "server_error",
        code: str | None = "upstream_error",
        param: str | None = None,
        account_email: str = "",
        conversation_id: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        self.param = param
        self.account_email = account_email
        self.conversation_id = conversation_id
        self.headers = headers

    def to_openai_error(self) -> dict[str, Any]:
        error_dict = {
            "error": {
                "message": public_image_error_message(str(self)),
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }
        if self.account_email:
            error_dict["error"]["account_email"] = self.account_email
        return error_dict


def _accepted_image_conversation_id(error: object, backend: OpenAIBackendAPI | None) -> str:
    for candidate in (
        getattr(error, "conversation_id", None), getattr(backend, "_image_conversation_id", None),
    ):
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""


def public_image_error_message(message: str) -> str:
    text = str(message or "").strip()
    lower = text.lower()
    if any(item in lower for item in ("backend-api/", "status=", "body=", "chatgpt.com", "upstreamhttperror")):
        return "The image generation request failed. Please try again later."
    return text or "The image generation request failed. Please try again later."


def is_token_invalid_error(message: str) -> bool:
    text = str(message or "").lower()
    return (
        "token_invalidated" in text
        or "token invalidated" in text
        or "token_revoked" in text
        or "authentication token has been invalidated" in text
        or "could not parse your authentication token" in text
        or "unauthorized_unknown" in text
        or "invalid_access_token" in text
        or "invalidated oauth token" in text
        or "status=401" in text
        or "upstream session expired" in text
    )


def is_model_not_found_error(error: object) -> bool:
    """Detect upstream's ``404 {"detail":"Model not found"}`` on the conversation surface.

    Error-surface mapping proved this is not a bad-slug verdict: ``model=""``,
    ``model="   "`` and ``model="zzz-not-a-model"`` all return the identical
    body, and so does every slug the account's own ``/backend-api/models``
    catalog advertises (35 slugs swept). Both ``/backend-api/f/conversation``
    and the legacy ``/backend-api/conversation`` answer it the same way, while
    ``/f/conversation/prepare`` succeeds and ``/f/conversation/resume`` returns
    a normal Pydantic validation error.

    It is also **not** an account-level terminal state. The anonymous surfaces
    ``/backend-anon/conversation`` and ``/backend-anon/f/conversation`` return
    the same 404 with no credential at all, and a freshly minted device id does
    not change it. The condition therefore lives on the ``-convo`` backend
    (``x-build: 814069032b2f-convo``), not on the credential — so callers must
    not treat a single hit as proof that this account is spent.

    Body validation runs *before* model resolution, which is what makes the
    error readable: an absent ``conversation_origin`` reaches model resolution
    and fails there, whereas ``tpp``/``flora`` reach it and succeed (they answer
    ``conversation_model_origin_mismatch`` instead). The valid origin enum, read
    straight off the server's own Pydantic error on ``/conversation/init``, is
    ``apple, apple_xcode, apple_image_playground, apple_shortcuts, kakao_talk,
    fyp_feedback, pulse, finserv_app, bigbrain, flora, tpp,
    workspace_agent_in_slack, viber, whatsapp, telegram`` — plain web chat is
    the absent value, so ``chat``/``codex``/``other``/``work`` are all invalid.

    Deliberately does not match the sibling 404 whose body is
    ``{"detail":{"message":"Conversation not found",...}}``: that one is a
    per-conversation miss and is retryable with a different conversation id.
    """
    status = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    if status == 404 and isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str) and "model not found" in detail.lower():
            return True
    text = str(error or "").lower()
    return "model not found" in text and "status=404" in text


# ── Global "Model not found" circuit breaker ─────────────────────────────────
# The 404 above is a backend-wide condition, not a per-credential one (see the
# docstring). Parking every credential that touches it for the full quota
# cooldown would drain the whole pool on a fault that has nothing to do with
# the accounts, so the breaker separates the two cases:
#   * a single credential hitting it  -> genuine per-account miss, park long;
#   * several distinct credentials hitting it inside the window -> global fault,
#     park short and trip the breaker so later callers fail fast instead of
#     walking the pool one 3.6-4.9s upstream round-trip at a time.
_MODEL_NOT_FOUND_LOCK = threading.Lock()
_MODEL_NOT_FOUND_STATE: dict[str, Any] = {"tokens": {}, "tripped_until": 0.0}
MODEL_NOT_FOUND_WINDOW_SECS = 300.0
MODEL_NOT_FOUND_GLOBAL_MIN_ACCOUNTS = 2
MODEL_NOT_FOUND_BREAKER_SECS = 60.0


class ModelNotFoundVerdict(NamedTuple):
    """Outcome of recording one ``Model not found`` hit.

    ``window_tokens`` lists every credential seen inside the window, so the
    caller can shorten parks that were already applied while the condition
    still looked isolated.
    """

    is_global: bool
    park_secs: float
    window_tokens: tuple[str, ...]


def note_model_not_found(token: str) -> ModelNotFoundVerdict:
    """Record one ``Model not found`` hit and decide how to treat it.

    ``is_global`` turns true once ``MODEL_NOT_FOUND_GLOBAL_MIN_ACCOUNTS``
    distinct credentials have hit the condition inside
    ``MODEL_NOT_FOUND_WINDOW_SECS``. Global hits park for
    ``MODEL_NOT_FOUND_BREAKER_SECS`` (so the credential is retried soon after
    upstream recovers) instead of the quota cooldown; isolated hits keep the
    long park because they really do look like a spent credential.
    """
    now = time.time()
    token = str(token or "")
    with _MODEL_NOT_FOUND_LOCK:
        tokens = {
            key: ts
            for key, ts in _MODEL_NOT_FOUND_STATE["tokens"].items()
            if now - float(ts) <= MODEL_NOT_FOUND_WINDOW_SECS
        }
        if token:
            tokens[token] = now
        _MODEL_NOT_FOUND_STATE["tokens"] = tokens
        is_global = len(tokens) >= MODEL_NOT_FOUND_GLOBAL_MIN_ACCOUNTS
        if is_global:
            _MODEL_NOT_FOUND_STATE["tripped_until"] = now + MODEL_NOT_FOUND_BREAKER_SECS
        park_secs = (
            MODEL_NOT_FOUND_BREAKER_SECS if is_global
            else float(config.image_tool_quota_cooldown_secs)
        )
        return ModelNotFoundVerdict(is_global, park_secs, tuple(tokens))


def model_not_found_breaker_remaining() -> float:
    """Seconds left on the global breaker (0.0 when it is not tripped)."""
    with _MODEL_NOT_FOUND_LOCK:
        return max(0.0, float(_MODEL_NOT_FOUND_STATE["tripped_until"]) - time.time())


def reset_model_not_found_breaker() -> None:
    """Clear the breaker and its window (used by tests and manual recovery)."""
    with _MODEL_NOT_FOUND_LOCK:
        _MODEL_NOT_FOUND_STATE["tokens"] = {}
        _MODEL_NOT_FOUND_STATE["tripped_until"] = 0.0


def is_soft_prepare_auth_error(message: str) -> bool:
    """chat-requirements 401 after TLS retry is often a new device-id, not a spent quota."""
    text = str(message or "").lower()
    return "chat_requirements_prepare" in text


def is_tls_connection_error(message: str) -> bool:
    """检测 TLS/SSL 连接错误，这类错误通常可以通过重试解决。"""
    text = str(message or "").lower()
    return (
        "curl: (35)" in text
        or "tls connect error" in text
        or "openssl_internal" in text
        or "ssl: wrong_version_number" in text
        or "ssl: certificate_verify_failed" in text
        or "connection aborted" in text
        or "remote disconnected" in text
        or "connection reset by peer" in text
    )


def is_connection_timeout_error(message: str) -> bool:
    """检测连接超时错误（如 curl 28），这类错误可通过同账号短等待重试解决。"""
    text = str(message or "").lower()
    return (
        "curl: (28)" in text
        or "operation timed out" in text
        or "connection timed out" in text
        or "read timed out" in text
        or "connect timeout" in text
        # Local hard caps on hung SSE via proxy half-open sockets.
        or "sse stream idle timeout" in text
        or "sse stream exceeded total timeout" in text
        or "upstream connection timed out" in text
    )


def is_openssl_invalid_library_error(message: str) -> bool:
    """curl_cffi BoringSSL/HTTP2 glitch. New session on the same SOCKS usually works."""
    text = str(message or "").lower()
    return "openssl_internal" in text and "invalid library" in text


def is_proxy_unreachable_error(message: str) -> bool:
    """代理本身不可达（拒连/隧道失败），再打同一条出口没有意义。"""
    text = str(message or "").lower()
    return (
        "proxyerror" in text
        or "proxy error" in text
        or "unable to connect to proxy" in text
        or "could not connect to proxy" in text
        or "failed to connect to proxy" in text
        or "tunnel connection failed" in text
        or "upstream socks failed" in text
        or "connection refused" in text
        or "name or service not known" in text
    )


def is_upstream_connection_error(message: str) -> bool:
    """识别代理、连接建立和 TLS 失败，统一进入上游连接重试/换号路径。"""
    text = str(message or "").lower()
    return (
        is_tls_connection_error(text)
        or is_connection_timeout_error(text)
        or is_proxy_unreachable_error(text)
        or "curl: (7)" in text
        or "curl: (35)" in text
        or "curl: (56)" in text
        or "proxy connect" in text
        or "failed to connect" in text
        or "could not connect" in text
        or "max retries exceeded" in text
        or "upstream image connection failed" in text
    )


def _call_image_callback(callback: Any, payload: Any) -> Any:
    try:
        return callback(payload)
    except Exception as exc:
        try:
            exc._image_callback_error = True
        except Exception:
            pass
        raise


def _is_retryable_image_read_error(error: object) -> bool:
    if getattr(error, "_image_callback_error", False):
        return False
    if isinstance(error, (SseProtocolError, ImageContentPolicyError, ArkoseRequiredError, ToolCooldownError, ToolQuotaError)):
        return False
    if isinstance(error, TimeoutError) and not isinstance(error, SseStreamTimeoutError):
        return False
    if isinstance(error, UpstreamHTTPError):
        return error.status_code >= 500 or error.status_code in {408, 409, 425}
    if isinstance(error, ImagePollTimeoutError):
        return True
    if isinstance(error, (SseStreamTimeoutError, requests.exceptions.RequestException)):
        return is_resumable_image_stream_error(error)
    return isinstance(error, RuntimeError) and is_upstream_connection_error(str(error))


def image_stream_error_message(message: str) -> str:
    text = str(message or "")
    if is_token_invalid_error(text):
        return "upstream session expired, please retry"
    if is_connection_timeout_error(text):
        return "upstream connection timed out, please retry later"
    if is_upstream_connection_error(text):
        return "upstream image connection failed, please retry later"
    return text or "image generation failed"


REFERENCED_IMAGE_IDS_RE = re.compile(r'"referenced_image_ids"\s*:\s*\[([^\]]+)\]')
# 检测模型返回的部分工具调用 JSON（如 {"size":"1920x1088","n":1}）
# 这些 JSON 包含图片生成工具的参数，但没有实际生成图片
TOOL_PARAMS_JSON_RE = re.compile(
    r'\{\s*"size"\s*:\s*"\d+x\d+"\s*,\s*"n"\s*:\s*\d+\s*\}'
)


def is_model_text_reply_instead_of_image(message: str) -> bool:
    """检测模型是否返回了文本回复（包含工具调用 JSON）而非实际生成图片。

    当上游 ChatGPT 未能触发图片生成工具时，会返回一段描述性文本，
    其中可能包含 JSON 参数（如 prompt、referenced_image_ids、size/n 等）。
    这种情况应被视为「上游未生成图片」而非「内容策略违规」。

    检测两种模式：
    1. 完整的工具调用 JSON（含 referenced_image_ids）
    2. 部分的工具参数 JSON（如 {"size":"1920x1088","n":1}）
    """
    if not message:
        return False
    if REFERENCED_IMAGE_IDS_RE.search(message):
        return True
    # 检测部分工具参数 JSON（模型返回了工具参数但未触发工具）
    if TOOL_PARAMS_JSON_RE.search(message):
        return True
    return False


_CLARIFYING_FOLLOWUP_HINTS = (
    "你更喜欢",
    "哪一种",
    "哪种风格",
    "选哪个",
    "which do you prefer",
    "would you prefer",
)


def is_clarifying_image_followup(message: str) -> bool:
    """Model asked a follow-up instead of treating the image as the final result."""
    text = str(message or "").strip()
    if not text or is_model_text_reply_instead_of_image(text):
        return False
    lower = text.lower()
    if "content policy" in lower or "内容政策" in text or "防护限制" in text:
        return False
    if "?" in text or "？" in text:
        return True
    return any(hint in text or hint in lower for hint in _CLARIFYING_FOLLOWUP_HINTS)


def encode_images(images: Iterable[tuple[bytes, str, str]]) -> list[str]:
    return [base64.b64encode(data).decode("ascii") for data, _, _ in images if data]


def save_image_bytes(image_data: bytes, base_url: str | None = None) -> str:
    return image_storage_service.save(image_data, base_url).url


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and str(item.get("type") or "") in {"text", "input_text", "output_text"}:
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return ""


def normalize_messages(messages: object, system: Any = None) -> list[dict[str, Any]]:
    normalized = []
    if config.global_system_prompt:
        normalized.append({"role": "system", "content": config.global_system_prompt})
    system_text = message_text(system)
    if system_text:
        normalized.append({"role": "system", "content": system_text})
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role", "user")
            content = message.get("content", "")
            text = message_text(content)
            images: list[tuple[bytes, str]] = []
            if role == "user":
                images.extend(extract_image_from_message_content(content))
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict) or part.get("type") != "image":
                            continue
                        data = part.get("data")
                        if isinstance(data, (bytes, bytearray)) and all(existing[0] != bytes(data) for existing in images):
                            images.append((bytes(data), str(part.get("mime") or "image/png")))
            if images:
                parts: list[Any] = []
                if text:
                    parts.append({"type": "text", "text": text})
                for data, mime in images:
                    parts.append({"type": "image", "data": data, "mime": mime})
                normalized.append({"role": role, "content": parts})
            else:
                normalized.append({"role": role, "content": text})
    return normalized


def prompt_with_global_system(prompt: str) -> str:
    return f"{config.global_system_prompt}\n\n{prompt}" if config.global_system_prompt else prompt


def assistant_history_text(messages: list[dict[str, Any]]) -> str:
    return "".join(str(item.get("content") or "") for item in messages if item.get("role") == "assistant")


def assistant_history_messages(messages: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("content") or "") for item in messages if item.get("role") == "assistant" and item.get("content")]


def build_image_prompt(prompt: str, size: str | None, quality: str = "auto") -> str:
    hints = []
    if size:
        hints.append(f"输出图片尺寸为 {size}。")
    if quality:
        hints.append(f"输出图片质量为 {quality}。")
    return f"{prompt.strip()}\n\n{''.join(hints)}" if hints else prompt


# encoding_for_model is loaded from utils.tiktoken_encoding (proxy-safe + fallback).


def count_message_image_tokens(messages: list[dict[str, Any]], model: str) -> int:
    return sum(count_image_content_tokens(message.get("content"), model) for message in messages)


def count_message_text_tokens(messages: list[dict[str, Any]], model: str) -> int:
    encoding = encoding_for_model(model)
    total = 0
    for message in messages:
        total += 3
        for key, value in message.items():
            if key == "content" and isinstance(value, list):
                total += len(encoding.encode(message_text(value)))
            elif isinstance(value, str):
                total += len(encoding.encode(value))
            else:
                continue
            if key == "name":
                total += 1
    return total + 3


def count_message_tokens(messages: list[dict[str, Any]], model: str) -> int:
    return count_message_text_tokens(messages, model) + count_message_image_tokens(messages, model)


def count_text_tokens(text: str, model: str) -> int:
    return len(encoding_for_model(model).encode(text))


def format_image_result(
    items: list[dict[str, Any]],
    prompt: str,
    response_format: str,
    base_url: str | None = None,
    created: int | None = None,
    message: str = "",
) -> dict[str, Any]:
    data: list[dict[str, Any]] = []
    for item in items:
        b64_json = str(item.get("b64_json") or "").strip()
        if not b64_json:
            continue
        revised_prompt = str(item.get("revised_prompt") or prompt).strip() or prompt
        if response_format == "b64_json":
            data.append({
                "b64_json": b64_json,
                "url": save_image_bytes(base64.b64decode(b64_json), base_url),
                "revised_prompt": revised_prompt,
            })
        else:
            data.append({
                "url": save_image_bytes(base64.b64decode(b64_json), base_url),
                "revised_prompt": revised_prompt,
            })
    result: dict[str, Any] = {"created": created or int(time.time()), "data": data}
    if message and not data:
        result["message"] = message
    return result


@dataclass
class ConversationRequest:
    model: str = "auto"
    prompt: str = ""
    messages: list[dict[str, Any]] | None = None
    thinking_effort: str = ""
    images: list[str] | None = None
    n: int = 1
    size: str | None = None
    quality: str = "auto"
    response_format: str = "b64_json"
    base_url: str | None = None
    message_as_error: bool = False
    progress_callback: Any = None  # Callable[[str], None] | None
    checkpoint_callback: Any = None  # Internal task recovery metadata; never includes credentials.
    excluded_tokens: set[str] = field(default_factory=set)
    is_cancelled: Any = None
    task_control: ImageTaskControl | None = None


@dataclass
class ConversationState:
    text: str = ""
    raw_text: str = ""
    conversation_id: str = ""
    file_ids: list[str] = field(default_factory=list)
    sediment_ids: list[str] = field(default_factory=list)
    blocked: bool = False
    tool_invoked: bool | None = None
    turn_use_case: str = ""


@dataclass
class ImageOutput:
    kind: str
    model: str
    index: int
    total: int
    created: int = field(default_factory=lambda: int(time.time()))
    text: str = ""
    upstream_event_type: str = ""
    data: list[dict[str, Any]] = field(default_factory=list)
    account_email: str = ""
    conversation_id: str = ""

    def to_chunk(self) -> dict[str, Any]:
        chunk: dict[str, Any] = {
            "object": "image.generation.chunk",
            "created": self.created,
            "model": self.model,
            "index": self.index,
            "total": self.total,
            "progress_text": self.text,
            "upstream_event_type": self.upstream_event_type,
            "data": [],
        }
        if self.account_email:
            chunk["_account_email"] = self.account_email
        if self.conversation_id:
            chunk["_conversation_id"] = self.conversation_id
        if self.kind == "message":
            chunk.update({
                "object": "image.generation.message",
                "message": self.text,
            })
            chunk.pop("progress_text", None)
            chunk.pop("upstream_event_type", None)
        elif self.kind == "result":
            chunk.update({
                "object": "image.generation.result",
                "data": self.data,
            })
            chunk.pop("progress_text", None)
            chunk.pop("upstream_event_type", None)
        return chunk


def assistant_message_text(message: dict[str, Any]) -> str:
    content = message.get("content") or {}
    parts = content.get("parts") or []
    if isinstance(parts, list) and parts:
        text = "".join(part for part in parts if isinstance(part, str))
        if text:
            return text
    # Fallback: content_type "code" stores text in the "text" field instead of "parts"
    text_field = str(content.get("text") or "")
    if text_field:
        # 生图回合的内部编排标记（隐藏 code cell），不是用户可见内容
        if text_field.strip().startswith('{"skipped_mainline"'):
            return ""
        return text_field
    return ""


def strip_history(text: str, history_text: str = "") -> str:
    text = str(text or "")
    history_text = str(history_text or "")
    while history_text and text.startswith(history_text):
        text = text[len(history_text):]
    return text


def sanitize_output_text(text: str) -> str:
    text = str(text or "")

    def is_internal_annotation_part(part: str) -> bool:
        value = part.strip()
        if not value:
            return True
        lower = value.lower()
        return bool(
            re.fullmatch(r"turn\d+[a-z]*\d*", lower)
            or re.fullmatch(r"turn\d+\w*", lower)
            or lower.startswith(("turn", "source", "sources"))
        )

    def readable_annotation_part(parts: list[str]) -> str:
        for part in parts:
            value = part.strip()
            if value and not is_internal_annotation_part(value):
                return value
        return ""

    def replace_annotation(match: re.Match[str]) -> str:
        payload = match.group(1)
        parts = [part.strip() for part in payload.split("\ue202")]
        kind = (parts[0] if parts else "").lower()
        data = parts[1:]
        if kind == "url":
            label = data[0] if data else ""
            url = data[1] if len(data) > 1 else ""
            if label and url.startswith(("http://", "https://")):
                return f"{label} ({url})"
            return label or url
        if kind == "cite":
            return readable_annotation_part(data)
        return readable_annotation_part(data)

    # ChatGPT web sometimes returns rich annotation markers using private-use
    # characters. API clients cannot render those. Preserve readable labels
    # from entity/link annotations, while removing internal citation pointers.
    text = re.sub(r"\ue200([^\ue201]*)\ue201", replace_annotation, text)
    text = re.sub(r"\ue200[^\ue201]*$", "", text)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    return text


def assistant_raw_text(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    for candidate in (event, event.get("v")):
        if not isinstance(candidate, dict):
            continue
        message = candidate.get("message")
        if not isinstance(message, dict):
            continue
        role = str((message.get("author") or {}).get("role") or "").strip().lower()
        if role != "assistant":
            continue
        text = assistant_message_text(message)
        if text:
            return strip_history(text, history_text)
    return apply_text_patch(event, current_text, history_text)


def assistant_text(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    return sanitize_output_text(assistant_raw_text(event, current_text, history_text))


def event_assistant_text(event: dict[str, Any], history_text: str = "") -> str:
    for candidate in (event, event.get("v")):
        if not isinstance(candidate, dict):
            continue
        message = candidate.get("message")
        if isinstance(message, dict) and (message.get("author") or {}).get("role") == "assistant":
            return strip_history(assistant_message_text(message), history_text)
    return ""


def apply_text_patch(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    if event.get("p") == "/message/content/parts/0":
        return apply_patch_op(event, current_text, history_text)

    operations = event.get("v")
    if isinstance(operations, str) and current_text and not event.get("p") and not event.get("o"):
        return current_text + operations

    if event.get("o") == "patch" and isinstance(operations, list):
        text = current_text
        for item in operations:
            if isinstance(item, dict):
                text = apply_text_patch(item, text, history_text)
        return text

    if not isinstance(operations, list):
        return current_text

    text = current_text
    for item in operations:
        if isinstance(item, dict):
            text = apply_text_patch(item, text, history_text)
    return text


def apply_patch_op(operation: dict[str, Any], current_text: str, history_text: str = "") -> str:
    op = operation.get("o")
    value = str(operation.get("v") or "")
    if op == "append":
        return current_text + value
    if op == "replace":
        return strip_history(value, history_text)
    return current_text


def add_unique(values: list[str], candidates: list[str]) -> None:
    for candidate in candidates:
        if candidate and candidate not in values:
            values.append(candidate)


FILE_SERVICE_ID_RE = re.compile(r"file-service://([A-Za-z0-9_-]+)")
FILE_ID_RE = re.compile(r"\b(file[-_](?!service\b)[A-Za-z0-9_-]+)\b")
# 真正的图片文件 ID 格式：file_00000000 + 24位十六进制字符（共32字符）
# 用于过滤非图片文件 ID（如 file_upload_business_upsell）
REAL_IMAGE_FILE_ID_RE = re.compile(r"\bfile_00000000[a-f0-9]{24}\b")
SEDIMENT_ID_RE = re.compile(r"sediment://([A-Za-z0-9_-]+)")


def extract_conversation_ids(payload: str) -> tuple[str, list[str], list[str]]:
    conversation_match = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', payload)
    conversation_id = conversation_match.group(1) if conversation_match else ""
    file_ids: list[str] = []
    # Negative lookahead excludes "file-service" (URI prefix, not a real id).
    add_unique(file_ids, FILE_SERVICE_ID_RE.findall(payload))
    # 只提取真正的图片文件 ID（file_00000000... 格式），过滤非图片文件 ID（如 file_upload_business_upsell）
    add_unique(file_ids, REAL_IMAGE_FILE_ID_RE.findall(payload))
    sediment_ids = SEDIMENT_ID_RE.findall(payload)
    return conversation_id, file_ids, sediment_ids


def is_image_tool_event(event: dict[str, Any]) -> bool:
    value = event.get("v")
    message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
    if not isinstance(message, dict):
        return False
    metadata = message.get("metadata") or {}
    author = message.get("author") or {}
    content = message.get("content") or {}
    if author.get("role") != "tool":
        return False
    if metadata.get("async_task_type") == "image_gen":
        return True
    if content.get("content_type") != "multimodal_text":
        return False
    return any(
        isinstance(part, dict) and (
                part.get("content_type") == "image_asset_pointer"
                or str(part.get("asset_pointer") or "").startswith(("file-service://", "sediment://"))
        )
        for part in content.get("parts") or []
    )


def _is_user_message_event(event: dict[str, Any]) -> bool:
    """输入消息及 user 角色携带的是参考图，不能成为生成输出。"""
    if event.get("type") == "input_message":
        return True
    value = event.get("v")
    if isinstance(value, dict) and value.get("type") == "input_message":
        return True
    message = event.get("input_message") or event.get("message") or (
        value.get("message") if isinstance(value, dict) else None
    )
    if isinstance(message, dict):
        author = message.get("author") or {}
        if str(author.get("role") or "").strip().lower() == "user":
            return True
    return False


def update_conversation_state(state: ConversationState, payload: str, event: dict[str, Any] | None = None) -> None:
    conversation_id, file_ids, sediment_ids = extract_conversation_ids(payload)
    if conversation_id and not state.conversation_id:
        state.conversation_id = conversation_id
    # Accept file_id / sediment_id when any of:
    #   1) event is a complete image_gen tool message
    #   2) prior server_ste_metadata already flipped tool_invoked True (in an image_gen turn),
    #      BUT only for non-user messages — user messages contain the uploaded input image
    #      which must NOT be treated as a generated output.
    #   3) patch event whose payload references asset_pointer / file-service://,
    #      BUT only when the event is not a user message.
    is_patch_event = isinstance(event, dict) and event.get("o") == "patch"
    is_user_msg = isinstance(event, dict) and _is_user_message_event(event)
    image_context = (
        (isinstance(event, dict) and is_image_tool_event(event))
        or (state.tool_invoked is True and not is_user_msg)
        or (is_patch_event and not is_user_msg and ("asset_pointer" in payload or "file-service://" in payload))
    )
    if image_context:
        add_unique(state.file_ids, file_ids)
        add_unique(state.sediment_ids, sediment_ids)
    if not isinstance(event, dict):
        return
    state.conversation_id = str(event.get("conversation_id") or state.conversation_id)
    value = event.get("v")
    if isinstance(value, dict):
        state.conversation_id = str(value.get("conversation_id") or state.conversation_id)
    if event.get("type") == "moderation":
        moderation = event.get("moderation_response")
        if isinstance(moderation, dict) and moderation.get("blocked") is True:
            state.blocked = True
    if event.get("type") == "server_ste_metadata":
        metadata = event.get("metadata")
        if isinstance(metadata, dict):
            if isinstance(metadata.get("tool_invoked"), bool):
                state.tool_invoked = metadata["tool_invoked"]
            state.turn_use_case = str(metadata.get("turn_use_case") or state.turn_use_case)


def conversation_base_event(event_type: str, state: ConversationState, **extra: Any) -> dict[str, Any]:
    return {
        "type": event_type,
        "text": state.text,
        "conversation_id": state.conversation_id,
        "file_ids": list(state.file_ids),
        "sediment_ids": list(state.sediment_ids),
        "blocked": state.blocked,
        "tool_invoked": state.tool_invoked,
        "turn_use_case": state.turn_use_case,
        **extra,
    }


def iter_conversation_payloads(payloads: Iterator[str], history_text: str = "",
                               history_messages: list[str] | None = None) -> Iterator[dict[str, Any]]:
    state = ConversationState()
    history_messages = history_messages or []
    history_index = 0
    for payload in payloads:
        # print(f"[upstream_sse] {payload}", flush=True)
        if not payload:
            continue
        if payload == "[DONE]":
            yield conversation_base_event("conversation.done", state, done=True)
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            update_conversation_state(state, payload)
            yield conversation_base_event("conversation.raw", state, payload=payload)
            continue
        if not isinstance(event, dict):
            yield conversation_base_event("conversation.event", state, raw=event)
            continue
        update_conversation_state(state, payload, event)
        if history_index < len(history_messages) and event_assistant_text(event, history_text) == history_messages[history_index]:
            history_index += 1
            state.raw_text = ""
            state.text = ""
            continue
        next_raw_text = assistant_raw_text(event, state.raw_text, history_text)
        next_text = sanitize_output_text(next_raw_text)
        state.raw_text = next_raw_text
        if next_text != state.text:
            delta = next_text[len(state.text):] if next_text.startswith(state.text) else next_text
            state.text = next_text
            yield conversation_base_event("conversation.delta", state, raw=event, delta=delta)
            continue
        yield conversation_base_event("conversation.event", state, raw=event)


def conversation_events(
    backend: OpenAIBackendAPI,
    messages: list[dict[str, Any]] | None = None,
    model: str = "auto",
    prompt: str = "",
    images: list[str] | None = None,
    size: str | None = None,
    quality: str = "auto",
    thinking_effort: str = "",
) -> Iterator[dict[str, Any]]:
    normalized = normalize_messages(messages or ([{"role": "user", "content": prompt}] if prompt else []))
    image_model = is_supported_image_model(model)
    history_text = "" if image_model else assistant_history_text(normalized)
    history_messages = [] if image_model else assistant_history_messages(normalized)
    final_prompt = prompt_with_global_system(build_image_prompt(prompt, size, quality)) if image_model else prompt
    payloads = backend.stream_conversation(
        messages=normalized,
        model=model,
        prompt=final_prompt,
        images=images if image_model else None,
        system_hints=["picture_v2"] if image_model else None,
        thinking_effort=thinking_effort if not image_model else "",
    )
    yield from iter_conversation_payloads(payloads, history_text, history_messages)


def text_backend() -> OpenAIBackendAPI:
    return OpenAIBackendAPI(access_token=account_service.get_text_access_token())


def stream_text_deltas(backend: OpenAIBackendAPI, request: ConversationRequest) -> Iterator[str]:
    attempted_tokens: set[str] = set()
    token = getattr(backend, "access_token", "")
    emitted = False
    while True:
        if token and token in attempted_tokens:
            raise RuntimeError("no available text account")
        if token:
            attempted_tokens.add(token)
        active_backend = None
        try:
            active_backend = OpenAIBackendAPI(access_token=token)
            for event in conversation_events(
                active_backend,
                messages=request.messages,
                model=request.model,
                prompt=request.prompt,
                thinking_effort=request.thinking_effort,
            ):
                if event.get("type") != "conversation.delta":
                    continue
                delta = str(event.get("delta") or "")
                if delta:
                    emitted = True
                    yield delta
            account_service.mark_text_used(token)
            return
        except Exception as exc:
            error_message = str(exc)
            if token:
                # Soft network failure: hold the account out of text rotation
                # briefly so a dead proxy stops absorbing one doomed request
                # per tick. Non-network errors are ignored by this classifier.
                account_service.note_text_penalty(token, error_message)
            if token and not emitted and is_token_invalid_error(error_message):
                refreshed_token = account_service.refresh_access_token(token, force=True, event="text_stream")
                if refreshed_token and refreshed_token != token and refreshed_token not in attempted_tokens:
                    token = refreshed_token
                else:
                    account_service.remove_invalid_token(token, "text_stream")
                    token = account_service.get_text_access_token(attempted_tokens)
                if token:
                    continue
            raise
        finally:
            if active_backend is not None:
                active_backend.close()
            # Also close the backend passed in by the caller so its
            # requests.Session doesn't leak on every text/chat call.
            try:
                backend.close()
            except Exception:
                pass


def collect_text(backend: OpenAIBackendAPI, request: ConversationRequest) -> str:
    return "".join(stream_text_deltas(backend, request))


def _get_detailed_error_from_tasks(
    backend: OpenAIBackendAPI,
    conversation_id: str,
    timeout_secs: float = 10.0,
    wait_secs: float = 2.0,
) -> str:
    """从 /backend-api/tasks/ 接口获取结构化错误信息。

    当 SSE 流检测到 moderation 拦截时，轮询 tasks 接口获取详细错误文本。
    使用结构化字段（metadata.is_error, author.role, content.content_type）判断，
    而非依赖易变的文本匹配。

    参数：
    - `backend`：OpenAIBackendAPI 实例。
    - `conversation_id`：会话 ID。
    - `timeout_secs`：请求超时秒数。
    - `wait_secs`：等待任务创建的秒数。设为 0 可跳过等待。

    返回：
    - 详细错误信息文本，如果未找到则返回空字符串。
    """
    import time as _time
    try:
        if wait_secs > 0:
            _time.sleep(wait_secs)
        tasks = backend._query_backend_tasks(conversation_id=conversation_id, timeout_secs=timeout_secs)
        if not tasks:
            return ""

        for task in tasks:
            is_error, error_msg, metadata = backend.check_task_error(task)
            if is_error and error_msg:
                logger.info({
                    "event": "image_task_structured_error",
                    "conversation_id": conversation_id,
                    "error_msg": error_msg,
                    "metadata": metadata,
                })
                return error_msg
        return ""
    except Exception as exc:
        logger.warning({
            "event": "image_task_error_query_failed",
            "conversation_id": conversation_id,
            "error": str(exc),
        })
        return ""


def _remove_image_conversation_later(backend: OpenAIBackendAPI, conversation_id: str) -> None:
    if not config.image_remove_conversation_after_result or not conversation_id:
        return

    # Capture the access_token now — the caller's backend may be closed by
    # the time this background thread runs. Create a fresh backend to avoid
    # using a session that has been closed.
    access_token = getattr(backend, "access_token", "") or ""

    def _run() -> None:
        cleanup_backend: OpenAIBackendAPI | None = None
        try:
            cleanup_backend = OpenAIBackendAPI(access_token=access_token)
            cleanup_backend.delete_conversation(conversation_id)
            logger.info({"event": "image_conversation_removed", "conversation_id": conversation_id})
        except Exception as exc:
            logger.warning({
                "event": "image_conversation_remove_failed",
                "conversation_id": conversation_id,
                "error": str(exc),
            })
        finally:
            if cleanup_backend is not None:
                try:
                    cleanup_backend.close()
                except Exception:
                    pass

    threading.Thread(target=_run, name=f"remove-image-conversation-{conversation_id}", daemon=True).start()


# Conversations owned by an in-flight generation in this process. The SSE
# conversation_id is registered as soon as it is seen; the "recover a lost
# conversation_id from the recent-conversations list" fallback must never pick
# one of these, or two generations sharing an account end up returning the same
# upstream image (and one request can receive another's result).
_CLAIMED_CONVERSATION_TTL_SECS = 30 * 60
_claimed_conversations: dict[str, float] = {}
_claimed_conversations_lock = threading.Lock()


def _prune_claimed_locked(now: float) -> None:
    for conversation_id, claimed_at in list(_claimed_conversations.items()):
        if now - claimed_at > _CLAIMED_CONVERSATION_TTL_SECS:
            _claimed_conversations.pop(conversation_id, None)


def claim_conversation(conversation_id: str) -> bool:
    """Mark a conversation as owned. Returns False if another generation holds it."""
    if not conversation_id:
        return False
    now = time.time()
    with _claimed_conversations_lock:
        _prune_claimed_locked(now)
        if conversation_id in _claimed_conversations:
            return False
        _claimed_conversations[conversation_id] = now
        return True


def claimed_conversation_ids() -> set[str]:
    with _claimed_conversations_lock:
        _prune_claimed_locked(time.time())
        return set(_claimed_conversations)


def stream_image_outputs(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        index: int = 1,
        total: int = 1,
) -> Iterator[ImageOutput]:
    last: dict[str, Any] = {}
    checkpoint_conversation_id = ""
    started_at = time.time()
    stream_error: Exception | None = None
    backend._image_has_output = False

    def wait_for_recovery(seconds: float) -> None:
        control = request.task_control or getattr(backend, "task_control", None)
        if control is not None:
            control.wait(seconds)
        else:
            time.sleep(seconds)

    def recover_conversation_id() -> str:
        nonlocal checkpoint_conversation_id
        control = request.task_control or getattr(backend, "task_control", None)
        timeout = control.remaining(5.0) if control is not None else 5.0
        try:
            recovered_id = backend.find_conversation_by_prompt(
                request.prompt, started_at, timeout_secs=timeout,
                exclude_ids=claimed_conversation_ids(),
            )
        except (SseStreamTimeoutError, requests.exceptions.RequestException) as exc:
            if not is_resumable_image_stream_error(exc):
                raise
            if control is not None:
                control.remaining()
            logger.warning({"event": "image_conversation_recovery_transport_error", "error_type": type(exc).__name__})
            return ""
        if control is not None:
            control.remaining()
        if not recovered_id or not claim_conversation(recovered_id):
            return ""
        checkpoint_conversation_id = recovered_id
        backend._image_conversation_id = recovered_id
        if request.checkpoint_callback:
            _call_image_callback(request.checkpoint_callback, {"conversation_id": recovered_id})
        return recovered_id

    events = iter(conversation_events(
        backend,
        prompt=request.prompt,
        model=request.model,
        images=request.images or [],
        size=request.size,
        quality=request.quality,
    ))
    try:
        while True:
            try:
                event = next(events)
            except StopIteration:
                break
            except (SseStreamTimeoutError, requests.exceptions.RequestException) as exc:
                if not is_resumable_image_stream_error(exc):
                    raise
                control = request.task_control or getattr(backend, "task_control", None)
                if control is not None:
                    control.remaining()
                if last.get("blocked"):
                    raise ImageContentPolicyError("Image generation was rejected by upstream policy.") from exc
                if not (last.get("conversation_id") or last.get("file_ids") or last.get("sediment_ids")):
                    raise
                stream_error = exc
                logger.info({
                    "event": "image_stream_recovering_existing_result",
                    "conversation_id": last.get("conversation_id", ""),
                    "error_type": type(exc).__name__,
                })
                break
            # Keep callbacks outside the transport try: an application error
            # must not be interpreted as a socket reset and silently recovered.
            last = event
            if any(item and item != "file_upload" for item in (event.get("file_ids") or [])) or event.get("sediment_ids"):
                backend._image_has_output = True
            event_conversation_id = str(event.get("conversation_id") or "")
            if event_conversation_id and event_conversation_id != checkpoint_conversation_id:
                checkpoint_conversation_id = event_conversation_id
                backend._image_conversation_id = event_conversation_id
                claim_conversation(event_conversation_id)
                if request.checkpoint_callback:
                    _call_image_callback(request.checkpoint_callback, {"conversation_id": event_conversation_id})
            if event.get("type") == "conversation.delta":
                yield ImageOutput(
                    kind="progress",
                    model=request.model,
                    index=index,
                    total=total,
                    text=str(event.get("delta") or ""),
                    upstream_event_type="conversation.delta",
                )
                continue
            if event.get("type") == "conversation.event":
                raw = event.get("raw")
                raw_type = str(raw.get("type") or "") if isinstance(raw, dict) else ""
                yield ImageOutput(
                    kind="progress",
                    model=request.model,
                    index=index,
                    total=total,
                    upstream_event_type=raw_type,
                )
    finally:
        close = getattr(events, "close", None)
        if callable(close):
            close()

    conversation_id = str(last.get("conversation_id") or "")
    file_ids = [str(item) for item in last.get("file_ids") or []]
    sediment_ids = [str(item) for item in last.get("sediment_ids") or []]
    message = str(last.get("text") or "").strip()
    logger.info({
        "event": "image_stream_resolve_start",
        "conversation_id": conversation_id,
        "file_ids": file_ids,
        "sediment_ids": sediment_ids,
        "tool_invoked": last.get("tool_invoked"),
        "turn_use_case": last.get("turn_use_case"),
    })
    if request.progress_callback:
        _call_image_callback(request.progress_callback, "image_stream_resolve_start")
    if message and not file_ids and not sediment_ids and last.get("blocked"):
        # 尝试从 /backend-api/tasks/ 获取详细错误信息
        detailed_error = _get_detailed_error_from_tasks(backend, conversation_id)
        error_text = detailed_error or message or "Image generation was rejected by upstream policy."
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=error_text, conversation_id=conversation_id)
        return
    # This used to compare turn_use_case against the literal "image gen", which
    # upstream never emits (observed values are text/multimodal; the tool form is
    # image_gen). It was therefore always False for text-to-image, which short
    # circuited below and made the whole retry/poll block unreachable — a turn
    # whose SSE dropped before the tool finished came back as a bare text reply
    # and got reported as a policy violation. Match on normalized upstream values
    # plus the "model is still generating" heuristic instead.
    turn_use_case = str(last.get("turn_use_case") or "").replace(" ", "_").lower()
    should_poll_for_image = (
        stream_error is not None
        or bool(request.images)
        or "image" in turn_use_case
        or (is_supported_image_model(request.model) and is_model_text_reply_instead_of_image(message))
    )
    if message and not file_ids and not sediment_ids and not should_poll_for_image:
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message, conversation_id=conversation_id)
        return

    # 检测模型是否返回了文本描述（含 referenced_image_ids）而非实际生成图片
    # 这说明模型已发起图片生成工具调用，但 SSE 在工具完成前断开，
    # 图片可能正在异步生成中。需要使用更积极的轮询策略来获取结果。
    is_text_reply = bool(message and is_model_text_reply_instead_of_image(message))
    if is_text_reply:
        logger.info({
            "event": "image_detected_text_reply_with_ids",
            "conversation_id": conversation_id,
            "message_preview": message[:200],
        })

    # 当检测到文本回复但 conversation_id 丢失时，尝试从最近对话列表中恢复
    # SSE 流太短时（模型返回文本而非触发图片工具），conversation_id 可能未被捕获，
    # 但图片已在上游异步生成。通过列出最近对话来恢复 conversation_id。
    if is_text_reply and not conversation_id:
        recovered_id = recover_conversation_id()
        if recovered_id:
            conversation_id = recovered_id
            logger.info({
                "event": "image_conversation_id_recovered",
                "conversation_id": conversation_id,
                "message_preview": message[:200],
            })

    # 在轮询图片之前，先检查 /backend-api/tasks/ 是否有 moderation 拦截
    # 这样可以避免不必要的长时间轮询超时
    # 注意：当 should_poll_for_image 为 True 或检测到文本回复时，
    # 即使 tasks 报告了"错误"，也不能直接返回——因为上游可能将工具调用的 JSON 参数
    # （如 {"size":"1792x1024","n":1}）标记为 is_error，而实际上图片正在异步生成中。
    # 此时应继续轮询图片。
    detailed_error = ""
    if not file_ids and not sediment_ids and conversation_id:
        # Image-gen turns already poll next; skip the extra 1s tasks settle.
        task_wait = 0.0 if (should_poll_for_image or is_text_reply) else 1.0
        detailed_error = _get_detailed_error_from_tasks(
            backend, conversation_id, timeout_secs=5.0, wait_secs=task_wait,
        )
        if detailed_error and not should_poll_for_image and not is_text_reply:
            logger.info({
                "event": "image_task_error_before_poll",
                "conversation_id": conversation_id,
                "error": detailed_error,
            })
            yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=detailed_error, conversation_id=conversation_id)
            return
        if detailed_error and (should_poll_for_image or is_text_reply):
            logger.info({
                "event": "image_task_error_skipped_for_poll",
                "conversation_id": conversation_id,
                "error": detailed_error,
            })

    # 当检测到文本回复（含 referenced_image_ids）时，使用更长的超时来轮询图片结果。
    # 因为上游可能将图片生成作为异步任务执行，SSE 流在工具完成前就断开了，
    # 导致对话文档中尚未写入图片工具的响应记录。
    poll_timeout = config.image_poll_timeout_secs
    if is_text_reply and conversation_id:
        # 文本回复场景下图片可能仍在异步生成，使用更长超时（默认 120s → 额外 180s = 300s）
        poll_timeout = max(poll_timeout, 300)
        logger.info({
            "event": "image_text_reply_extended_poll",
            "conversation_id": conversation_id,
            "poll_timeout_secs": poll_timeout,
        })

    try:
        image_urls = backend.resolve_conversation_image_urls(
            conversation_id, file_ids, sediment_ids, poll_timeout_secs=poll_timeout,
        )
    except (ImageContentPolicyError, ImagePollTimeoutError) as exc:
        # 当检测到文本回复时，task error 不应直接判定为内容策略违规，
        # 因为图片可能仍在后台异步生成中
        if is_text_reply and isinstance(exc, ImageContentPolicyError):
            logger.warning({
                "event": "image_text_reply_task_error_ignored",
                "conversation_id": conversation_id,
                "error": str(exc),
            })
            image_urls = []
        else:
            raise
    except Exception as exc:
        # Only transient reads can be retried for the same accepted turn.
        if not _is_retryable_image_read_error(exc):
            raise
        if is_text_reply and conversation_id:
            logger.warning({
                "event": "image_text_reply_first_poll_error_ignored",
                "conversation_id": conversation_id,
                "error": repr(exc)[:300],
            })
            image_urls = []
        else:
            raise

    pending_file_ids = [item for item in file_ids if item and item != "file_upload"]
    if not image_urls and (pending_file_ids or sediment_ids):
        logger.warning({
            "event": "image_stream_retry_resolve_after_empty_urls",
            "conversation_id": conversation_id,
            "file_ids": pending_file_ids,
            "sediment_ids": sediment_ids,
        })
        image_urls = backend.resolve_conversation_image_urls(
            conversation_id, pending_file_ids, sediment_ids, poll=False,
        )

    if image_urls:
        if request.progress_callback:
            _call_image_callback(request.progress_callback, "receiving_image")
        image_items = [
            {"b64_json": base64.b64encode(image_data).decode("ascii")}
            for image_data in backend.download_image_bytes(image_urls)
        ]
        data = format_image_result(
            image_items,
            request.prompt,
            request.response_format,
            request.base_url,
            int(time.time()),
        )["data"]
        if data:
            _remove_image_conversation_later(backend, conversation_id)
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data, conversation_id=conversation_id)
        return

    if pending_file_ids or sediment_ids:
        # SSE already produced an image id. Do not surface a model follow-up
        # (「你更喜欢…？」) as content_policy_violation.
        raise RuntimeError("upstream image connection failed: download url unresolved")

    if message:
        # 检测模型是否返回了文本描述（含 referenced_image_ids）而非实际生成图片
        # 这说明模型已发起图片生成工具调用，但 SSE 在工具完成前断开。
        # 此时应再尝试轮询图片结果，而不是直接把文本当作最终输出。
        # 当 is_text_reply 但 conversation_id 丢失时，尝试从最近对话列表恢复
        if is_text_reply and not conversation_id:
            recovered_id = recover_conversation_id()
            if recovered_id:
                conversation_id = recovered_id
                logger.info({
                    "event": "image_text_reply_conversation_id_recovered",
                    "conversation_id": conversation_id,
                    "message_preview": message[:200],
                })
        if is_text_reply and conversation_id:
            logger.info({
                "event": "image_model_text_reply_retry_poll",
                "conversation_id": conversation_id,
                "message_preview": message[:200],
            })
            # 文本回复场景下，图片可能需要 4-5 分钟才能异步生成完成。
            # 使用 300s 超时并允许多次重试，避免因临时网络问题提前退出。
            retry_poll_timeout = max(config.image_poll_timeout_secs, 300)
            MAX_POLL_RETRIES = 3
            for poll_attempt in range(1, MAX_POLL_RETRIES + 1):
                try:
                    polled_file_ids, polled_sediment_ids = backend._poll_image_results(
                        conversation_id,
                        retry_poll_timeout,
                        file_ids,
                        sediment_ids,
                    )
                    file_ids.extend(item for item in polled_file_ids if item and item not in file_ids)
                    sediment_ids.extend(item for item in polled_sediment_ids if item and item not in sediment_ids)
                    break  # 轮询成功，退出重试循环
                except Exception as exc:
                    is_transient = _is_retryable_image_read_error(exc)
                    logger.warning({
                        "event": "image_model_text_reply_poll_failed",
                        "conversation_id": conversation_id,
                        "poll_attempt": poll_attempt,
                        "error": repr(exc)[:300],
                        "is_transient": is_transient,
                    })
                    if not is_transient:
                        raise
                    if poll_attempt < MAX_POLL_RETRIES and not isinstance(exc, ImagePollTimeoutError):
                        backoff = 30.0 * poll_attempt
                        wait_for_recovery(backoff)
                        continue
                    exc.conversation_id = conversation_id
                    raise

            if file_ids or sediment_ids:
                image_urls = backend.resolve_conversation_image_urls(
                    conversation_id, file_ids, sediment_ids, poll=False,
                )
                if image_urls:
                    if request.progress_callback:
                        _call_image_callback(request.progress_callback, "receiving_image")
                    image_items = [
                        {"b64_json": base64.b64encode(image_data).decode("ascii")}
                        for image_data in backend.download_image_bytes(image_urls)
                    ]
                    data = format_image_result(
                        image_items,
                        request.prompt,
                        request.response_format,
                        request.base_url,
                        int(time.time()),
                    )["data"]
                    if data:
                        _remove_image_conversation_later(backend, conversation_id)
                        yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data, conversation_id=conversation_id)
                        return
        elif is_text_reply:
            logger.warning({
                "event": "image_model_text_reply_no_image",
                "conversation_id": conversation_id,
                "message_preview": message[:200],
            })
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message, conversation_id=conversation_id)
        return

    # 兜底：当 message 为空且图片 URL 解析失败时，先尝试一次短延迟重试轮询
    # 然后抛出明确错误而非让调用方得到 "upstream completed without generating images" 这种模糊报错
    logger.warning({
        "event": "image_stream_no_result_fallback",
        "conversation_id": conversation_id,
        "file_ids": file_ids,
        "sediment_ids": sediment_ids,
        "should_poll_for_image": should_poll_for_image,
    })
    # 当 should_poll_for_image 为 True 但 conversation_id 丢失时，尝试恢复
    if should_poll_for_image and not conversation_id:
        recovered_id = recover_conversation_id()
        if recovered_id:
            conversation_id = recovered_id
            logger.info({
                "event": "image_fallback_conversation_id_recovered",
                "conversation_id": conversation_id,
            })
    if should_poll_for_image and conversation_id:
        # 图片可能仍在异步处理中（上游 SSE 流在图片生成完成前就结束了）。
        # 使用 300s 超时并允许多次重试，避免因临时网络问题或图片尚未提交而提前退出。
        retry_poll_timeout = max(config.image_poll_timeout_secs, 300)
        MAX_FALLBACK_POLL_RETRIES = 3
        for poll_attempt in range(1, MAX_FALLBACK_POLL_RETRIES + 1):
            retry_wait_secs = min(30.0 * poll_attempt, config.image_poll_initial_wait_secs * poll_attempt)
            logger.info({
                "event": "image_stream_retry_poll_after_wait",
                "conversation_id": conversation_id,
                "retry_wait_secs": retry_wait_secs,
                "poll_attempt": poll_attempt,
            })
            wait_for_recovery(retry_wait_secs)
            try:
                polled_file_ids, polled_sediment_ids = backend._poll_image_results(
                    conversation_id,
                    retry_poll_timeout,
                    file_ids,
                    sediment_ids,
                )
                file_ids.extend(item for item in polled_file_ids if item and item not in file_ids)
                sediment_ids.extend(item for item in polled_sediment_ids if item and item not in sediment_ids)
                break  # 轮询成功，退出重试循环
            except Exception as exc:
                is_transient = _is_retryable_image_read_error(exc)
                logger.warning({
                    "event": "image_stream_retry_poll_failed",
                    "conversation_id": conversation_id,
                    "poll_attempt": poll_attempt,
                    "error": repr(exc)[:300],
                    "is_transient": is_transient,
                })
                if not is_transient:
                    raise
                if poll_attempt < MAX_FALLBACK_POLL_RETRIES and not isinstance(exc, ImagePollTimeoutError):
                    backoff = 30.0 * poll_attempt
                    wait_for_recovery(backoff)
                    continue
                exc.conversation_id = conversation_id
                raise
        
        if file_ids or sediment_ids:
            image_urls = backend.resolve_conversation_image_urls(
                conversation_id, file_ids, sediment_ids, poll=False,
            )
            if image_urls:
                if request.progress_callback:
                    _call_image_callback(request.progress_callback, "receiving_image")
                image_items = [
                    {"b64_json": base64.b64encode(image_data).decode("ascii")}
                    for image_data in backend.download_image_bytes(image_urls)
                ]
                data = format_image_result(
                    image_items,
                    request.prompt,
                    request.response_format,
                    request.base_url,
                    int(time.time()),
                )["data"]
                if data:
                    _remove_image_conversation_later(backend, conversation_id)
                    yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data, conversation_id=conversation_id)
                    return
        
        # 重试后仍然失败，yield 错误消息
        yield ImageOutput(kind="message", model=request.model, index=index, total=total,
                          text="Image generation completed upstream but the result could not be retrieved. "
                               "The image may still be processing. Please try again in a moment.",
                          conversation_id=conversation_id)
    elif message:
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message, conversation_id=conversation_id)
    else:
        # conversation_id 也为空时（SSE 流极短、未捕获到会话 ID），
        # 仍然 yield 一条消息，避免 stream_image_outputs_with_pool 产生
        # "upstream completed without generating images" 模糊报错
        yield ImageOutput(kind="message", model=request.model, index=index, total=total,
                          text="Image generation started upstream but the response was incomplete. "
                               "Please try again.",
                          conversation_id=conversation_id)


def _codex_response_images(value: Any) -> list[str]:
    if isinstance(value, dict):
        if value.get("type") == "image_generation_call" and isinstance(value.get("result"), str):
            result = value["result"].strip()
            if result:
                return [result.split(",", 1)[1] if result.startswith("data:image/") else result]
        images: list[str] = []
        for item in value.values():
            images.extend(_codex_response_images(item))
        return images
    if isinstance(value, list):
        images: list[str] = []
        for item in value:
            images.extend(_codex_response_images(item))
        return images
    return []


def stream_codex_image_outputs(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        index: int = 1,
        total: int = 1,
) -> Iterator[ImageOutput]:
    images = _codex_response_images(list(backend.iter_codex_image_response_events(
        prompt=request.prompt,
        images=request.images or [],
        size=request.size,
        quality=request.quality,
    )))
    if not images:
        raise ImageGenerationError("No image result found in response")
    data = format_image_result(
        [{"b64_json": item, "revised_prompt": request.prompt} for item in images],
        request.prompt,
        request.response_format,
        request.base_url,
        int(time.time()),
    )["data"]
    if data:
        yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data)
        return
    raise ImageGenerationError("No image result found in response")


class ImageResultSettlementError(RuntimeError):
    pass


class _ImageAccountLease:
    """Own exactly one reservation across sticky retries and token rotation."""
    def __init__(self) -> None:
        self.token = ""
        self.result_id = ""

    def release(self) -> None:
        token, self.token = self.token, ""
        if token:
            account_service.release_image_slot(token)

    def finish(self, success: bool) -> None:
        if not self.token:
            return
        try:
            kwargs: dict[str, Any] = {"release_slot": False}
            if success:
                kwargs["result_id"] = self.result_id
            account_service.mark_image_result(self.token, success, **kwargs)
        except Exception as exc:
            raise ImageResultSettlementError("图片账号结算保存失败，请稍后续传") from exc
        finally:
            self.release()


def _generate_single_image(request: ConversationRequest, index: int, total: int) -> list[ImageOutput]:
    lease = _ImageAccountLease()
    try:
        return _generate_single_image_impl(request, index, total, lease)
    finally:
        # Includes checkpoints, proxy setup, backend construction/close, and
        # exceptions raised inside retry handlers, not just stream failures.
        lease.release()


def _generate_single_image_impl(
        request: ConversationRequest,
        index: int,
        total: int,
        lease: _ImageAccountLease,
) -> list[ImageOutput]:
    """为单张图片执行生成逻辑（含重试），返回结果列表。

    该函数在独立线程中运行，每个线程使用不同的账号，
    实现并行生图，避免串行超时阻塞。
    """
    # 全局熔断：-convo 后端整体回 404 Model not found 时，新请求不该再走一遍
    # 号池（每个凭据都要白等一轮 3.6~4.9 秒的上游往返）。熔断窗口很短
    # （默认 60 秒），上游恢复后自动放行。
    breaker_left = model_not_found_breaker_remaining()
    if breaker_left > 0:
        raise ImageGenerationError(
            "Upstream conversation backend is answering 404 Model not found "
            "globally; failing fast until the breaker window elapses.",
            status_code=503,
            code="upstream_model_not_found_global",
            account_email="",
        )

    # TLS/超时：同出口短重试；代理拒连则立刻换下一条出口
    MAX_TLS_RETRIES = 3
    MAX_CONNECTION_ACCOUNT_RETRIES = max(1, int(config.image_account_failover_retries))
    MAX_CONN_TIMEOUT_RETRIES = 3
    MAX_POLL_TIMEOUT_RETRIES = max(0, int(config.image_poll_failover_retries))
    MAX_TEXT_REPLY_RETRIES = max(0, int(config.image_text_failover_retries))

    text_reply_retry_count = 0
    poll_timeout_retry_count = 0
    account_email = ""
    state: dict[str, Any] = {
        "tls_retry_count": 0,
        "conn_timeout_retry_count": 0,
        "sticky_token": "",
        "egress_queue": [],
        "current_proxy_url": "",
        "current_proxy_source": "",
        "failed_proxy_urls": set(),
        "failed_connection_tokens": set(request.excluded_tokens),
        "last_connection_error": "",
    }
    refreshed_tokens: set[str] = set()

    def exclude_account(active_token: str, error: str, *, revoked: bool = False,
                        failure_kind: str = "", retry_after_secs: float = 0.0) -> None:
        state["failed_connection_tokens"].add(active_token)
        state["last_connection_error"] = error
        state["sticky_token"] = ""
        state["tls_retry_count"] = 0
        state["conn_timeout_retry_count"] = 0
        if request.checkpoint_callback:
            checkpoint = {
                "failed_account_email": account_email,
                "failed_token_hash": hashlib.sha256(active_token.encode()).hexdigest(),
                "failure_kind": failure_kind or ("revoked" if revoked else "transient"),
            }
            # Carry the upstream-stated window so the task service parks this
            # credential for exactly as long as upstream said, instead of the
            # generic image_transient_failure_cooldown_secs (60s) that lets the
            # picker hand the same exhausted account straight back out.
            if retry_after_secs > 0:
                checkpoint["retry_after_secs"] = float(retry_after_secs)
            _call_image_callback(request.checkpoint_callback, checkpoint)
        if request.progress_callback:
            _call_image_callback(request.progress_callback, "switching_account")

    def recover_auth(active_token: str, error: str) -> None:
        # Keep the reserved slot while refreshing: token rotation migrates it.
        refreshed = None
        if active_token not in refreshed_tokens:
            refreshed_tokens.add(active_token)
            refreshed = account_service.refresh_access_token(active_token, force=True, event="image_stream")
        if refreshed and refreshed != active_token and refreshed not in refreshed_tokens:
            refreshed_tokens.add(refreshed)
            lease.token = refreshed
            state["sticky_token"] = refreshed
            return
        lease.finish(False)
        revoked = False
        if not is_soft_prepare_auth_error(error):
            # Persist hard invalidation evidence before remove_invalid_token:
            # session-only rows may be retained and must not become selectable
            # again on the next request just because local quota is positive.
            account_service._record_invalid_token_seen(active_token, "image_stream", error)
            account_service.remove_invalid_token(active_token, "image_stream:auth_failed")
            revoked = AccountService._token_looks_revoked({"last_refresh_error": error})
        elif account_service.confirm_token_revoked(active_token):
            revoked = True
            account_service.remove_invalid_token(active_token, "image_stream:auth_failed")
        exclude_account(active_token, error, revoked=revoked)

    def recover_connection(active_token: str, last_error: str) -> bool:
        """同一账号换出口，或换号。返回 True 表示外层应 continue。"""
        state["last_connection_error"] = last_error
        if is_proxy_unreachable_error(last_error) or is_connection_timeout_error(last_error):
            try:
                from services.proxy_service import mark_egress_unusable

                mark_egress_unusable(str(state.get("current_proxy_url") or ""), last_error)
            except Exception:
                pass
        # OPENSSL_internal is a local curl handle glitch. Same SOCKS often
        # works after a new session; jumping to direct just times out on WSL.
        if not (
            is_proxy_unreachable_error(last_error)
            or is_connection_timeout_error(last_error)
        ):
            if is_connection_timeout_error(last_error):
                state["conn_timeout_retry_count"] += 1
                retry_count = int(state["conn_timeout_retry_count"])
                max_retries = MAX_CONN_TIMEOUT_RETRIES
                wait_secs = min(1.0 * retry_count, 3.0)
                retry_event = "image_stream_conn_timeout_retry"
            else:
                state["tls_retry_count"] += 1
                retry_count = int(state["tls_retry_count"])
                max_retries = MAX_TLS_RETRIES
                wait_secs = min(0.4 * retry_count, 1.5)
                retry_event = "image_stream_tls_retry"
            if retry_count <= max_retries:
                logger.warning({
                    "event": retry_event,
                    "request_token": active_token,
                    "account_email": account_email,
                    "retry_count": retry_count,
                    "index": index,
                    "wait_secs": wait_secs,
                    "sticky": True,
                    "proxy_source": state["current_proxy_source"],
                    "error": last_error[:200],
                })
                time.sleep(wait_secs)
                state["sticky_token"] = active_token
                return True

        dead_proxy = str(state["current_proxy_url"])
        failed_proxies: set[str] = state["failed_proxy_urls"]
        failed_proxies.add(dead_proxy)
        queue: list[tuple[str, str]] = [
            (src, url) for src, url in state["egress_queue"] if url not in failed_proxies
        ]
        if is_openssl_invalid_library_error(last_error):
            queue = [(src, url) for src, url in queue if url]
        state["egress_queue"] = queue
        state["tls_retry_count"] = 0
        state["conn_timeout_retry_count"] = 0
        if queue:
            logger.warning({
                "event": "image_stream_connection_proxy_failover",
                "request_token": active_token,
                "account_email": account_email,
                "from_proxy_source": state["current_proxy_source"],
                "next_proxy_source": queue[0][0],
                "index": index,
                "error": last_error[:200],
            })
            state["sticky_token"] = active_token
            return True

        failed_tokens: set[str] = state["failed_connection_tokens"]
        lease.finish(False)
        exclude_account(active_token, last_error)
        if len(failed_tokens) >= MAX_CONNECTION_ACCOUNT_RETRIES:
            return False
        logger.warning({
            "event": "image_stream_connection_account_failover",
            "request_token": active_token,
            "account_email": account_email,
            "failed_accounts": len(failed_tokens),
            "index": index,
            "error": last_error[:200],
        })
        return True

    while True:
        if request.is_cancelled and request.is_cancelled():
            if state["sticky_token"]:
                lease.release()
            raise ImageGenerationError("image task attempt cancelled", code="task_cancelled")
        try:
            if request.progress_callback:
                _call_image_callback(request.progress_callback, "getting_account")
            plan_type, _ = split_image_model(request.model)
            codex_model = is_codex_image_model(request.model)
            if state["sticky_token"]:
                token = str(state["sticky_token"])
            else:
                token = account_service.get_available_access_token(
                    plan_type=plan_type,
                    source_type="codex" if codex_model else None,
                    plan_types=("plus", "team", "pro") if codex_model and not plan_type else None,
                    excluded_tokens=state["failed_connection_tokens"],
                )
                lease.token = token
            if token in state["failed_connection_tokens"]:
                raise ImageGenerationError(
                    image_stream_error_message(str(state["last_connection_error"] or "image generation failed")),
                    account_email=account_email,
                )
        except RuntimeError as exc:
            message_l = str(exc).lower()
            unavailable = "no available" in message_l
            # Picker wait budget exhausted: the pool exists, slots are just
            # busy right now. That is retryable throttling, not a server fault.
            busy = not unavailable and "wait budget exhausted" in message_l
            message = str(exc) or "image generation failed"
            if state["failed_connection_tokens"] and state["last_connection_error"]:
                last_message = image_stream_error_message(str(state["last_connection_error"]))
                if unavailable:
                    message = f"{message}; last attempt: {last_message}"
                elif not busy:
                    # busy keeps the wait-budget explanation: it says why 429
                    message = last_message
            raise ImageGenerationError(
                message, account_email=account_email,
                status_code=429 if (unavailable or busy) else 502,
                error_type="insufficient_quota" if unavailable else ("rate_limit_exceeded" if busy else "server_error"),
                code="insufficient_quota" if unavailable else ("upstream_busy" if busy else "upstream_error"),
                headers={"Retry-After": "30"} if busy else None,
            ) from exc

        # The slot picker may have waited behind another request while the
        # enclosing task timed out. Do not launch a new upstream request now.
        if request.is_cancelled and request.is_cancelled():
            lease.release()
            raise ImageGenerationError("image task attempt cancelled", code="task_cancelled")

        emitted_for_token = False
        returned_message = False
        returned_result = False
        account = account_service.get_account(token) or {}
        account_email = str(account.get("email") or "").strip()
        lease.result_id = uuid.uuid4().hex
        if request.checkpoint_callback:
            _call_image_callback(request.checkpoint_callback, {
                "account_email": account_email, "conversation_id": "",
                "account_token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "generation_id": lease.result_id,
            })
        logger.debug({
            "event": "image_account_lookup",
            "token_prefix": token[:12] + "..." if len(token) > 12 else token,
            "account_email": account_email,
            "account_found": bool(account),
            "index": index,
        })
        if not state["sticky_token"] or not state["egress_queue"]:
            failed_proxies = state["failed_proxy_urls"]
            candidates = proxy_settings.list_egress_candidates(account=account, upstream=True)
            state["egress_queue"] = [(src, url) for src, url in candidates if url not in failed_proxies]
            if not state["egress_queue"]:
                state["failed_connection_tokens"].add(token)
                lease.finish(False)
                state["sticky_token"] = ""
                if state["last_connection_error"] and len(state["failed_connection_tokens"]) >= MAX_CONNECTION_ACCOUNT_RETRIES:
                    raise ImageGenerationError(
                        image_stream_error_message(str(state["last_connection_error"])),
                        account_email=account_email,
                    )
                continue
        proxy_source, proxy_url = state["egress_queue"][0]
        state["current_proxy_source"] = proxy_source
        state["current_proxy_url"] = proxy_url
        backend = None
        outputs: list[ImageOutput] = []
        try:
            if request.progress_callback:
                _call_image_callback(request.progress_callback, "preparing_conversation")
            backend = OpenAIBackendAPI(access_token=token, force_proxy=proxy_url)
            backend.task_control = request.task_control
            if request.progress_callback:
                backend.progress_callback = request.progress_callback
            stream_fn = stream_codex_image_outputs if is_codex_image_model(request.model) else stream_image_outputs
            for output in stream_fn(backend, request, index, total):
                if request.is_cancelled and request.is_cancelled():
                    raise ImageGenerationError("image task attempt cancelled", code="task_cancelled")
                if output.conversation_id and request.checkpoint_callback:
                    _call_image_callback(request.checkpoint_callback, {"conversation_id": output.conversation_id})
                if account_email and not output.account_email:
                    output.account_email = account_email
                if output.kind == "progress":
                    outputs.append(output)
                    continue
                if output.kind == "message" and request.message_as_error:
                    text = output.text or ""
                    if is_clarifying_image_followup(text):
                        raise ImageGenerationError(
                            text or "Image generation did not complete.",
                            status_code=502,
                            error_type="server_error",
                            code="upstream_text_reply",
                            account_email=account_email,
                            conversation_id=output.conversation_id,
                        )
                    raise ImageGenerationError(
                        text or "Image generation was rejected by upstream policy.",
                        status_code=400,
                        error_type="invalid_request_error",
                        code="content_policy_violation",
                        account_email=account_email,
                        conversation_id=output.conversation_id,
                    )
                emitted_for_token = True
                returned_message = output.kind == "message"
                returned_result = returned_result or output.kind == "result"
                outputs.append(output)
            if returned_message:
                lease.finish(False)
                return outputs
            if not returned_result:
                if emitted_for_token:
                    conv_id = outputs[-1].conversation_id if outputs else ""
                    raise ImageGenerationError(
                        "upstream completed without generating images",
                        status_code=400,
                        error_type="invalid_request_error",
                        code="no_image_generated",
                        account_email=account_email,
                        conversation_id=conv_id,
                    )
                raise ImageGenerationError("upstream completed without generating images", code="upstream_text_reply")
            lease.finish(True)
            return outputs
        except ImageResultSettlementError:
            raise
        except ArkoseRequiredError as exc:
            # A risk verdict on this account/egress, not a dead token: it used
            # to fall through to the generic branch and fail the request on the
            # first account. Settle it as a plain failure (no invalidation) and
            # let the next account try.
            error_text = str(exc)
            logger.warning({
                "event": "image_arkose_required",
                "request_token": token,
                "account_email": account_email,
                "proxy_source": state["current_proxy_source"],
                "index": index,
            })
            lease.finish(False)
            exclude_account(token, error_text)
            if len(state["failed_connection_tokens"]) < MAX_CONNECTION_ACCOUNT_RETRIES:
                continue
            raise ImageGenerationError(
                "Upstream demanded an Arkose challenge from every account tried; "
                "please retry later or switch the egress IP.",
                status_code=503,
                code="upstream_arkose_required",
                account_email=account_email,
            ) from exc
        except (ToolCooldownError, ToolQuotaError) as exc:
            if getattr(backend, "_image_has_output", False) is True and not returned_result:
                lease.finish(False)
                raise
            # A refusal before any output is observed may fail over.
            # Both kinds are per-credential pacing verdicts, so they must not
            # fail the request while other accounts still have budget: park the
            # credential for the window upstream stated and move on.
            is_quota = isinstance(exc, ToolQuotaError)
            retry_after_secs = float(getattr(exc, "retry_after_secs", 0.0) or 0.0)
            if retry_after_secs <= 0:
                retry_after_secs = float(config.image_tool_quota_cooldown_secs) if is_quota \
                    else float(config.image_tool_short_cooldown_secs)
            error_text = str(exc)
            logger.warning({
                "event": "image_tool_quota_exhausted" if is_quota else "image_tool_cooldown",
                "request_token": token,
                "account_email": account_email,
                "retry_after_secs": retry_after_secs,
                "failed_accounts": len(state["failed_connection_tokens"]),
                "index": index,
            })
            # Results already emitted for this token still win: never discard a
            # finished image because the tail of the stream carried a refusal.
            if returned_result and outputs:
                lease.finish(True)
                return outputs
            lease.finish(False)
            exclude_account(
                token, error_text,
                failure_kind="quota" if is_quota else "cooldown",
                retry_after_secs=retry_after_secs,
            )
            # Park the credential for the window upstream stated. The picker
            # already skips it via failed_connection_tokens for this task, but
            # without a park the *next* request would immediately re-pick an
            # account whose gate is closed for hours.
            account_service.park_image_account(
                token, until_ts=time.time() + retry_after_secs,
                kind="quota" if is_quota else "cooldown",
                reason=error_text,
            )
            if len(state["failed_connection_tokens"]) < MAX_CONNECTION_ACCOUNT_RETRIES:
                continue
            raise ImageGenerationError(
                "Every account tried hit the upstream image generation limit; "
                "no account in the pool can generate right now. "
                f"Earliest reset in about {max(1, int(retry_after_secs // 60))} minute(s).",
                status_code=429,
                error_type="insufficient_quota",
                code="tool_quota_exhausted" if is_quota else "tool_rate_limited",
                account_email=account_email,
            ) from exc
        except ImagePollTimeoutError as exc:
            if account_email:
                setattr(exc, "account_email", account_email)
            # 结果已到手：收尾阶段的流错误不能把已生成的图片判死。
            if returned_result and outputs:
                lease.finish(True)
                return outputs
            lease.finish(False)
            accepted_id = _accepted_image_conversation_id(exc, backend)
            if accepted_id or getattr(backend, "_image_has_output", False) is True:
                exc.conversation_id = accepted_id
                if accepted_id and request.checkpoint_callback:
                    _call_image_callback(request.checkpoint_callback, {"conversation_id": accepted_id})
                # No other account may regenerate a turn with an accepted
                # conversation or a generated output already observed.
                raise
            # Only a failure without a trustworthy checkpoint may use the
            # existing bounded account-failover path.
            exclude_account(token, str(exc))
            if request.checkpoint_callback and getattr(exc, "conversation_id", ""):
                _call_image_callback(request.checkpoint_callback, {"conversation_id": exc.conversation_id})
            poll_timeout_retry_count += 1
            if poll_timeout_retry_count <= MAX_POLL_TIMEOUT_RETRIES:
                logger.warning({
                    "event": "image_poll_timeout_retry",
                    "request_token": token,
                    "account_email": account_email,
                    "retry_count": poll_timeout_retry_count,
                    "index": index,
                    "error": str(exc)[:200],
                })
                continue
            logger.warning({
                "event": "image_poll_timeout_exhausted_retries",
                "request_token": token,
                "account_email": account_email,
                "retry_count": poll_timeout_retry_count,
                "index": index,
            })
            raise
        except ImageContentPolicyError as exc:
            if returned_result and outputs:
                lease.finish(True)
                return outputs
            lease.finish(False)
            logger.warning({
                "event": "image_stream_content_policy_error",
                "request_token": token,
                "account_email": account_email,
                "error": str(exc),
                "index": index,
            })
            raise ImageGenerationError(
                str(exc) or "Image generation was rejected by upstream policy.",
                status_code=400,
                error_type="invalid_request_error",
                code="content_policy_violation",
                account_email=account_email,
                conversation_id=getattr(exc, "conversation_id", ""),
            ) from exc
        except ImageGenerationError as exc:
            if account_email and not getattr(exc, "account_email", ""):
                exc.account_email = account_email
            error_text = str(exc)
            # 结果已到手：收尾阶段的错误不能把已生成的图片判死，直接返回。
            if returned_result and outputs:
                lease.finish(True)
                return outputs
            accepted_id = _accepted_image_conversation_id(exc, backend)
            if accepted_id or getattr(backend, "_image_has_output", False) is True:
                lease.finish(False)
                exc.conversation_id = accepted_id
                raise
            if not emitted_for_token and is_token_invalid_error(error_text):
                recover_auth(token, error_text)
                continue
            # 如果是模型返回文本而非图片，尝试换账号重试
            if (
                not emitted_for_token
                and (
                    is_model_text_reply_instead_of_image(error_text)
                    or is_clarifying_image_followup(error_text)
                    or getattr(exc, "code", "") == "upstream_text_reply"
                )
            ):
                lease.finish(False)
                exclude_account(token, error_text)
                text_reply_retry_count += 1
                if text_reply_retry_count <= MAX_TEXT_REPLY_RETRIES:
                    logger.warning({
                        "event": "image_model_text_reply_retry",
                        "request_token": token,
                        "account_email": account_email,
                        "retry_count": text_reply_retry_count,
                        "index": index,
                        "error": error_text[:200],
                    })
                    continue
                logger.warning({
                    "event": "image_model_text_reply_exhausted_retries",
                    "request_token": token,
                    "account_email": account_email,
                    "retry_count": text_reply_retry_count,
                    "index": index,
                })
                raise ImageGenerationError(
                    "Image generation failed: the upstream model returned a text description "
                    "instead of generating an image. Please try again later.",
                    status_code=502,
                    error_type="server_error",
                    code="upstream_text_reply",
                    account_email=account_email,
                    conversation_id=getattr(exc, "conversation_id", ""),
                ) from exc
            if not emitted_for_token and is_upstream_connection_error(error_text):
                if _accepted_image_conversation_id(exc, backend):
                    lease.finish(False)
                    exc.conversation_id = _accepted_image_conversation_id(exc, backend)
                    raise
                logger.warning({
                    "event": "image_stream_fail",
                    "request_token": token,
                    "account_email": account_email,
                    "error": error_text,
                    "index": index,
                })
                if recover_connection(token, error_text):
                    continue
                raise ImageGenerationError(
                    image_stream_error_message(error_text),
                    account_email=account_email,
                ) from exc
            lease.finish(False)
            logger.warning({
                "event": "image_stream_generation_error",
                "request_token": token,
                "account_email": account_email,
                "error": error_text,
                "index": index,
            })
            raise
        except Exception as exc:
            last_error = str(exc)
            logger.warning({
                "event": "image_stream_fail",
                "request_token": token,
                "account_email": account_email,
                "error": last_error,
                "index": index,
            })
            # 结果已到手：收尾阶段的流错误不能把已生成的图片判死，直接返回。
            if returned_result and outputs:
                lease.finish(True)
                return outputs
            accepted_id = _accepted_image_conversation_id(exc, backend)
            if accepted_id or getattr(backend, "_image_has_output", False) is True:
                lease.finish(False)
                if not _is_retryable_image_read_error(exc):
                    raise
                pending_message = (
                    "Image generation started upstream but its result is incomplete. Resume polling the existing conversation."
                    if accepted_id else
                    "Generated output exists upstream but could not be downloaded. Retrieve the existing output instead of generating again."
                )
                raise ImageGenerationError(
                    pending_message,
                    code="upstream_result_pending", account_email=account_email, conversation_id=accepted_id,
                ) from exc
            if not emitted_for_token and is_model_not_found_error(exc):
                # 不能像瞬态错误那样把账号放回池子——那只会让下一次请求再挑中它，
                # 白等一轮 3.6~4.9 秒的上游往返。
                #
                # 但也不能一律按「账号级终态」处理：匿名链路同样回这个 404
                # （见 is_model_not_found_error 的说明），说明它可能是 -convo
                # 后端的全局故障。熔断器据此把两种情况分开——单个凭据命中按
                # 配额冷却长停，多个不同凭据在窗口内命中则判定为全局故障，
                # 短停并在窗口内快速失败，避免把整个号池按 24 小时烧掉。
                lease.finish(False)
                exclude_account(token, last_error, failure_kind="model_unavailable")
                verdict = note_model_not_found(token)
                account_service.park_image_account(
                    token,
                    until_ts=time.time() + verdict.park_secs,
                    kind="model_unavailable",
                    reason=last_error,
                )
                if verdict.is_global:
                    # 之前几个凭据是在「看起来只是局部故障」时被长停的，
                    # 现在判定为全局故障，必须把它们的停放一并改短，
                    # 否则一次后端抖动仍会烧掉它们 24 小时。
                    for earlier in verdict.window_tokens:
                        if earlier == token:
                            continue
                        account_service.park_image_account(
                            earlier,
                            until_ts=time.time() + MODEL_NOT_FOUND_BREAKER_SECS,
                            kind="model_unavailable",
                            reason=last_error,
                        )
                logger.warning({
                    "event": "image_model_not_found",
                    "request_token": token,
                    "account_email": account_email,
                    "park_secs": int(verdict.park_secs),
                    "global": verdict.is_global,
                    "window_accounts": len(verdict.window_tokens),
                    "breaker_secs": int(model_not_found_breaker_remaining()),
                    "failed_accounts": len(state["failed_connection_tokens"]),
                    "index": index,
                    "error": last_error[:200],
                })
                if verdict.is_global:
                    # 全局故障：不再逐个账号重试（每个都要等一轮上游往返），
                    # 直接把判定结果交给调用方，并保留原始错误便于排查。
                    raise ImageGenerationError(
                        "Upstream conversation backend answered 404 Model not found "
                        "for multiple distinct credentials, which the anonymous "
                        "surfaces reproduce without any credential at all — this is "
                        "a backend-wide condition, not a spent account.",
                        status_code=503,
                        code="upstream_model_not_found_global",
                        account_email=account_email,
                    ) from exc
                if len(state["failed_connection_tokens"]) < MAX_CONNECTION_ACCOUNT_RETRIES:
                    continue
                raise ImageGenerationError(
                    "No account in the pool exposes a usable conversation model; "
                    "upstream answered 404 Model not found for every credential tried.",
                    status_code=503,
                    code="upstream_model_not_found",
                    account_email=account_email,
                ) from exc
            if not emitted_for_token and is_upstream_connection_error(last_error):
                if recover_connection(token, last_error):
                    continue
                raise ImageGenerationError(
                    image_stream_error_message(last_error),
                    account_email=account_email,
                ) from exc
            state["sticky_token"] = ""
            if not emitted_for_token and is_token_invalid_error(last_error):
                recover_auth(token, last_error)
                continue
            lease.finish(False)
            raise ImageGenerationError(image_stream_error_message(last_error), account_email=account_email, conversation_id="") from exc
        finally:
            if backend is not None:
                backend.close()


def stream_image_outputs_with_pool(request: ConversationRequest) -> Iterator[ImageOutput]:
    """并行生成多张图片，每张图片使用独立线程和账号，互不阻塞。"""
    if not is_supported_image_model(request.model):
        raise ImageGenerationError("unsupported image model,supported models: " + ", ".join(sorted(IMAGE_MODELS)))

    if request.n <= 1:
        # 单张图片，直接执行（无需线程池开销）
        outputs = _generate_single_image(request, 1, 1)
        for output in outputs:
            yield output
        return

    # 多张图片：根据配置选择并行或串行执行
    if not config.image_parallel_generation:
        logger.info({
            "event": "image_serial_generation_start",
            "n": request.n,
            "model": request.model,
        })
        for index in range(1, request.n + 1):
            outputs = _generate_single_image(request, index, request.n)
            for output in outputs:
                yield output
        return

    logger.info({
        "event": "image_parallel_generation_start",
        "n": request.n,
        "model": request.model,
    })
    futures = {}
    errors: dict[int, Exception] = {}
    emitted = False
    last_error = ""
    executor = ThreadPoolExecutor(max_workers=request.n)
    try:
        for index in range(1, request.n + 1):
            future = executor.submit(_generate_single_image, request, index, request.n)
            futures[future] = index

        # Yield as each image finishes so the client sees progress, and so a
        # disconnect can cancel not-yet-started workers instead of waiting
        # for the entire batch.
        for future in as_completed(futures):
            index = futures[future]
            try:
                outputs = future.result()
            except Exception as exc:
                errors[index] = exc
                last_error = str(exc)
                logger.warning({
                    "event": "image_parallel_generation_error",
                    "index": index,
                    "error": str(exc)[:300],
                })
                continue
            for output in outputs:
                emitted = True
                yield output
    except GeneratorExit:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if emitted:
        for index, exc in errors.items():
            logger.warning({
                "event": "image_parallel_partial_failure",
                "failed_index": index,
                "error": str(exc)[:200],
            })
        return

    if errors:
        # Preserve structured quota/policy errors (and timeout conversation IDs).
        # Rewrapping them as generic 502 discards the client's recovery context.
        raise errors[max(errors)]
    if not last_error:
        last_error = "no account in the pool could generate images — check account quota and rate-limit status"
    raise ImageGenerationError(image_stream_error_message(last_error), conversation_id="")


def stream_image_chunks(outputs: Iterable[ImageOutput]) -> Iterator[dict[str, Any]]:
    for output in outputs:
        yield output.to_chunk()


def collect_image_outputs(outputs: Iterable[ImageOutput]) -> dict[str, Any]:
    created = None
    data: list[dict[str, Any]] = []
    message = ""
    progress_parts: list[str] = []
    account_email = ""
    for output in outputs:
        created = created or output.created
        if output.account_email and not account_email:
            account_email = output.account_email
        if output.kind == "progress" and output.text:
            progress_parts.append(output.text)
        elif output.kind == "message":
            message = output.text
        elif output.kind == "result":
            data.extend(output.data)

    result: dict[str, Any] = {"created": created or int(time.time()), "data": data}
    if not data:
        text = message or "".join(progress_parts).strip()
        if text:
            result["message"] = text
    if account_email:
        result["_account_email"] = account_email
    return result
