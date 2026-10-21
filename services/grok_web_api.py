"""grok.com 网页路径客户端 —— 与 CLI/Build 路径（grok_backend_api）隔离。

用途：把 grok.com 网页对话当**搜索**用，以及调用网页**画图**。

与 `grok_backend_api.py` 的区别（两套凭证不通用）：

| | 网页路径（本模块） | CLI 路径（grok_backend_api） |
|---|---|---|
| 端点 | `grok.com/rest/app-chat/*` | `cli-chat-proxy.grok.com/v1/*` |
| 认证 | 会话 Cookie | `Authorization: Bearer <access_token>` |
| 流格式 | **NDJSON**（逐行 JSON） | SSE |
| 搜索 | 内嵌在对话流 | 无 |
| 画图 | Imagine | 生图 |

协议来源：`docs/grok-web-protocol.md`（静态逆向 1367 个前端 chunk）。

**验证状态**：解析逻辑有单测覆盖（`test/test_grok_web_api.py`）；
网络与认证部分**未经实况验证** —— 需要有效的 grok.com 会话 cookie。
"""
from __future__ import annotations

import base64
import json
import re
import time
import uuid
from typing import Any, Iterable, Iterator

import requests

WEB_BASE = "https://grok.com"

# 每 (host, proxy) 一个 Session —— 复用 keep-alive 可绕开 TLS 握手抖动
_SESSIONS: dict[str, requests.Session] = {}
CHAT_NEW_PATH = "/rest/app-chat/conversations/new"
CHAT_ADD_PATH = "/rest/app-chat/conversations/{conversation_id}/responses"
RECONNECT_PATH = "/rest/app-chat/conversations/reconnect-response/{response_id}"

ASSET_SERVER_URL = "https://assets.grok.com"

# 生图会话类型（bundle 原文：ConversationKindImagine:"CONVERSATION_KIND_IMAGINE"）
KIND_IMAGINE = "CONVERSATION_KIND_IMAGINE"

DEFAULT_MODEL = "grok-4-auto"

# 网页生图**必须**用这个模型名。实测 2026-10-19：`grok-4.7` / `grok-4.5` /
# `grok-4.20-*` 一律 403 "Model is not found"；`grok-3` 通过名称校验但返回
# `modelNotAvailable`（表现为 "Invalid request. Please update your client."）；
# 只有 `build` 真正出图。
#
# 它是 `/rest/models` 目录里的 `modelId: "build"`（`modelMode: MODEL_MODE_BUILD`）。
IMAGE_MODEL = "build"

# 生图 Agent 模型（走 MCP 工具 `imagine_text_to_image`，出图但慢，且额度受限）
IMAGE_AGENT_MODEL = "imagine-agent-mode"
IMAGE_AGENT_MODEL_45 = "imagine-agent-mode-grok-4-5"

# 图片 CDN。`imageUrl` 是**相对 key**，拼这个前缀；下载**需要 Bearer**（无认证 403）。
ASSET_SERVER_URL = "https://assets.grok.com"

# toolOverrides 14 键全集（GrokApiToolOverrides）。
# 语义是「只在禁用时下发」—— 全开时整个字段不发。
TOOL_KEYS = (
    "imageGen", "webSearch", "xSearch", "xMediaSearch", "trendsSearch", "xPostAnalyze",
    "videoGen", "audioGen", "gmailSearch", "googleCalendarSearch", "outlookSearch",
    "outlookCalendarSearch", "documentSearch", "googleDriveSearch",
)


class GrokWebError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


# --------------------------------------------------------------------------
# 解析层（纯函数，可单测）
# --------------------------------------------------------------------------

def iter_ndjson(lines: Iterable[bytes | str]) -> Iterator[dict[str, Any]]:
    """按行解析 grok.com 的流。

    **不是 SSE** —— bundle 里 `processChunks` 的实现是按 `\\n` 切分后直接
    `JSON.parse`，没有 `data:` 前缀、没有 `event:` 字段。用 SSE 解析器会把
    JSON 的首字符吃掉。

    与前端一致：解析失败的行**静默跳过**（前端在 catch 里只上报，不中断流）。
    """
    for raw in lines:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
        text = raw.strip()
        if not text:
            continue
        try:
            chunk = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(chunk, dict):
            yield chunk


def unwrap(chunk: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """拆开流式信封，返回 (model_response, error)。

    信封形状（`/conversations/new`）::

        {"result": {"response": {...}, "conversation": {...}, "title": {...}},
         "error": {"code": ..., "message": ..., "details": [...]}}

    注意两个端点的信封不同：`/conversations/{id}/responses` 的 `result`
    直接就是 ModelResponse，不是 `{response, conversation, title}`。
    """
    if not isinstance(chunk, dict):
        return {}, None
    error = chunk.get("error") if isinstance(chunk.get("error"), dict) else None
    result = chunk.get("result")
    if not isinstance(result, dict):
        return {}, error
    response = result.get("response")
    if isinstance(response, dict):
        return response, error
    # add-response 端点：result 本身就是 ModelResponse
    if "responseId" in result or "message" in result:
        return result, error
    return {}, error


def extract_text(response: dict[str, Any]) -> str:
    """取本 chunk 的正文增量。

    **实况修正（2026-10-19）**：流式分块里正文在 **`token`** 字段，
    不是 `message` —— 实测响应：

        {"messageTag":"final","token":"Got"}   ← 逐词增量
        {"messageTag":"final","token":" it"}

    只有**最终帧**的 `modelResponse.message` 才是完整正文。
    早先只读 `message` 会完全收不到流式文本（`ask()` 返回空串）。

    另外 `messageTag` 决定这块文本属于哪个通道：`thinking` 走思考区、
    `tool_usage_card` 走工具卡片、`final`/`UNSET` 才是正文。
    这里只取正文通道，避免把思考内容混进结果。
    """
    tag = str(response.get("messageTag") or "").strip().lower()
    if tag and tag not in ("final", "unset", ""):
        # 最终帧的 modelResponse 没有 messageTag，走下面的分支
        if not isinstance(response.get("modelResponse"), dict):
            return ""
    value = response.get("token")
    if isinstance(value, str) and value:
        return value
    value = response.get("message")
    return value if isinstance(value, str) else ""


def extract_citations(response: dict[str, Any]) -> list[dict[str, Any]]:
    """取「被正文引用」的来源。

    `citedWebSearchResults` 与 `webSearchResults` 同类型
    （`{results: [{url, title, preview}]}` 的客户端模型），语义是
    被引用到的子集。前端在 REST 路径上同时双写两者。
    """
    out: list[dict[str, Any]] = []
    for key in ("citedWebSearchResults", "webSearchResults"):
        value = response.get(key)
        if isinstance(value, dict):
            items = value.get("results")
        elif isinstance(value, list):
            items = value
        else:
            items = None
        if isinstance(items, list):
            out.extend(item for item in items if isinstance(item, dict))
    return out


def extract_search_pages(response: dict[str, Any]) -> list[dict[str, Any]]:
    """取富字段版的搜索结果。

    **路径是 `outputChunks[].toolResult.webSearch.webpages[]`** ——
    `toolResult` 不在 ModelResponse 顶层。这版有 `snippet` / `parsedText` /
    `datePublished` / `siteName`，比 `citedWebSearchResults` 丰富得多。
    """
    out: list[dict[str, Any]] = []
    chunks = response.get("outputChunks")
    if not isinstance(chunks, list):
        return out
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        tool_result = chunk.get("toolResult")
        if not isinstance(tool_result, dict):
            continue
        web_search = tool_result.get("webSearch")
        if not isinstance(web_search, dict):
            continue
        pages = web_search.get("webpages")
        if isinstance(pages, list):
            out.extend(page for page in pages if isinstance(page, dict))
    return out


def extract_images(response: dict[str, Any]) -> list[dict[str, Any]]:
    """取本 chunk 的图片信息。

    **实况修正（2026-10-19）**：网页生图的图片走 `cardAttachment`，**不是**
    `streamingImageGenerationResponse`。真实响应（`build` + IMAGINE）：

        {"cardAttachment": {"jsonData": "{\\"id\\":\\"gi6r5\\",
          \\"type\\":\\"render_generated_image\\",
          \\"cardType\\":\\"generated_image_card\\",
          \\"resolution\\":{\\"width\\":784,\\"height\\":1176,\\"aspect_ratio\\":\\"2:3\\"},
          \\"image_chunk\\":{\\"imageUuid\\":\\"...\\",
            \\"imageUrl\\":\\"users/<uid>/generated/<uuid>/image.jpg\\",
            \\"mimeType\\":\\"image/jpeg\\",\\"seq\\":1,\\"progress\\":100,
            \\"moderated\\":false,\\"imageModel\\":\\"imagine_x_1\\"}}}"}}

    `jsonData` 是**字符串化的 JSON**，要二次解析。
    进度分两帧：`seq=0 progress=50`（`…-part-0/image.jpg`）→
    `seq=1 progress=100`（`…/image.jpg`，去掉 `-part-0`）。

    另外三个通道（`streamingImageGenerationResponse` / `fileAttachmentAssetMetadata` /
    `generatedImageUrls`）在网页生图里**实测为空**，保留作兼容。
    """
    out: list[dict[str, Any]] = []

    # 通道 0：cardAttachment（网页生图的真实通道）
    card = response.get("cardAttachment")
    if isinstance(card, dict):
        raw = card.get("jsonData")
        data: dict[str, Any] | None = None
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    data = parsed
            except (json.JSONDecodeError, ValueError):
                data = None
        elif isinstance(raw, dict):
            data = raw
        if data is not None:
            chunk = data.get("image_chunk")
            if isinstance(chunk, dict):
                resolution = data.get("resolution")
                out.append({
                    "kind": "card",
                    "card_id": data.get("id"),
                    "image_url": asset_url(chunk.get("imageUrl")),
                    "asset_id": chunk.get("imageUuid"),
                    "mime_type": chunk.get("mimeType"),
                    "progress": chunk.get("progress"),
                    "seq": chunk.get("seq"),
                    "image_model": chunk.get("imageModel"),
                    "prompt": chunk.get("imagePrompt") or data.get("prompt"),
                    "resolution": resolution if isinstance(resolution, dict) else None,
                    "moderated": chunk.get("moderated"),
                    "rejection_reason": chunk.get("rejectionReason"),
                    # 生图失败时给 `systemErrCode`（实测 1006 = 被限流），
                    # 此时 `imageUrl` 缺失但 `progress` 仍是 100 ——
                    # 只看 progress 会把失败帧误判成完成。
                    "system_err_code": chunk.get("systemErrCode"),
                })

    frame = response.get("streamingImageGenerationResponse")
    if isinstance(frame, dict):
        out.append({
            "kind": "stream",
            "image_url": asset_url(frame.get("imageUrl")),
            "asset_id": frame.get("assetId"),
            "image_id": frame.get("imageId"),
            "progress": frame.get("progress"),
            "seq": frame.get("seq"),
            "has_watermark": frame.get("hasWatermark"),
            "rejection_reason": frame.get("rejectionReason"),
            "moderation_rule": frame.get("appliedModerationRule"),
        })

    meta = response.get("fileAttachmentAssetMetadata")
    if isinstance(meta, list):
        for item in meta:
            if not isinstance(item, dict):
                continue
            mime = str(item.get("mimeType") or item.get("mime_type") or "")
            if mime and not mime.startswith("image/"):
                continue
            out.append({
                "kind": "asset",
                "image_url": asset_url(
                    item.get("key") or item.get("previewImageKey") or item.get("url")
                ),
                "asset_id": item.get("assetId") or item.get("id"),
                "mime_type": mime or None,
            })

    urls = response.get("generatedImageUrls")
    if isinstance(urls, list):
        for url in urls:
            out.append({"kind": "url", "image_url": asset_url(url)})

    return out


def asset_url(key: Any) -> str | None:
    """把相对 key 拼成绝对 URL。

    前端逻辑（`getAssetUrl`）：已是 `data:` / `blob:` / `http` 的原样返回，
    否则把 key 当作 base 的 pathname。
    """
    if not isinstance(key, str) or not key:
        return None
    if key.startswith(("data:", "blob:", "http://", "https://")):
        return key
    return f"{ASSET_SERVER_URL}/{key.lstrip('/')}"


# 服务端在「模型不可用」时发的兜底文案。实测 2026-10-19：`grok-3` 通过名称
# 校验但模型不可用，流里先给这句，再给真正的 `modelNotAvailable` 错误。
CLIENT_UPDATE_NOTICE = "Invalid request. Please update your client."


def model_unavailable(response: dict[str, Any]) -> str | None:
    """模型不可用时返回原因，否则 None。

    实测：`grok-3` 在网页路径返回 200，会话被创建、用户消息被回显，然后

        {"token":"Invalid request. Please update your client.","messageTag":"final"}
        {"error":{"message":"This model is temporarily unavailable. ...",
                  "modelNotAvailable":{},"severity":"STREAM_ERROR_SEVERITY_NORMAL"}}

    **`"Invalid request. Please update your client."` 是兜底文案**，真实原因是
    `modelNotAvailable`。只看 `token` 字段会误判成「客户端版本太旧」而去改请求头，
    那是一条死路（所有头变体都停在同一个错误）。
    """
    error = response.get("error")
    if isinstance(error, dict):
        if error.get("modelNotAvailable") is not None:
            return str(error.get("message") or "model not available")
        # 只认「模型不可用」类文案，别把限流等其它错误也吞进来 ——
        # `image_rate_limited()` 要能独立识别 `renderToolRateLimited`。
        message = str(error.get("message") or "")
        lowered = message.lower()
        if "model" in lowered and ("not available" in lowered or "unavailable" in lowered):
            return message
    token = response.get("token")
    if isinstance(token, str) and CLIENT_UPDATE_NOTICE in token:
        return CLIENT_UPDATE_NOTICE
    return None


# 生图被拒的**三种形态**（实测 2026-10-19，三个凭证各命中一种）：
#
#   1. 请求层 429 —— 流开始前就被拒
#      HTTP 429  {"error":{"code":8,"message":"Too many requests","details":[]}}
#      整个账号被限流，连对话都发不出
#
#   2. 流内 renderToolRateLimited —— 请求成功但生图被拒
#      {"error":{"message":"You've hit your image rate limit...",
#                "renderToolRateLimited":{"rateLimitedCount":1}}}
#      同时 cardAttachment.image_chunk 给 systemErrCode: 1006 且无 imageUrl
#
#   3. **静默降级** —— 最隐蔽的一种
#      HTTP 200，无 error，无 cardAttachment，模型只回文字
#      （"Got it — test received. How can I help?"）
#      即生图额度耗尽后，服务端不再报错，而是当成普通对话处理。
#      **只看 error 字段会把它误判成成功。**
IMAGE_SILENT_DEGRADE_HINT = (
    "生图请求未产生 cardAttachment —— 可能是额度耗尽后的静默降级"
)


def image_rate_limited(response: dict[str, Any]) -> str | None:
    """生图被限流时返回原因，否则 None。

    实测 2026-10-19：连续第二次生图返回

        {"error": {"message": "You've hit your image rate limit. Please try again later.",
                   "renderToolRateLimited": {"rateLimitedCount": 1},
                   "severity": "STREAM_ERROR_SEVERITY_NORMAL"}}

    同时 `cardAttachment.image_chunk` 给 `systemErrCode: 1006` 且**没有 imageUrl**。
    注意 `progress` 仍是 `100` —— 只看 progress 会把失败帧当成完成。
    """
    error = response.get("error")
    if isinstance(error, dict):
        if error.get("renderToolRateLimited") is not None:
            return str(error.get("message") or "image rate limited")
        message = str(error.get("message") or "")
        if "rate limit" in message.lower() and "image" in message.lower():
            return message
    return None


def is_rejected(response: dict[str, Any]) -> str | None:
    """生图被拒时返回原因，否则 None。

    `streamingImageGenerationResponse` 有 4 个审核相关字段
    （`moderated` / `rejectionReason` / `appliedModerationRule` / `rRated`）。
    只看 `imageUrl` 会把「被拒」误判成「还在生成」。
    """
    frame = response.get("streamingImageGenerationResponse")
    if not isinstance(frame, dict):
        return None
    reason = frame.get("rejectionReason")
    if isinstance(reason, str) and reason:
        return reason
    if frame.get("moderated") is True and not frame.get("imageUrl"):
        return "moderated"
    return None


# --------------------------------------------------------------------------
# 请求构造（纯函数，可单测）
# --------------------------------------------------------------------------

def build_body(
    message: str,
    *,
    model: str = DEFAULT_MODEL,
    search: bool = True,
    image: bool = False,
    image_count: int = 2,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    image_mode: str | None = None,
    temporary: bool = False,
    reasoning: bool = False,
    disable_tools: Iterable[str] | None = None,
    device_env: dict[str, Any] | None = None,
    video: bool = False,
    video_duration: int = 6,
    video_resolution: str = "480p",
    video_skip_audio: bool = False,
    media_gen: dict[str, Any] | None = None,
    image_gen_config: dict[str, Any] | None = None,
    image_edit_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 `/rest/app-chat/conversations/new` 的请求体。

    `search=False` 走 `toolOverrides.webSearch = False` —— **不是** `disableSearch`。
    bundle 里 `disableSearch` 恒为 `false`，从未置 `true`；`buildToolOverrides`
    只在开关关闭时下发 `{webSearch:false}`。

    三条媒体路径（**`media_gen` 优先级最高**，可覆盖前两条）：

    * `image=True` → `modelName: "build"` + `mediaGenInput.textToImage`
      （已证实出图，见 §1.7）
    * `video=True` → `modelName: "imagine-video-gen"` + `textToVideo`
      （已证实 200 返回 video id）
    * `media_gen={...}` → 原样作为 `mediaGenInput`，配合调用方自选的 `model`
      （六种变体见 :data:`MEDIA_GEN_VARIANTS`）
    """
    body: dict[str, Any] = {
        "message": message,
        "modelName": model,
        "disableSearch": False,
        "temporary": temporary,
        "isReasoning": reasoning,
        "modelMode": "none",
        "deviceEnvInfo": device_env or default_device_env(),
    }

    disabled = set(disable_tools or ())
    if not search:
        disabled |= {"webSearch", "xSearch"}
    if disabled:
        unknown = disabled - set(TOOL_KEYS)
        if unknown:
            raise ValueError(f"未知的 toolOverrides 键: {sorted(unknown)}")
        body["toolOverrides"] = {key: False for key in sorted(disabled)}

    if media_gen is not None:
        kind = media_gen_kind(media_gen)
        if kind is None:
            raise ValueError(
                f"mediaGenInput 必须含六种变体之一: {list(MEDIA_GEN_VARIANTS)}")
        body.update({
            "kind": KIND_IMAGINE,
            "enableImageGeneration": True,
            "enableImageStreaming": True,
            "mediaGenInput": media_gen,
        })
        if kind in ("textToImage", "imageToImage") and "responseMetadata" not in body:
            body["responseMetadata"] = {
                "modelConfigOverride": {"modelMap": {"imageEditModel": "imagine"}}
            }
        return body

    if video:
        body["modelName"] = model if model != DEFAULT_MODEL else VIDEO_GEN_MODEL
        body.update({
            "kind": KIND_IMAGINE,
            "enableImageGeneration": True,
            "enableImageStreaming": True,
            "mediaGenInput": build_text_to_video(
                message,
                aspect_ratio=aspect_ratio or "16:9",
                duration=video_duration,
                resolution=video_resolution,
                skip_audio=video_skip_audio,
            ),
        })
        return body

    if image:
        gen: dict[str, Any] = {"prompt": message, "numOfImages": image_count}
        if aspect_ratio:
            gen["aspectRatio"] = aspect_ratio
        if resolution:
            gen["resolutionName"] = resolution
        if image_mode:
            gen["mode"] = image_mode
        body.update({
            "kind": KIND_IMAGINE,
            "enableImageGeneration": True,
            "enableImageStreaming": True,
            "mediaGenInput": {"textToImage": gen},
            "responseMetadata": {
                "modelConfigOverride": {"modelMap": {"imageEditModel": "imagine"}}
            },
        })
        # 直连模型名（`imagine-image-gen` / `imagine-image-edit`）需要各自的
        # 配置块 —— 见 :func:`build_image_gen_config` / :func:`build_image_edit_config`
        if image_gen_config is not None:
            body["imageGenModelConfig"] = dict(image_gen_config)
        if image_edit_config is not None:
            body["imageEditModelConfig"] = dict(image_edit_config)
    return body


def default_device_env() -> dict[str, Any]:
    """`deviceEnvInfo` 的 6 个字段（纯浏览器 API，无 canvas/WebGL 指纹）。"""
    return {
        "darkModeEnabled": False,
        "devicePixelRatio": 1,
        "screenWidth": 1920,
        "screenHeight": 1080,
        "viewportWidth": 1920,
        "viewportHeight": 947,
    }


def build_headers(
    *,
    referer: str = f"{WEB_BASE}/",
    bearer: str | None = None,
) -> dict[str, str]:
    """必需请求头。

    前端显式设置的只有 `Content-Type` 与 `x-xai-request-id`；其余靠浏览器
    自动带（`credentials:"include"` 负责 Cookie）。

    **实况修正（2026-10-19）**：静态逆向得出「网页路径用 Cookie」，但实测
    `Authorization: Bearer <access_token>` **可用** ——
    `GET /rest/app-chat/conversations` → 200；而所有 cookie 名变体
    （`sso` / `sso-rw` / `token` / `access_token` /
    `__Secure-next-auth.session-token`）全部 401。所以 `bearer` 是首选路径。

    常见误判：`x-statsig-id` 在 1367 个 chunk 里**无此字符串**；
    `x-app-version` / `x-device-model` / `x-device-vendor` 只存在于一个
    **无调用点**的遥测方法里；`x-app-language` / `x-app-timezone` 是
    WebSocket query 参数，不是 HTTP 头。
    """
    headers = {
        "Content-Type": "application/json",
        "x-xai-request-id": str(uuid.uuid4()),
        "Accept": "*/*",
        "Origin": WEB_BASE,
        "Referer": referer,
    }
    token = str(bearer or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _auth(cookies: Any = None, bearer: str | None = None) -> tuple[dict[str, str] | None, str | None]:
    """解析认证材料。返回 (cookie jar, bearer)。

    两者都给时优先 Bearer（实测可用），Cookie 作为兜底。
    """
    token = str(bearer or "").strip() or None
    jar = _cookies(cookies)
    return jar, token


# --------------------------------------------------------------------------
# 网络层（需真实 cookie，未验证）
# --------------------------------------------------------------------------

def _cookies(raw: Any) -> dict[str, str] | None:
    """接受 dict 或 `a=b; c=d` 形式的 cookie 串。空结果返回 None。"""
    if isinstance(raw, dict):
        out = {str(k): str(v) for k, v in raw.items() if k and v is not None}
        return out or None
    text = str(raw or "").strip()
    if not text:
        return None
    out = {}
    for part in text.split(";"):
        name, _, value = part.strip().partition("=")
        if name and value:
            out[name] = value
    return out or None


def _session(host: str, proxy: str | None) -> requests.Session:
    """每 (host, proxy) 一个 Session —— 复用 keep-alive 连接绕开 TLS 握手抖动。

    实测：该 SOCKS 池对**新建** TLS 握手有 25-50% 的
    `SSLEOFError: UNEXPECTED_EOF_WHILE_READING` 抖动，而复用 keep-alive
    连接 8/8 成功。所以优先复用而不是每次重连。
    """
    key = f"{host}|{proxy or ''}"
    sess = _SESSIONS.get(key)
    if sess is None:
        sess = requests.Session()
        sess.trust_env = False
        sess.proxies = {"http": proxy, "https": proxy} if proxy else None
        _SESSIONS[key] = sess
    return sess


def _stream_post(
    url: str,
    *,
    headers: dict[str, str],
    cookies: dict[str, str] | None,
    body: dict[str, Any],
    proxy: str | None,
    timeout: float,
    attempts: int = 8,
) -> requests.Response:
    """POST 并返回流式响应，对瞬时 TLS 抖动重试。

    握手阶段 EOF 说明请求从未发出，重试安全（与 `grok_backend_api` 的两级
    分类一致：`is_transient_tls_error`）。这里只做 POST 到对话端点，服务端
    按 `temporary` 去重，重复提交不会重复计费。
    """
    from urllib.parse import urlparse

    from services.proxy_service import is_transient_tls_error

    host = urlparse(url).netloc
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _session(host, proxy).post(
                url, json=body, headers=headers, cookies=cookies,
                stream=True, timeout=timeout,
            )
        except requests.RequestException as exc:
            last = exc
            if attempt < attempts and is_transient_tls_error(str(exc), method="post"):
                _SESSIONS.pop(f"{host}|{proxy or ''}", None)  # 换一条连接
                time.sleep(0.4 * attempt)
                continue
            raise
    raise last  # type: ignore[misc]


def _stream_get(
    url: str,
    *,
    headers: dict[str, str],
    cookies: dict[str, str] | None,
    proxy: str | None,
    timeout: float,
    attempts: int = 8,
) -> requests.Response:
    """GET 流式响应，对瞬时 TLS 抖动重试（GET 幂等，重试安全）。"""
    from urllib.parse import urlparse

    from services.proxy_service import is_transient_tls_error

    host = urlparse(url).netloc
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _session(host, proxy).get(
                url, headers=headers, cookies=cookies, stream=True, timeout=timeout,
            )
        except requests.RequestException as exc:
            last = exc
            if attempt < attempts and is_transient_tls_error(str(exc), method="get"):
                _SESSIONS.pop(f"{host}|{proxy or ''}", None)
                time.sleep(0.4 * attempt)
                continue
            raise
    raise last  # type: ignore[misc]


def stream_conversation(
    message: str,
    *,
    cookies: Any = None,
    bearer: str | None = None,
    proxy: str | None = None,
    conversation_id: str | None = None,
    timeout: float = 300.0,
    **body_kwargs: Any,
) -> Iterator[dict[str, Any]]:
    """发一条消息并逐块产出解析后的 ModelResponse。

    `conversation_id` 为 None 时走 `/conversations/new`（新会话），
    否则走 `/conversations/{id}/responses`（续话）。两个端点的**信封不同**，
    :func:`unwrap` 都处理。

    认证优先 Bearer（实测可用），Cookie 兜底。

    **已知阻断（2026-10-19 实测）**：OAuth2 token 用户的网页路径**没有可用
    的对话模型** —— 唯一通过名称校验的 `grok-3` 返回 `modelNotAvailable`，
    表现为 `{"token":"Invalid request. Please update your client."}`。
    本函数仍会正常返回该响应，调用方需自行判断。
    """
    jar, token = _auth(cookies, bearer)
    if not jar and not token:
        raise GrokWebError("缺少认证：需要 bearer 或 cookies")

    body = build_body(message, **body_kwargs)
    if conversation_id:
        url = f"{WEB_BASE}{CHAT_ADD_PATH.format(conversation_id=conversation_id)}"
    else:
        url = f"{WEB_BASE}{CHAT_NEW_PATH}"

    try:
        resp = _stream_post(
            url,
            headers=build_headers(
                referer=f"{WEB_BASE}/c/{conversation_id}" if conversation_id else f"{WEB_BASE}/",
                bearer=token,
            ),
            cookies=jar,
            body=body,
            proxy=proxy,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise GrokWebError(f"请求失败: {exc}") from exc

    with resp:
        if resp.status_code == 429:
            raise GrokWebError("上游限流 (429)", status=429)
        if resp.status_code == 403:
            # 前端触发条件是 403 + cf-mitigated: challenge，需走 turnstile 后重放
            mitigated = resp.headers.get("cf-mitigated", "")
            raise GrokWebError(
                f"403 被拦截 (cf-mitigated={mitigated or '无'})", status=403
            )
        if resp.status_code >= 400:
            raise GrokWebError(
                f"HTTP {resp.status_code}: {(resp.text or '')[:300]}",
                status=resp.status_code,
            )
        for chunk in iter_ndjson(resp.iter_lines()):
            response, error = unwrap(chunk)
            if error and not response:
                raise GrokWebError(
                    f"流内错误: {error.get('message') or error}", body=error
                )
            if response:
                yield response


def ask(
    message: str,
    *,
    cookies: Any = None,
    bearer: str | None = None,
    proxy: str | None = None,
    timeout: float = 300.0,
    **body_kwargs: Any,
) -> dict[str, Any]:
    """一次性调用，聚合全部增量。

    返回 `{"text", "citations", "pages", "images", "rejected", "response_id",
    "conversation_id"}`。
    """
    text_parts: list[str] = []
    citations: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    rejected: list[str] = []
    rate_limited: list[str] = []
    response_id = None
    conversation_id = None

    for response in stream_conversation(
        message, cookies=cookies, bearer=bearer, proxy=proxy,
        timeout=timeout, **body_kwargs
    ):
        text_parts.append(extract_text(response))
        citations.extend(extract_citations(response))
        pages.extend(extract_search_pages(response))
        images.extend(extract_images(response))
        reason = is_rejected(response)
        if reason:
            rejected.append(reason)
        limited = image_rate_limited(response)
        if limited:
            rate_limited.append(limited)
        response_id = response.get("responseId") or response_id
        conversation_id = response.get("conversationId") or conversation_id

    return {
        "text": "".join(text_parts),
        "citations": _dedupe(citations, "url"),
        "pages": _dedupe(pages, "url"),
        "images": _dedupe(images, "image_url"),
        "rejected": rejected,
        "rate_limited": rate_limited,
        "response_id": response_id,
        "conversation_id": conversation_id,
    }


def _dedupe(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        value = item.get(key)
        if value in seen:
            continue
        seen.add(value)
        out.append(item)
    return out


def search(query: str, *, cookies: Any = None, bearer: str | None = None,
           proxy: str | None = None, timeout: float = 300.0) -> dict[str, Any]:
    """把网页对话当搜索用：开搜索、关其余工具。

    `sources` 与 `citations` 语义不同，**都返回**：

    * `sources` —— 检索到的网页（`outputChunks[].toolResult.webSearch.webpages`，
      富字段版：有 `snippet` / `parsedText` / `datePublished` / `siteName`）。
      没有富字段时回退到 `citations`。
    * `citations` —— 被正文**实际引用**的子集（`citedWebSearchResults`）。
    """
    result = ask(query, cookies=cookies, bearer=bearer, proxy=proxy,
                 timeout=timeout, search=True)
    return {
        "answer": result["text"],
        "sources": result["pages"] or result["citations"],
        "citations": result["citations"],
        "response_id": result["response_id"],
        "conversation_id": result["conversation_id"],
    }


# --------------------------------------------------------------------------
# 额度 / 限流
# --------------------------------------------------------------------------

# 生图额度的**真实端点**是 `POST /rest/media/imagine/quota_info`（body `{}`），
# 返回 5 个类别，每类 23 字段（bundle 提取，见 docs/grok-web-protocol.md §1.7）：
#
#   {estimate: {remainingImageGenFast1k, remainingImageGenFast2k,
#               remainingImageGenQuality1k, remainingImageGenQuality2k,
#               remainingImageEditQuality1k, remainingImageEditQuality2k,
#               remainingT2v{480,720,1080}pSeconds,
#               remainingI2v{480,720,1080}pSeconds,
#               remainingR2v{480,720,1080}pSeconds,
#               remainingExtension{480,720,1080}pSeconds},
#    shouldShow, imagineFreeCreditActive,
#    imagineFreeCreditAllowance, imagineFreeCreditRemaining}
#
# **实测该端点对 OAuth2 token 用户返回 403**
# （`WKE=unauthorized:oauth2-auth-forbidden`），但生图本身可用 ——
# 即额度是服务端强制、OAuth2 用户不可查询。可查的替代见
# :func:`free_usage_gates` 与 :func:`cli_rate_limits`。
QUOTA_INFO_PATH = "/rest/media/imagine/quota_info"
FREE_USAGE_GATES_PATH = "/rest/usage/free-usage-gates"

# CLI 路径（cli-chat-proxy）返回的真实限流头。
CLI_BASE = "https://cli-chat-proxy.grok.com/v1"
CLI_USER_AGENT = "grok-cli/1.0.62"
CLI_RATELIMIT_HEADERS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
)

# 生图额度类别（quota_info 的顶层键）
QUOTA_CATEGORIES = ("image", "imagePro", "imageEdit", "video", "video720p")

# 单个类别的 estimate 字段全集
QUOTA_ESTIMATE_FIELDS = (
    "remainingImageGenFast1k", "remainingImageGenFast2k",
    "remainingImageGenQuality1k", "remainingImageGenQuality2k",
    "remainingImageEditQuality1k", "remainingImageEditQuality2k",
    "remainingT2v480pSeconds", "remainingT2v720pSeconds", "remainingT2v1080pSeconds",
    "remainingI2v480pSeconds", "remainingI2v720pSeconds", "remainingI2v1080pSeconds",
    "remainingR2v480pSeconds", "remainingR2v720pSeconds", "remainingR2v1080pSeconds",
    "remainingExtension480pSeconds", "remainingExtension720pSeconds",
    "remainingExtension1080pSeconds",
)


def _as_int(value: Any) -> int:
    """把额度字段转成 int —— 服务端会给**字符串**（`"0"`）与数字两种形态。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def free_usage_gates(
    *,
    bearer: str | None = None,
    cookies: Any = None,
    proxy: str | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """`GET /rest/usage/free-usage-gates` —— 免费层用量闸门。

    返回 `{"chat": {...}, "imagine": {...}, "voice": {...}, "build": {...}}`，
    每个是 `{allowance, remaining}`（客户端语义，见 bundle 的
    `function o(e){return {allowance:n(e.allowance), remaining:n(e.remaining)}}`）。

    **实测（2026-10-19）**：三个 OAuth2 凭证全部返回 `0/0`，**但生图可用** ——
    说明这个闸门只对免费网页用户生效，OAuth2 token 用户走另一条额度路径。
    不要用它判断能否生图。
    """
    jar, token = _auth(cookies, bearer)
    if not jar and not token:
        raise GrokWebError("缺少认证：需要 bearer 或 cookies")
    resp = _stream_get(
        f"{WEB_BASE}{FREE_USAGE_GATES_PATH}",
        headers=build_headers(bearer=token),
        cookies=jar,
        proxy=proxy,
        timeout=timeout,
    )
    with resp:
        if resp.status_code != 200:
            raise GrokWebError(f"用量闸门 HTTP {resp.status_code}", status=resp.status_code)
        data = resp.json()
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for gate in ("chat", "imagine", "voice", "build"):
        value = data.get(gate)
        if isinstance(value, dict):
            out[gate] = {
                "allowance": _as_int(value.get("allowance")),
                "remaining": _as_int(value.get("remaining")),
            }
        else:
            out[gate] = {"allowance": 0, "remaining": 0}
    return out


def imagine_quota(
    *,
    bearer: str | None = None,
    cookies: Any = None,
    proxy: str | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """`POST /rest/media/imagine/quota_info` —— 生图额度（权威端点）。

    返回 5 个类别（见 :data:`QUOTA_CATEGORIES`），每类含
    `estimate`（19 个 `remaining*` 字段）、`shouldShow`、
    `imagineFreeCreditActive/Allowance/Remaining`，顶层另有
    `maxVideoDurationSeconds`。

    **实测对 OAuth2 token 用户返回 403**（`oauth2-auth-forbidden`）。
    这不是代码问题 —— 服务端策略把该端点限定给网页会话。
    调用方应捕获 :class:`GrokWebError` 并回退到
    :func:`free_usage_gates` 或 :func:`cli_rate_limits`。
    """
    jar, token = _auth(cookies, bearer)
    if not jar and not token:
        raise GrokWebError("缺少认证：需要 bearer 或 cookies")
    try:
        resp = _stream_post(
            f"{WEB_BASE}{QUOTA_INFO_PATH}",
            headers=build_headers(bearer=token, referer=f"{WEB_BASE}/imagine"),
            cookies=jar,
            body={},
            proxy=proxy,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise GrokWebError(f"额度查询失败: {exc}") from exc
    with resp:
        if resp.status_code == 403:
            raise GrokWebError(
                "生图额度端点对 OAuth2 token 用户不可用（oauth2-auth-forbidden）",
                status=403,
                body=(resp.text or "")[:300],
            )
        if resp.status_code != 200:
            raise GrokWebError(f"额度查询 HTTP {resp.status_code}",
                               status=resp.status_code, body=(resp.text or "")[:300])
        data = resp.json()
    return data if isinstance(data, dict) else {}


def cli_rate_limits(
    bearer: str,
    *,
    proxy: str | None = None,
    timeout: float = 60.0,
    probe: bool = True,
    probe_model: str = "grok-4.7",
) -> dict[str, Any]:
    """CLI 路径（`cli-chat-proxy`）的真实限流额度，从响应头读。

    **这是 OAuth2 用户唯一可用的额度接口。** 实测（2026-10-19）：

        x-ratelimit-limit-requests: 21
        x-ratelimit-limit-tokens: 1000000
        x-ratelimit-remaining-requests: 21
        x-ratelimit-remaining-tokens: 1000000

    窗口内会递减（实测连发时出现过 `16/21`），窗口重置后回到满值。

    **只有 `POST /responses` 带这些头** —— `/models` 不带（实测确认）。
    所以探测会真的发一次最小对话（`max_output_tokens` 压到最小）。
    `probe=False` 时只读 `/models`（不消耗额度，但通常拿不到头）。
    """
    token = str(bearer or "").strip()
    if not token:
        raise GrokWebError("缺少 bearer")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": CLI_USER_AGENT,
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-grok-client-version": CONTROL_CLIENT_VERSION,
        "x-grok-client-identifier": "grok-shell",
        "Accept": "application/json",
    }
    if probe:
        resp = _stream_post(
            f"{CLI_BASE}/responses",
            headers=headers,
            cookies=None,
            body={"model": probe_model, "input": [{"role": "user", "content": "hi"}],
                  "max_output_tokens": 1},
            proxy=proxy,
            timeout=timeout,
        )
    else:
        resp = _stream_get(f"{CLI_BASE}/models", headers=headers, cookies=None,
                           proxy=proxy, timeout=timeout)
    with resp:
        if resp.status_code != 200:
            raise GrokWebError(f"限流探测 HTTP {resp.status_code}", status=resp.status_code)
        raw_headers = resp.headers
    out: dict[str, Any] = {}
    for name in CLI_RATELIMIT_HEADERS:
        raw = raw_headers.get(name)
        out[name.replace("x-ratelimit-", "")] = _as_int(raw) if raw is not None else None
    return out


# --------------------------------------------------------------------------
# CLI 控制面（套餐 / 额度 / 策略）
# --------------------------------------------------------------------------
#
# 来源：codex2api 的 `grok-build-main/docs/GROK_BUILD_INFERENCE_API_REVERSE.md`
# §9「账号控制面」，本仓库 2026-10-19 实测全部可用。
#
# 公共请求头（各端点有差异，见各函数）：
#   Authorization: Bearer <oauth-access-token>
#   X-XAI-Token-Auth: xai-grok-cli
#   x-grok-client-version: 1.0.1
#   x-grok-client-mode: headless
#   x-userid: <user-id>          （billing/auto-topup 用，/user 不用）
#
# **关键实测结论（2026-10-19）**：网页路径生图**不改变** billing ——
# `creditUsagePercent` 生图前后都是 1.0，`productUsage` 始终只有 `GrokBuild`，
# `GrokImagine` 从未出现。即**生图额度是独立账本，billing 看不到**。
CONTROL_BASE = CLI_BASE  # cli-chat-proxy.grok.com/v1

# 控制面版本头。上游 `GET /settings` 返回 `min_client_version`，
# 低于它生成请求会被 426 拒绝。
#
# **实测 2026-10-19：上游要求 `1.0.13`，而早先硬编码的是 `1.0.1`。**
# 这很可能就是 `CLIENT_UPDATE_NOTICE`（"Invalid request. Please update your
# client."）的根源 —— 用 :func:`client_settings` 读到的值覆盖它。
CONTROL_CLIENT_VERSION = "1.0.13"
CONTROL_CLIENT_MODE = "headless"

# 免费层额度耗尽（模型级，滚动 24 小时窗口）。错误体形如：
#   {"code":"subscription:free-usage-exhausted",
#    "error":"You've used all the included free usage for model
#             grok-4.5-build-free for now. Usage resets over a rolling
#             24-hour window — tokens (actual/limit): 1003617/1000000."}
FREE_QUOTA_MARKERS = ("used all the included free usage",
                      "subscription:free-usage-exhausted")

# 超支 / 余额耗尽（账号级）
SPENDING_LIMIT_MARKERS = ("spending-limit", "spending limit",
                          "usage balance exhausted", "balance_exhausted",
                          "balance-exhausted", "credit balance is too low",
                          "ran out of credits")

# 上游错误 → 冷却时长（对齐 codex2api `applyGrokCooldown` 的映射）：
#   免费额度耗尽       → free 账号整号 24h / 付费账号模型级 24h
#   超支（402/429）    → 账号 24h
#   裸 402             → 不硬隔离（证据不足）
#   429                → Retry-After 或 1 分钟，**上限 15 分钟**
#   401                → 1 分钟 + 异步刷新
#   403                → 5 分钟（绝不刷新 token）
#   426                → 1 分钟（version_required）
COOLDOWN_BY_STATUS = {
    401: 60.0,
    402: 0.0,        # 裸 402 不硬隔离
    403: 300.0,
    426: 60.0,
}
RATE_LIMIT_DEFAULT_COOLDOWN = 60.0
RATE_LIMIT_MAX_COOLDOWN = 900.0      # 429 冷却上限 15 分钟
FREE_QUOTA_COOLDOWN = 86400.0        # 24 小时
SPENDING_LIMIT_COOLDOWN = 86400.0


def _control_headers(token: str, *, user_id: str | None = None,
                     email: str | None = None,
                     identifier: str | None = None) -> dict[str, str]:
    """控制面公共头。各端点差异用参数控制，不要无条件全加。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-grok-client-version": CONTROL_CLIENT_VERSION,
        "x-grok-client-mode": CONTROL_CLIENT_MODE,
        "Accept": "application/json",
    }
    if user_id:
        headers["x-userid"] = user_id
    if email:
        headers["x-email"] = email
    if identifier:
        headers["x-grok-client-identifier"] = identifier
    return headers


def _control_get(path: str, token: str, *, proxy: str | None,
                 timeout: float, **header_kw: Any) -> requests.Response:
    resp = _stream_get(
        f"{CONTROL_BASE}{path}",
        headers=_control_headers(token, **header_kw),
        cookies=None,
        proxy=proxy,
        timeout=timeout,
    )
    return resp


def billing(
    bearer: str,
    *,
    proxy: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """`GET /billing?format=credits` —— 套餐额度与账期（**权威端点**）。

    实测响应（2026-10-19，三个凭证）：

        {"config": {
           "creditUsagePercent": 1.0,          // ← 缺失表示字段未返回，**不是 0%**
           "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                             "start": "2026-10-19T05:23:41.035+00:00",
                             "end":   "2026-10-26T05:23:41.035+00:00"},
           "prepaidBalance": {"val": 0},
           "isUnifiedBillingUser": true,
           "productUsage": [{"product": "GrokBuild", "usagePercent": 1.0}],
           "topUpMethod": null,
           "billingPeriodStart": "...", "billingPeriodEnd": "..."}}

    **重要语义**：

    * `creditUsagePercent` **缺失 ≠ 0%** —— 要保留「字段缺失」这一事实，
      不要当成明确 0%（codex2api 文档 §9.5 明确要求 `usage_pct_raw=null`）。
    * 周期是 **WEEKLY（7 天）**，`currentPeriod.end` 是 reset 时间。
    * `productUsage` 的产品字符串是**开放枚举**，实测见过 `GrokBuild`、
      `GrokChat`、`GrokImagine` —— 不是模型 ID。
    * **网页生图不计入这里**（实测：生图前后 `creditUsagePercent` 与
      `productUsage` 完全不变）。生图额度是独立账本。
    * `{}` 要解析成 `val=0`（proto3 把零值 cents 编码成空对象）。
    """
    token = str(bearer or "").strip()
    if not token:
        raise GrokWebError("缺少 bearer")
    resp = _control_get("/billing?format=credits", token, proxy=proxy,
                        timeout=timeout)
    with resp:
        if resp.status_code != 200:
            raise GrokWebError(f"billing HTTP {resp.status_code}",
                               status=resp.status_code, body=(resp.text or "")[:300])
        data = resp.json()
    if not isinstance(data, dict):
        return {}
    return data.get("config") if isinstance(data.get("config"), dict) else data


def subscription(
    bearer: str,
    *,
    proxy: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """`GET /user?include=subscription` —— 实时套餐。

    **这个端点不发送 `x-userid`**（与 billing 的差异）。

    返回 `{"userId": "...", "subscriptionTier": "GrokPro"}`。
    `subscriptionTier` 可选；判定规则是「非空且不等于 `Free`」才算付费。

    不要把 `GrokPro` / `SuperGrok` / `SuperGrokPro` 过早压成固定枚举 ——
    实测出现过 `/user = GrokPro` 而 `/settings.subscription_tier_display = SuperGrok`
    的不一致。缺失/`null`/空串都应是 `unknown`，**不要粗暴写成 `Free`**。
    """
    token = str(bearer or "").strip()
    if not token:
        raise GrokWebError("缺少 bearer")
    resp = _control_get("/user?include=subscription", token, proxy=proxy,
                        timeout=timeout)
    with resp:
        if resp.status_code != 200:
            raise GrokWebError(f"user HTTP {resp.status_code}",
                               status=resp.status_code, body=(resp.text or "")[:300])
        data = resp.json()
    return data if isinstance(data, dict) else {}


def client_settings(
    bearer: str,
    *,
    proxy: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """`GET /settings` —— 远程策略与最低客户端版本。

    实测（2026-10-19）：`min_client_version` 已是 **`1.0.13`**，
    `force_update: true`。**低于它的生成请求会被 426 拒绝** ——
    所以客户端版本要能跟着上游走，不要写死。

    另含 `allow_access`（Grok Build 门控）、`max_upload_file_bytes`、
    `memory_v2` 等。
    """
    token = str(bearer or "").strip()
    if not token:
        raise GrokWebError("缺少 bearer")
    resp = _control_get("/settings", token, proxy=proxy, timeout=timeout)
    with resp:
        if resp.status_code != 200:
            raise GrokWebError(f"settings HTTP {resp.status_code}",
                               status=resp.status_code, body=(resp.text or "")[:300])
        data = resp.json()
    return data if isinstance(data, dict) else {}


def auto_topup_rule(
    bearer: str,
    *,
    proxy: str | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """`GET /auto-topup-rule` —— 自动充值规则。

    实测三个凭证都返回 `{}`（无规则）。`rule` 缺失表示没有规则；
    proto3 会省略 `enabled: false`，所以缺失按 false；各 cents `{}` 按 0。
    """
    token = str(bearer or "").strip()
    if not token:
        raise GrokWebError("缺少 bearer")
    resp = _control_get("/auto-topup-rule", token, proxy=proxy, timeout=timeout)
    with resp:
        if resp.status_code != 200:
            raise GrokWebError(f"auto-topup HTTP {resp.status_code}",
                               status=resp.status_code, body=(resp.text or "")[:300])
        data = resp.json()
    return data if isinstance(data, dict) else {}


def classify_upstream_error(
    status: int,
    body: str = "",
) -> dict[str, Any]:
    """把上游错误映射成冷却建议（对齐 codex2api `applyGrokCooldown`）。

    返回 `{"kind": ..., "cooldown_seconds": ..., "retryable": ...}`。

    kind 取值：

    * `free_quota_exhausted` —— 免费额度耗尽（滚动 24h 窗口），冷却 24h
    * `spending_limit` —— 超支/余额耗尽，冷却 24h
    * `payment_required_unknown` —— 裸 402，**证据不足，不硬隔离**
    * `rate_limited` —— 429，Retry-After 或 60s，**上限 900s**
    * `unauthorized` —— 401，60s + 刷新 token
    * `forbidden` —— 403，300s，**绝不刷新 token**
    * `version_required` —— 426，60s
    * `ok` / `unknown`

    判定顺序很重要：**先看错误体**（免费额度 / 超支），再看状态码 ——
    因为免费额度耗尽可能以 429 或 402 出现。
    """
    lowered = (body or "").lower()
    if any(m in lowered for m in FREE_QUOTA_MARKERS):
        return {"kind": "free_quota_exhausted",
                "cooldown_seconds": FREE_QUOTA_COOLDOWN,
                "retryable": False}
    if any(m in lowered for m in SPENDING_LIMIT_MARKERS):
        return {"kind": "spending_limit",
                "cooldown_seconds": SPENDING_LIMIT_COOLDOWN,
                "retryable": False}
    if status == 402:
        return {"kind": "payment_required_unknown",
                "cooldown_seconds": 0.0, "retryable": False}
    if status == 429:
        return {"kind": "rate_limited",
                "cooldown_seconds": RATE_LIMIT_DEFAULT_COOLDOWN,
                "retryable": True}
    if status == 401:
        return {"kind": "unauthorized", "cooldown_seconds": 60.0,
                "retryable": False}
    if status == 403:
        return {"kind": "forbidden", "cooldown_seconds": 300.0,
                "retryable": False}
    if status == 426:
        return {"kind": "version_required", "cooldown_seconds": 60.0,
                "retryable": False}
    if status == 200:
        return {"kind": "ok", "cooldown_seconds": 0.0, "retryable": False}
    if 500 <= status < 600:
        return {"kind": "upstream_error", "cooldown_seconds": 0.0,
                "retryable": True}
    return {"kind": "unknown", "cooldown_seconds": 0.0, "retryable": False}


# 版本门控：CLI 路径对过低的 `x-grok-client-version` 直接 426。
# 实测（2026-10-19）：
#   version=1.0.1  → 426 {"error":"Your Grok CLI version (1.0.1) is outdated.
#                          Please update to version 1.0.13 or later ..."}
#   version=1.0.13 → 200
#   version=1.0.62 → 200
# **控制面端点不做版本门控**（1.0.1 也能拿 billing），只有生成端点做。
VERSION_OUTDATED_MARKER = "is outdated"


def version_outdated(response_or_body: Any) -> str | None:
    """识别版本门控（HTTP 426），返回上游的原文提示。

    上游会同时给出**要求的最低版本**，形如
    `Please update to version 1.0.13 or later` —— 调用方应据此更新
    :data:`CONTROL_CLIENT_VERSION` 而不是硬编码。
    """
    if isinstance(response_or_body, dict):
        err = response_or_body.get("error")
        text = err if isinstance(err, str) else str(err or "")
    else:
        text = str(response_or_body or "")
    if VERSION_OUTDATED_MARKER not in text.lower():
        return None
    return text.strip() or None


def required_client_version(text: str) -> str | None:
    """从 426 提示里提取要求的最低版本号。"""
    match = re.search(r"version\s+(\d+\.\d+\.\d+)\s+or later", text or "")
    return match.group(1) if match else None


def clamp_rate_limit_cooldown(seconds: float) -> float:
    """429 冷却钳制 —— 有 Retry-After 用它，但**上限 15 分钟**。

    实测依据：三个凭证被 429 后约 15 分钟内恢复。
    """
    if seconds <= 0:
        return RATE_LIMIT_DEFAULT_COOLDOWN
    return min(float(seconds), RATE_LIMIT_MAX_COOLDOWN)


# --------------------------------------------------------------------------
# 网页额度端点（protobuf 逆向）
# --------------------------------------------------------------------------
#
# 从 bundle 模块 `9096207` 的 `fileDesc` base64 解码出的 **完整 protobuf 定义**
# （`prod/grok/backend/proto/grok_build_billing.proto` + `billing_product`）。
#
# 服务 `GrokBuildBilling` 的全部 RPC（含 HTTP 注解）：
#
#   GetGrokBuildBillingConfig          GET  /rest/grok-build/billing
#   SetGrokBuildOnDemandConfig         POST /rest/grok-build/billing/on-demand
#   GetGrokCreditsConfig               GET  /rest/grok/credits          ← 网页额度
#   ListPaymentMethods                 GET  /rest/grok/payment-methods
#   CheckPaymentSetupFinalized         GET  /rest/grok/payment-setup-finalized
#   TopUpOrGetExistingPendingChange    POST /rest/grok/top-up
#   TopUpUsingInlinePaymentMethod      POST /rest/grok/top-up-inline
#   SetDefaultPaymentMethod            POST /rest/grok/set-default-payment-method
#   ListPrepaidBalanceChanges          GET  /rest/grok/prepaid-balance-changes
#   GetAutoTopupRule                   GET  /rest/grok/auto-topup-rule
#   SetAutoTopupRule                   POST /rest/grok/auto-topup-rule
#   GetAutoTopupStats                  GET  /rest/grok/auto-topup-stats
#   GetInvoice                         GET  /rest/grok/invoice
#   ListInvoices                       GET  /rest/grok/invoices
CREDITS_PATH = "/rest/grok/credits"
BUILD_BILLING_PATH = "/rest/grok-build/billing"

# `billing_product.Product` 枚举 —— **`PRODUCT_GROK_IMAGINE = 5` 是生图独立产品位**。
PRODUCT_ENUM = {
    0: "PRODUCT_UNSPECIFIED",
    1: "PRODUCT_API",
    2: "PRODUCT_GROK_BUILD",
    3: "PRODUCT_GROK_PLUGINS",
    4: "PRODUCT_GROK_CHAT",
    5: "PRODUCT_GROK_IMAGINE",
    6: "PRODUCT_GROK_VOICE",
    7: "PRODUCT_GROK_APP_BUILDER",
    8: "PRODUCT_GROK_TASKS",
    9: "PRODUCT_GROK_BOT",
    10: "PRODUCT_CURSOR",
    11: "PRODUCT_BUGBOT",
}
PRODUCT_GROK_IMAGINE = 5

# `UsagePeriodType` —— 实测三个凭证都是 WEEKLY。
USAGE_PERIOD_TYPES = {
    0: "USAGE_PERIOD_TYPE_UNSPECIFIED",
    1: "USAGE_PERIOD_TYPE_MONTHLY",
    2: "USAGE_PERIOD_TYPE_WEEKLY",
}

# `GrokCreditsConfig` 字段（proto 编号）：
#   credit_usage_percent=1  on_demand_cap=2  on_demand_used=3
#   billing_period_start=4  billing_period_end=5  history=6
#   product_usage=7  current_period=8  is_unified_billing_user=11
#   prepaid_balance=12  top_up_method=13
CREDITS_CONFIG_FIELDS = (
    "credit_usage_percent", "on_demand_cap", "on_demand_used",
    "billing_period_start", "billing_period_end", "history",
    "product_usage", "current_period", "is_unified_billing_user",
    "prepaid_balance", "top_up_method",
)

# 前端 `useChatUsageLimit` 的阈值与轮询策略（bundle 提取）：
#   let f = [90, 75, 50, 25];                      // 提示阈值
#   function crossedThreshold(e,t){ return f.find(n => e<n && t>=n) ?? null }
#   refetchInterval = pct>=90 ? 30000 : pct>=50 ? 60000 : 180000
#   if (config.isUnifiedBillingUser === false) -> 整条额度链关闭
USAGE_TOAST_THRESHOLDS = (90, 75, 50, 25)
USAGE_POLL_NEAR_MS = 30_000      # ≥90%
USAGE_POLL_MID_MS = 60_000       # ≥50%
USAGE_POLL_FAR_MS = 180_000      # <50%


# 额度端点的上游瞬时故障标记 —— 实测 `503 code:14 "cursor billing: snapshot failed"`。
CREDITS_TRANSIENT_MARKERS = ("cursor billing", "snapshot failed")


def web_credits(
    *,
    bearer: str | None = None,
    cookies: Any = None,
    proxy: str | None = None,
    timeout: float = 60.0,
    retries: int = 3,
) -> dict[str, Any]:
    """`GET /rest/grok/credits` —— **网页 UI 真正使用的额度端点**。

    这是 `useChatUsageLimit` → `getGrokCreditsConfig` 背后的 RPC
    （proto `GetGrokCreditsConfig`）。返回 `GrokCreditsConfig`，
    其中 `product_usage[]` 按 `billing_product.Product` 分项，
    **含 `PRODUCT_GROK_IMAGINE = 5`（生图独立产品位）**。

    **实测 2026-10-19：CLI 签发的 OAuth token 被拒**：

        Bearer          → 403 code:7  "The OAuth2 access token could not
                                       be validated. [WKE=unauthenticated:bad-credentials]"
        x-api-key       → 400 code:3  "Cannot authenticate with API key."
        Cookie sso      → 401 code:16 "Bad credentials."
        无认证           → 401 code:16 "No credentials presented."

    **但实测（2026-10-20）这条不是绝对的 —— 取决于账号**：

    * `Cookie: sso=<token>`（网页会话）→ **200**，稳定可用
    * `Authorization: Bearer <CLI oauth-at>` → **4 个凭证里 1 个成功、3 个 403**
      （`e578i2gj1l` / team `fa960e03` ✅；其余三个 team ❌ `bad-credentials`）

    四者的 JWT `aud` / `client_id` **完全相同**（都是 `b1a00492-...`），
    只有 `team_id` 不同 —— 所以拒绝**不是 token 形态问题，是团队/账号级策略**。
    调用方应把 403 当作「该账号读不到」而不是「端点不可用」。

    **生图是否计入这个账本：不计入**（实测 2026-10-20，8 张图后
    `creditUsagePercent` 恒 0、`productUsage` 恒 `[]`）。所以即便能读，
    它反映的也是文本额度，不是生图配额。生图配额只能靠实际请求的
    三种拒绝形态判断（见 :func:`generate_image` 的 `silent_degrade`）。

    `503 code:14 "cursor billing: snapshot failed"` 是上游瞬时故障 ——
    `retries` 控制重试次数（默认 3）。
    """
    jar, token = _auth(cookies, bearer)
    if not jar and not token:
        raise GrokWebError("缺少认证：需要 bearer 或 cookies")
    last: GrokWebError | None = None
    for attempt in range(1, max(1, retries) + 1):
        resp = _stream_get(
            f"{WEB_BASE}{CREDITS_PATH}",
            headers=build_headers(bearer=token),
            cookies=jar,
            proxy=proxy,
            timeout=timeout,
        )
        with resp:
            if resp.status_code == 200:
                data = resp.json()
                if not isinstance(data, dict):
                    return {}
                return (data.get("config") if isinstance(data.get("config"), dict)
                        else data)
            detail = (resp.text or "")[:300]
            error = GrokWebError(
                f"网页额度端点拒绝（{resp.status_code}）：{detail}",
                status=resp.status_code,
                body=detail,
            )
            # 503 "cursor billing: snapshot failed" 是上游瞬时故障，可重试
            transient = resp.status_code >= 500 or any(
                m in detail.lower() for m in CREDITS_TRANSIENT_MARKERS)
            last = error
            if not transient or attempt >= retries:
                raise error
        time.sleep(min(2.0 * attempt, 6.0))
    raise last or GrokWebError("网页额度端点无响应")


def product_name(value: Any) -> str | None:
    """把 `product_usage[].product` 归一成可读名。

    proto3 JSON 可能给**枚举字符串**（`"PRODUCT_GROK_IMAGINE"`）或**数字**（`5`）。
    """
    if isinstance(value, str):
        return value if value.startswith("PRODUCT_") else None
    if isinstance(value, int):
        return PRODUCT_ENUM.get(value)
    return None


def usage_poll_interval_ms(percent: float | None) -> int | None:
    """按前端策略给出下次轮询间隔（毫秒）。

    `percent` 为 `None`（字段缺失）或 `isUnifiedBillingUser === false` 时返回
    `None` —— **不要轮询**。缺失 ≠ 0%，所以不能当成「用量 0%」去走最慢档。
    """
    if percent is None:
        return None
    try:
        value = float(percent)
    except (TypeError, ValueError):
        return None
    if value >= 90:
        return USAGE_POLL_NEAR_MS
    if value >= 50:
        return USAGE_POLL_MID_MS
    return USAGE_POLL_FAR_MS


def crossed_usage_threshold(
    before: float | None, after: float | None
) -> int | None:
    """前端 `crossedThreshold` 的等价实现 —— 返回本次跨过的阈值。

    ```js
    let f = [90, 75, 50, 25];
    function crossedThreshold(e,t){ return f.find(n => e<n && t>=n) ?? null }
    ```
    """
    if before is None or after is None:
        return None
    for threshold in USAGE_TOAST_THRESHOLDS:
        if before < threshold <= after:
            return threshold
    return None


# --------------------------------------------------------------------------
# 代码执行（网页路径）
# --------------------------------------------------------------------------
#
# `POST /rest/app-chat/run-code` —— **真的执行代码**，返回 stdout/stderr。
#
# 实测（2026-10-20，SSO 会话）沙箱刻画：
#
#   python 3.12.3
#   platform  Linux-4.19.0-gvisor-x86_64-with-glibc2.39   ← gVisor 用户态内核
#   cwd       /home/workdir        uid 0        cpu 2
#   DNS       通（example.com → 172.66.147.243）
#   TCP       拒绝（ConnectionRefused）—— **无网络出口**
#   文件系统   /home/workdir/{.grok,artifacts}；/root/{.bashrc,.cache,.config,.local,.npm,.profile,.ssh}
#   可用库     numpy pandas requests PIL bs4 lxml scipy matplotlib torch httpx yaml
#   缺失       sklearn aiohttp regex
#
# `language` 取值（实测，其余一律内部错误）：
#
#   python / python3  → CPython 3.12.3
#   bash              → bash
#   javascript        → Node（源码写到 `/home/workdir/temp.js`）
#
# **`sh` / `node` / `sql` / `ruby` 都不支持** —— 必须用 `bash` 与 `javascript`。
# 省略 `language` 或只传 `code` 会得到
# `An unexpected error occurred while executing your code.`（不是语法错，是缺语言）。
RUN_CODE_PATH = "/rest/app-chat/run-code"
RUN_CODE_LANGUAGES = ("python", "python3", "bash", "javascript")
RUN_CODE_DEFAULT_LANGUAGE = "python"

# 沙箱事实（写死供调用方判断可行性，实测值）
RUN_CODE_SANDBOX = {
    "python": "3.12.3",
    "platform": "Linux-4.19.0-gvisor-x86_64-with-glibc2.39",
    "cwd": "/home/workdir",
    "uid": 0,
    "cpu": 2,
    "dns": True,
    "tcp_egress": False,
    "libraries": ("numpy", "pandas", "requests", "PIL", "bs4", "lxml",
                  "scipy", "matplotlib", "torch", "httpx", "yaml"),
    "missing_libraries": ("sklearn", "aiohttp", "regex"),
}


def run_code(
    code: str,
    *,
    language: str = RUN_CODE_DEFAULT_LANGUAGE,
    bearer: str | None = None,
    cookies: Any = None,
    proxy: str | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """在 Grok 网页沙箱里执行代码。

    返回 `{"success": bool, "stdout": str, "stderr": str, "outputFiles": dict}`。

    **注意**：`success=False` 时 `stderr` 可能是**通用错误文案**
    （`An unexpected error occurred while executing your code.`）而不是真实
    异常 —— 实测语言缺失、`sh`/`node`/`sql`/`ruby`、以及部分运行时失败
    都返回这句。真正的语法/运行错误会带堆栈（如 Node 的 `SyntaxError`）。

    沙箱是 **gVisor**、`uid 0`、2 CPU、**无 TCP 出口**（DNS 可用）。
    见 :data:`RUN_CODE_SANDBOX`。
    """
    jar, token = _auth(cookies, bearer)
    if not jar and not token:
        raise GrokWebError("缺少认证：需要 bearer 或 cookies")
    lang = str(language or RUN_CODE_DEFAULT_LANGUAGE).strip().lower()
    if lang not in RUN_CODE_LANGUAGES:
        raise GrokWebError(
            f"不支持的语言 {lang!r}；实测可用：{', '.join(RUN_CODE_LANGUAGES)}")
    resp = _stream_post(
        f"{WEB_BASE}{RUN_CODE_PATH}",
        headers=build_headers(bearer=token),
        cookies=jar,
        body={"code": code, "language": lang},
        proxy=proxy,
        timeout=timeout,
    )
    with resp:
        if resp.status_code != 200:
            detail = (resp.text or "")[:300]
            raise GrokWebError(f"代码执行 HTTP {resp.status_code}：{detail}",
                               status=resp.status_code, body=detail)
        data = resp.json()
    if not isinstance(data, dict):
        return {"success": False, "stdout": "", "stderr": "", "outputFiles": {}}
    return {
        "success": bool(data.get("success")),
        "stdout": str(data.get("stdout") or ""),
        "stderr": str(data.get("stderr") or ""),
        "outputFiles": data.get("outputFiles") if isinstance(
            data.get("outputFiles"), dict) else {},
    }


def image_generations(
    *,
    bearer: str | None = None,
    cookies: Any = None,
    proxy: str | None = None,
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """`GET /rest/app-chat/image-generations` —— 生图历史。

    **实测（2026-10-20）**：SSO 会话下 → 200 `{"generations": []}`；
    但生成图片后仍为 `[]`（生图未计入），且被限流时也不记录。
    所以**不能用来计数生图用量** —— 保留作可用性记录。

    CLI OAuth token 在部分账号上是 403（`oauth2-auth-forbidden`）。
    """
    jar, token = _auth(cookies, bearer)
    if not jar and not token:
        raise GrokWebError("缺少认证：需要 bearer 或 cookies")
    resp = _stream_get(
        f"{WEB_BASE}/rest/app-chat/image-generations",
        headers=build_headers(bearer=token),
        cookies=jar,
        proxy=proxy,
        timeout=timeout,
    )
    with resp:
        if resp.status_code != 200:
            detail = (resp.text or "")[:300]
            raise GrokWebError(f"生图历史 HTTP {resp.status_code}：{detail}",
                               status=resp.status_code, body=detail)
        data = resp.json()
    if not isinstance(data, dict):
        return []
    gens = data.get("generations")
    return gens if isinstance(gens, list) else []


# --------------------------------------------------------------------------
# mediaGenInput 的六种变体与直连模型名（bundle 逆向）
# --------------------------------------------------------------------------
#
# `mediaGenInput` 是一个 **oneof**，六选一（`js/00pmbps1j6es_.js` 的 `d()`）：
#
#   if (e.textToImage)      return {kind:"textToImage",      params:e.textToImage};
#   if (e.imageToImage)     return {kind:"imageToImage",     params:e.imageToImage};
#   if (e.textToVideo)      return {kind:"textToVideo",      params:e.textToVideo};
#   if (e.imageToVideo)     return {kind:"imageToVideo",     params:e.imageToVideo};
#   if (e.referenceToVideo) return {kind:"referenceToVideo", params:e.referenceToVideo};
#   if (e.videoExtension)   return {kind:"videoExtension",   params:e.videoExtension};
#
# 各变体的字段（`js/0a18uk1xp-t5a.js` / `29p4h4iryyz76.js`）：
#
#   textToImage      {prompt, numOfImages, aspectRatio, resolutionName, modelName}
#   imageToImage     {inputAssets[], prompt, extractSubject, draw{styleId,regionIntent},
#                     mediaReferences[]}
#   textToVideo      {prompt, aspectRatio, duration, resolutionName, modelName, skipAudio}
#   imageToVideo     {prompt, inputAssets[], aspectRatio, duration, resolutionName,
#                     modelName, mode, skipAudio, useFirstFrame}
#   referenceToVideo {inputAssets[], audioAssets[], mediaReferences[],
#                     firstFrameAsset, lastFrameAsset, aspectRatio, duration,
#                     resolutionName, modelName, skipAudio}
#   videoExtension   {prompt, inputAssets[], duration, videoExtensionStartTime, skipAudio}
#
# 构造器（`js/2bbm9n6clh3_p.js`）：
#   buildTextToImageInput / buildTextToVideoInput / buildImageToVideoInput /
#   buildReferenceToVideoInput / buildVideoExtensionInput
#
# 直连模型名（`js/0oottrppfxt08.js` 的构造分支）：
#
#   if (hasInputImages) {
#     modelName = "imagine-image-edit";
#     body.imageEditModelConfig = {
#       imageReferences: [...],        // 必需
#       aspectRatio: "w:h",           // 仅 1 张图且显式设置
#       selectionRegions: [...],      // 可选，局部重绘
#     };
#   } else {
#     modelName = "imagine-image-gen";
#     body.imageGenModelConfig = {
#       aspectRatio: "w:h",
#       mode: "quality" | "fast",      // pro 档 → quality，否则 fast
#       resolutionName: "2k",          // 仅 quality + 2k + 开关打开
#     };
#   }
#
# **验证状态（2026-10-20）**：
#   * `imagine-video-gen` + `textToVideo` → **200，返回 video id** ✅ 已证实
#   * `imagine-video` → 403 `Model is not found`（错名）
#   * `imagine-image-gen` / `imagine-image-edit` → 解出但**未验证**（两次都被 429）
#   * `build` + `textToImage` → 已证实出图（§1.7）
#
# **图片与视频共享同一个速率限制桶** —— 一次视频成功后再发图片立刻 429。
MEDIA_GEN_VARIANTS = (
    "textToImage", "imageToImage", "textToVideo",
    "imageToVideo", "referenceToVideo", "videoExtension",
)

IMAGE_GEN_MODEL = "imagine-image-gen"       # 解出，未验证
IMAGE_EDIT_MODEL = "imagine-image-edit"     # 解出，未验证
VIDEO_GEN_MODEL = "imagine-video-gen"       # ✅ 已证实 200

IMAGE_GEN_MODES = ("fast", "quality")
IMAGE_RESOLUTIONS = ("1k", "2k")
VIDEO_RESOLUTIONS = ("480p", "720p", "1080p")

# 视频变体的字段集（供构造与校验）
VIDEO_VARIANT_FIELDS = {
    "textToVideo": ("prompt", "aspectRatio", "duration", "resolutionName",
                    "modelName", "skipAudio"),
    "imageToVideo": ("prompt", "inputAssets", "aspectRatio", "duration",
                     "resolutionName", "modelName", "mode", "skipAudio",
                     "useFirstFrame"),
    "referenceToVideo": ("inputAssets", "audioAssets", "mediaReferences",
                         "firstFrameAsset", "lastFrameAsset", "aspectRatio",
                         "duration", "resolutionName", "modelName", "skipAudio"),
    "videoExtension": ("prompt", "inputAssets", "duration",
                       "videoExtensionStartTime", "skipAudio"),
}


def media_gen_kind(media_gen_input: Any) -> str | None:
    """判定 `mediaGenInput` 属于哪个变体（对齐 bundle 的 `d()`）。

    六选一，按 `textToImage → imageToImage → textToVideo → imageToVideo →
    referenceToVideo → videoExtension` 的顺序取第一个存在的键。
    """
    if not isinstance(media_gen_input, dict):
        return None
    for kind in MEDIA_GEN_VARIANTS:
        if media_gen_input.get(kind) is not None:
            return kind
    return None


def build_text_to_video(
    prompt: str,
    *,
    aspect_ratio: str = "16:9",
    duration: int = 6,
    resolution: str = "480p",
    skip_audio: bool = False,
    model: str = VIDEO_GEN_MODEL,
) -> dict[str, Any]:
    """构造 `textToVideo` 的 `mediaGenInput` 片段（对齐 `buildTextToVideoInput`）。

    ```js
    function({prompt, aspectRatio, duration, resolutionName, skipAudio}) {
      return {textToVideo: {prompt,
        ...void 0 !== aspectRatio ? {aspectRatio} : {},
        ...void 0 !== duration    ? {duration}    : {},
        ...void 0 !== resolutionName ? {resolutionName} : {},
        ...skipAudio ? {skipAudio} : {}}};
    }
    ```

    `duration` 是**秒**；`resolution` 取 :data:`VIDEO_RESOLUTIONS`。
    """
    payload: dict[str, Any] = {"prompt": prompt}
    if aspect_ratio:
        payload["aspectRatio"] = aspect_ratio
    if duration:
        payload["duration"] = int(duration)
    if resolution:
        payload["resolutionName"] = resolution
    if skip_audio:
        payload["skipAudio"] = True
    return {"textToVideo": payload}


def build_image_gen_config(
    *,
    aspect_ratio: str = "1:1",
    mode: str = "fast",
    resolution: str | None = None,
) -> dict[str, Any]:
    """构造 `imageGenModelConfig`（配 `modelName="imagine-image-gen"`）。

    `mode` 取 :data:`IMAGE_GEN_MODES`；`resolution` 仅在 `quality` 档下有意义
    （`1k` 默认、`2k` 需开关）。
    """
    config: dict[str, Any] = {"aspectRatio": aspect_ratio, "mode": mode}
    if resolution and mode == "quality":
        config["resolutionName"] = resolution
    return config


def build_image_edit_config(
    image_references: list[str],
    *,
    aspect_ratio: str | None = None,
    selection_regions: list[Any] | None = None,
) -> dict[str, Any]:
    """构造 `imageEditModelConfig`（配 `modelName="imagine-image-edit"`）。

    `image_references` 是图片引用列表（bundle 里由 `E(assetServerUrl, mediaUrl)`
    生成）。`aspect_ratio` **仅单图且显式设置**时才发；`selection_regions`
    用于局部重绘。
    """
    if not image_references:
        raise GrokWebError("图片编辑至少需要 1 张参考图")
    config: dict[str, Any] = {"imageReferences": list(image_references)}
    if aspect_ratio and len(image_references) == 1:
        config["aspectRatio"] = aspect_ratio
    if selection_regions:
        config["selectionRegions"] = list(selection_regions)
    return config


def generate_video(
    prompt: str,
    *,
    cookies: Any = None,
    bearer: str | None = None,
    proxy: str | None = None,
    aspect_ratio: str = "16:9",
    duration: int = 6,
    resolution: str = "480p",
    skip_audio: bool = False,
    model: str = VIDEO_GEN_MODEL,
    timeout: float = 900.0,
) -> dict[str, Any]:
    """调用网页路径生成视频。

    **实测（2026-10-20）**：`modelName: "imagine-video-gen"` +
    `mediaGenInput.textToVideo` → **200**，响应里出现
    `d1b9572a-348c-4a25-8b01-a912c09c2284` 形式的 **video id**。

    模型名必须精确：`imagine-video` → **403 `Model is not found`**。

    视频是**异步任务** —— 本次只拿到 id，未确认轮询端点
    （`/rest/media/video/*` 只有 `music`/`caption`/`upscale`/`video-sequence`，
    都不是状态查询）。所以本函数返回 id 与原始帧，由调用方自行处理产物。

    **注意：图片与视频共享同一个速率限制桶** —— 实测一次视频成功后再发图片
    立刻 `429 Too many requests`。视频额度未单独测出。
    """
    frames: list[dict[str, Any]] = []
    text_parts: list[str] = []
    video_ids: list[str] = []
    errors: list[str] = []
    response_id = None
    conversation_id = None

    for response in stream_conversation(
        prompt, cookies=cookies, bearer=bearer, proxy=proxy, timeout=timeout,
        model=model, temporary=True, video=True,
        aspect_ratio=aspect_ratio, video_duration=duration,
        video_resolution=resolution, video_skip_audio=skip_audio,
    ):
        frames.append(response)
        if response_id is None and isinstance(response.get("responseId"), str):
            response_id = response["responseId"]
        if conversation_id is None and isinstance(response.get("conversationId"), str):
            conversation_id = response["conversationId"]
        text_parts.append(extract_text(response))
        for vid in _extract_video_ids(response):
            if vid not in video_ids:
                video_ids.append(vid)
        limited = image_rate_limited(response)
        if limited:
            errors.append(limited)
        unavailable = model_unavailable(response)
        if unavailable:
            errors.append(unavailable)

    return {
        "video_ids": video_ids,
        "text": "".join(text_parts),
        "errors": errors,
        "response_id": response_id,
        "conversation_id": conversation_id,
        "frames": len(frames),
    }


def _extract_video_ids(response: dict[str, Any]) -> list[str]:
    """从一帧里取视频 id（`video_chunk` / `streamingVideoGenerationResponse`）。"""
    out: list[str] = []

    card = response.get("cardAttachment")
    if isinstance(card, dict):
        raw = card.get("jsonData")
        data: dict[str, Any] | None = None
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    data = parsed
            except (json.JSONDecodeError, ValueError):
                data = None
        elif isinstance(raw, dict):
            data = raw
        if isinstance(data, dict):
            chunk = data.get("video_chunk")
            if isinstance(chunk, dict):
                for key in ("videoUuid", "videoUrl", "videoId"):
                    value = chunk.get(key)
                    if isinstance(value, str) and value:
                        out.append(value)

    frame = response.get("streamingVideoGenerationResponse")
    if isinstance(frame, dict):
        for key in ("videoId", "videoUrl", "assetId"):
            value = frame.get(key)
            if isinstance(value, str) and value:
                out.append(value)

    return out


UPLOAD_FILE_PATH = "/rest/app-chat/upload-file"

# `chatUploadFileRaw` 的真实请求体（bundle 提取）—— **JSON + base64**，
# 不是 multipart。用 `files=` 会 400。
#
#   {fileName, fileMimeType, content, makePublic,
#    fileSource, thirdPartyFileId, workspaceId}
UPLOAD_FILE_FIELDS = ("fileName", "fileMimeType", "content", "makePublic",
                      "fileSource", "thirdPartyFileId", "workspaceId")


def upload_file(
    content: bytes | str,
    *,
    file_name: str = "upload.bin",
    mime_type: str = "application/octet-stream",
    make_public: bool = False,
    bearer: str | None = None,
    cookies: Any = None,
    proxy: str | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """上传文件，返回 `fileMetadataId` / `fileUri`（图生图的前置步骤）。

    **请求体是 JSON，`content` 是 base64 字符串** —— 不是 multipart。
    实测用 `files=` 会得到 **400**（服务端读不到 body）。

    返回的 `fileUri` 用作 `mediaGenInput.imageToImage.inputAssets` 与
    `imageEditModelConfig.imageReferences` 的元素。
    """
    jar, token = _auth(cookies, bearer)
    if not jar and not token:
        raise GrokWebError("缺少认证：需要 bearer 或 cookies")
    if isinstance(content, str):
        raw = content.encode("utf-8")
    else:
        raw = bytes(content)
    if not raw:
        raise GrokWebError("上传内容为空")
    payload = {
        "fileName": file_name,
        "fileMimeType": mime_type,
        "content": base64.b64encode(raw).decode("ascii"),
        "makePublic": bool(make_public),
    }
    resp = _stream_post(
        f"{WEB_BASE}{UPLOAD_FILE_PATH}",
        headers=build_headers(bearer=token),
        cookies=jar,
        body=payload,
        proxy=proxy,
        timeout=timeout,
    )
    with resp:
        if resp.status_code != 200:
            detail = (resp.text or "")[:300]
            raise GrokWebError(f"上传失败 HTTP {resp.status_code}：{detail}",
                               status=resp.status_code, body=detail)
        data = resp.json()
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------
# 媒体限流诊断
# --------------------------------------------------------------------------
#
# **修正（2026-10-21 晚）**：曾一度得出「媒体配额按出口 IP 计量」的结论，
# 该结论**已被推翻** —— 证据被一个请求体缺陷污染了。
#
# 对照实验（同一账号、同一 prompt，只改请求体）：
#
#   A) `build_body` 的完整 body          → **出图** ✅
#   B) 精简 body（缺 disableSearch / isReasoning / modelMode，
#      且缺 `x-xai-request-id` 头）       → 200 + **无图**，正文却说
#                                          "I generated an image with the prompt: ..."
#
# 即 **`silent_degrade` 有两个完全不同的成因**：
#
#   1. **请求体不完整** —— 服务端照常回 200 并声称已生成，但不产出
#      `cardAttachment`。缺 `disableSearch` / `isReasoning` / `modelMode`
#      任一即可触发。**这是调用方的 bug，不是配额问题。**
#   2. **真配额耗尽** —— 用完整 body 连发约 4–8 次后出现。
#
# 之前「三个全新账号首次调用即失败 → IP 级限流」的推断**不成立**：
# 那三个账号当时用的正是精简 body。
#
# 正确的排查顺序：**先确认请求体走的是 `build_body`（完整字段 + request-id 头），
# 再看是不是配额。** 用精简 body 探配额会得到假阳性。
#
# 关于 IP：codex2api 的 CHANGELOG 确实提到
# "Too many Grok accounts egressing from one IP triggers upstream 402s"，
# 且它有 `POST /proxies/auto-balance`。但**本仓库没有复现出 IP 级约束** ——
# 同 IP 下新账号在完整 body 下可以正常出图。
MEDIA_LIMIT_VERDICTS = {
    "ok": "仍有媒体预算",
    "quota_exhausted": "配额耗尽（全部账号 429 / 静默降级）",
    "account_limited": "账号级差异（部分账号可用）",
    "unknown": "无法判定",
}


def diagnose_media_limit(
    results: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """根据多个账号的 `generate_image()` 结果判定限流层级。

    传入每个账号的结果（:func:`generate_image` 的返回值即可），返回::

        {"verdict": "ok" | "ip_exhausted" | "account_limited" | "unknown",
         "ok_count": int, "limited_count": int, "reason": str}

    判定逻辑：

    * 有任一成功 → ``ok``（还有预算）
    * 全部失败 → ``quota_exhausted``
    * 部分成功部分失败 → ``account_limited``（账号间存在差异）

    **前提**：传入的结果必须来自 :func:`generate_image`（它走完整的
    :func:`build_body`）。若调用方自己拼了精简 body，会得到假的
    ``silent_degrade`` —— 那种情况下本函数会误判为 ``quota_exhausted``。
    """
    ok = 0
    limited = 0
    for result in results:
        if not isinstance(result, dict):
            continue
        if result.get("images"):
            ok += 1
            continue
        if (result.get("rate_limited") or result.get("silent_degrade")
                or result.get("failed")):
            limited += 1
    if ok and not limited:
        verdict, reason = "ok", "全部成功"
    elif ok and limited:
        verdict, reason = ("account_limited",
                           "部分账号成功 —— IP 有预算，失败是账号级差异")
    elif limited and not ok:
        verdict, reason = ("quota_exhausted",
                           "全部账号失败 —— 先确认请求体走的是 build_body"
                           "（完整字段 + request-id 头），再判断配额")
    else:
        verdict, reason = "unknown", "无有效结果"
    return {"verdict": verdict, "ok_count": ok, "limited_count": limited,
            "reason": reason}


# --------------------------------------------------------------------------
# 模型目录与提示词增强
# --------------------------------------------------------------------------
#
# `POST /rest/models`（GET 返回 501）返回 33 个模型。单个条目 schema：
#
#   {modelId, name, description, modeDescription, modeName,
#    badgeText, tags[], modelMode, promptingBackend}
#
# 例（`build`）：`badgeText: "Beta"`、`tags: ["hide_tools_settings", "computer"]`、
# `modelMode: "MODEL_MODE_BUILD"`、`promptingBackend: "CHAT"`。
#
# 顶层另有默认值：
#   defaultAnonModel/defaultFreeModel = "grok-3"
#   defaultHeavyModel/defaultProModel = "grok-4"
#   unavailableModels = []
#
# **注意：`imagine-image-gen` / `imagine-image-edit` / `imagine-video-gen`
# 不在目录里** —— 它们是前端 image studio 直连的隐藏模型名。
MODELS_PATH = "/rest/models"
MODEL_FIELDS = ("modelId", "name", "description", "modeDescription",
                "modeName", "badgeText", "tags", "modelMode", "promptingBackend")

# 目录里的媒体相关模型（实测 2026-10-21）
CATALOG_MEDIA_MODELS = (
    "build", "imagine-agent-mode", "imagine-agent-mode-dev",
    "imagine-agent-mode-grok-4-5", "imagine-video-prompt-enhance",
)

# 提示词增强 —— **纯文本模型，不消耗媒体配额**。
# 实测：所有生图模型都 429 时它仍返回 200。
# 输出形如："A cat walks steadily through falling rain along a wet street...
#            Raindrops glisten on its fur as it moves forward with a natural
#            gait, paws carefully navigating puddles and reflective ground."
PROMPT_ENHANCE_MODEL = "imagine-video-prompt-enhance"


def list_models(
    *,
    bearer: str | None = None,
    cookies: Any = None,
    proxy: str | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """`POST /rest/models` —— 模型目录（**必须用 POST**，GET 返回 501）。

    返回原始响应，含 `models[]` 与默认值字段（见 :data:`MODEL_FIELDS`）。
    """
    jar, token = _auth(cookies, bearer)
    if not jar and not token:
        raise GrokWebError("缺少认证：需要 bearer 或 cookies")
    resp = _stream_post(
        f"{WEB_BASE}{MODELS_PATH}",
        headers=build_headers(bearer=token),
        cookies=jar,
        body={},
        proxy=proxy,
        timeout=timeout,
    )
    with resp:
        if resp.status_code != 200:
            detail = (resp.text or "")[:300]
            raise GrokWebError(f"模型目录 HTTP {resp.status_code}：{detail}",
                               status=resp.status_code, body=detail)
        data = resp.json()
    return data if isinstance(data, dict) else {}


def model_ids(catalog: dict[str, Any]) -> list[str]:
    """从目录响应里取全部 `modelId`。"""
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if not isinstance(models, list):
        return []
    out: list[str] = []
    for item in models:
        if isinstance(item, dict):
            value = item.get("modelId")
            if isinstance(value, str) and value:
                out.append(value)
    return out


def enhance_prompt(
    prompt: str,
    *,
    bearer: str | None = None,
    cookies: Any = None,
    proxy: str | None = None,
    timeout: float = 180.0,
) -> str:
    """用 `imagine-video-prompt-enhance` 把简短提示扩写成详细描述。

    **这是纯文本模型，不消耗媒体配额** —— 实测所有生图模型都 429 时它仍返回 200。

    实测输出（输入 `"a cat walking in rain"`）：

        "A cat walks steadily through falling rain along a wet street or path.
         Raindrops glisten on its fur as it moves forward with a natural gait,
         paws carefully navigating puddles and reflective ground. ..."

    返回扩写后的文本；无内容时返回空串。
    """
    result = ask(
        prompt, cookies=cookies, bearer=bearer, proxy=proxy, timeout=timeout,
        model=PROMPT_ENHANCE_MODEL, temporary=True,
    )
    return result["text"].strip()


# --------------------------------------------------------------------------
# 语音合成（TTS）
# --------------------------------------------------------------------------
#
# `POST /rest/app-chat/text-to-speech` —— 返回 **NDJSON**，每行
# `{"result":{"data":"<base64 音频分块>"}}`，把所有 `data` 拼接后 base64 解码
# 即为完整音频（实测 MP3，帧头 `0xFF 0xF3`）。
#
# 请求体（`chatTextToSpeechRaw`）：
#   {text, voiceId, language, codec, sampleRate, bitRate,
#    optimizeStreamingLatency, textNormalization, speed}
#
# `GET /rest/app-chat/tts-voices` 返回 `{voices:[{voiceId,name,language,gender}]}`；
# 实测该端点用 **SSO cookie** 可读，用 CLI Bearer 可能 403。
TTS_PATH = "/rest/app-chat/text-to-speech"
TTS_VOICES_PATH = "/rest/app-chat/tts-voices"
TTS_FIELDS = ("text", "voiceId", "language", "codec", "sampleRate", "bitRate",
              "optimizeStreamingLatency", "textNormalization", "speed")


def _audio_format(raw: bytes) -> str:
    """按魔数判断音频容器。"""
    if not raw:
        return "empty"
    if raw[:4] == b"RIFF":
        return "wav"
    if raw[:4] == b"OggS":
        return "ogg"
    if raw[:3] == b"ID3" or raw[:2] in (b"\xff\xf3", b"\xff\xfb", b"\xff\xf2"):
        return "mp3"
    if len(raw) >= 8 and raw[4:8] == b"ftyp":
        return "m4a"
    return "unknown"


def text_to_speech(
    text: str,
    *,
    voice_id: str = "altair",
    language: str | None = None,
    codec: str | None = None,
    sample_rate: int | None = None,
    bit_rate: int | None = None,
    speed: float | None = None,
    text_normalization: bool | None = None,
    optimize_streaming_latency: bool | None = None,
    bearer: str | None = None,
    cookies: Any = None,
    proxy: str | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """把文本合成为语音。

    返回 `{"audio": bytes, "format": "mp3", "chunks": int, "bytes": int}`。

    响应是 **NDJSON**：每行 `{"result":{"data":"<base64>"}}`，逐块拼接后
    base64 解码得到完整音频（实测 MP3，`0xFF 0xF3` 帧头）。
    """
    jar, token = _auth(cookies, bearer)
    if not jar and not token:
        raise GrokWebError("缺少认证：需要 bearer 或 cookies")
    if not str(text or "").strip():
        raise GrokWebError("文本为空")
    body: dict[str, Any] = {"text": text, "voiceId": voice_id}
    for key, value in (("language", language), ("codec", codec),
                       ("sampleRate", sample_rate), ("bitRate", bit_rate),
                       ("speed", speed),
                       ("textNormalization", text_normalization),
                       ("optimizeStreamingLatency", optimize_streaming_latency)):
        if value is not None:
            body[key] = value
    resp = _stream_post(
        f"{WEB_BASE}{TTS_PATH}",
        headers=build_headers(bearer=token),
        cookies=jar,
        body=body,
        proxy=proxy,
        timeout=timeout,
    )
    with resp:
        if resp.status_code != 200:
            detail = (resp.text or "")[:300]
            raise GrokWebError(f"语音合成 HTTP {resp.status_code}：{detail}",
                               status=resp.status_code, body=detail)
        raw = resp.text or ""
    chunks: list[str] = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = item.get("result") if isinstance(item, dict) else None
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, str) and data:
                chunks.append(data)
    audio = b""
    if chunks:
        try:
            audio = b"".join(base64.b64decode(c) for c in chunks)
        except (ValueError, TypeError) as exc:
            raise GrokWebError(f"音频分块解码失败：{exc}") from exc
    return {"audio": audio, "format": _audio_format(audio),
            "chunks": len(chunks), "bytes": len(audio)}


def tts_voices(
    *,
    bearer: str | None = None,
    cookies: Any = None,
    proxy: str | None = None,
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """`GET /rest/app-chat/tts-voices` —— 可用音色列表。

    返回 `[{voiceId, name, language, gender}, ...]`（实测含 `altair` / `ara` 等）。
    该端点用 SSO cookie 可读；CLI Bearer 可能 403。
    """
    jar, token = _auth(cookies, bearer)
    if not jar and not token:
        raise GrokWebError("缺少认证：需要 bearer 或 cookies")
    resp = _stream_get(
        f"{WEB_BASE}{TTS_VOICES_PATH}",
        headers=build_headers(bearer=token),
        cookies=jar,
        proxy=proxy,
        timeout=timeout,
    )
    with resp:
        if resp.status_code != 200:
            detail = (resp.text or "")[:300]
            raise GrokWebError(f"音色列表 HTTP {resp.status_code}：{detail}",
                               status=resp.status_code, body=detail)
        data = resp.json()
    voices = data.get("voices") if isinstance(data, dict) else None
    return voices if isinstance(voices, list) else []


# --------------------------------------------------------------------------
# 模板库（Imagine pipeline templates）
# --------------------------------------------------------------------------
#
# 实测（2026-10-21）：**20 个模板，全部 `creditCost: 0`（免费）**。
#
#   POST /rest/media/pipeline/template/list           → {templates[], summaries[]}
#   POST /rest/media/pipeline/template-folder/list    → {folders[]}
#   POST /rest/media/pipeline/template/get            → {summary, isSaved}
#
# `summary` 字段：
#   {id, name, description, thumbnailUrl, inputs[], publishedVersion, userId,
#    runCount, creditCost, createdAt, primaryOutputMediaType, folders[]}
#
#   `inputs[]` 是 **typed**：`{name, inputType: "image"|"text", label, required}`
#   `primaryOutputMediaType` 实测为 `image` 或 `video`
#
# **`template/get` 只返回 `summary`，不含实际提示词/流水线定义** —— 提示词在服务端。
# **`POST /rest/media/pipeline/run` 实测 403 `oauth2-auth-forbidden`** ——
# 模板可以列、可以读，但**不能用 API 跑**（只能从网页 UI 用）。
TEMPLATE_LIST_PATH = "/rest/media/pipeline/template/list"
TEMPLATE_FOLDER_LIST_PATH = "/rest/media/pipeline/template-folder/list"
TEMPLATE_GET_PATH = "/rest/media/pipeline/template/get"
PIPELINE_RUN_PATH = "/rest/media/pipeline/run"

# 模板 summary 的字段集
TEMPLATE_FIELDS = ("id", "name", "description", "thumbnailUrl", "inputs",
                   "publishedVersion", "userId", "runCount", "creditCost",
                   "createdAt", "primaryOutputMediaType", "folders")

# 实测 20 个模板的名称 → 输出类型（2026-10-21）
CATALOG_TEMPLATES = (
    ("Glossy Product Shot", "image"), ("Chibi", "image"),
    ("Object Remover", "image"), ("Professional Headshot", "image"),
    ("Haze Portrait", "image"), ("Product Showcase", "video"),
    ("Logo Editor", "image"), ("70s Street Style", "image"),
    ("Quality Enhancer", "image"), ("Comic Book", "image"),
    ("80s Anime", "image"), ("Watercolor Portrait", "image"),
    ("Video Game", "image"), ("3D Animation", "image"),
    ("Spaghetti Western", "image"), ("Funky Dance", "video"),
    ("Future Past", "image"), ("Roman Empire", "image"),
    ("Ad Astra", "video"), ("Add Girlfriend", "video"),
)


def templates(
    *,
    bearer: str | None = None,
    cookies: Any = None,
    proxy: str | None = None,
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """列出 Imagine 模板库（实测 20 个，全部 `creditCost: 0`）。

    返回 `summaries[]`，每项含 `id` / `name` / `description` / `inputs[]` /
    `primaryOutputMediaType` / `runCount` / `folders[]` 等。

    **注意**：模板可以列、可以读，但 `POST /rest/media/pipeline/run` 实测
    **403** —— 不能用 API 执行，只能从网页 UI 用。
    """
    jar, token = _auth(cookies, bearer)
    if not jar and not token:
        raise GrokWebError("缺少认证：需要 bearer 或 cookies")
    resp = _stream_post(
        f"{WEB_BASE}{TEMPLATE_LIST_PATH}",
        headers=build_headers(bearer=token),
        cookies=jar,
        body={},
        proxy=proxy,
        timeout=timeout,
    )
    with resp:
        if resp.status_code != 200:
            detail = (resp.text or "")[:300]
            raise GrokWebError(f"模板列表 HTTP {resp.status_code}：{detail}",
                               status=resp.status_code, body=detail)
        data = resp.json()
    if not isinstance(data, dict):
        return []
    summaries = data.get("summaries")
    return summaries if isinstance(summaries, list) else []


def template_folders(
    *,
    bearer: str | None = None,
    cookies: Any = None,
    proxy: str | None = None,
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """模板分类（`{slug, name, sortOrder}`）。"""
    jar, token = _auth(cookies, bearer)
    if not jar and not token:
        raise GrokWebError("缺少认证：需要 bearer 或 cookies")
    resp = _stream_post(
        f"{WEB_BASE}{TEMPLATE_FOLDER_LIST_PATH}",
        headers=build_headers(bearer=token),
        cookies=jar,
        body={},
        proxy=proxy,
        timeout=timeout,
    )
    with resp:
        if resp.status_code != 200:
            detail = (resp.text or "")[:300]
            raise GrokWebError(f"模板分类 HTTP {resp.status_code}：{detail}",
                               status=resp.status_code, body=detail)
        data = resp.json()
    if not isinstance(data, dict):
        return []
    folders = data.get("folders")
    return folders if isinstance(folders, list) else []


def template_detail(
    template_id: str,
    *,
    bearer: str | None = None,
    cookies: Any = None,
    proxy: str | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """取单个模板的 `summary`（**不含提示词** —— 提示词在服务端）。"""
    jar, token = _auth(cookies, bearer)
    if not jar and not token:
        raise GrokWebError("缺少认证：需要 bearer 或 cookies")
    tid = str(template_id or "").strip()
    if not tid:
        raise GrokWebError("template_id 为空")
    resp = _stream_post(
        f"{WEB_BASE}{TEMPLATE_GET_PATH}",
        headers=build_headers(bearer=token),
        cookies=jar,
        body={"templateId": tid},
        proxy=proxy,
        timeout=timeout,
    )
    with resp:
        if resp.status_code != 200:
            detail = (resp.text or "")[:300]
            raise GrokWebError(f"模板详情 HTTP {resp.status_code}：{detail}",
                               status=resp.status_code, body=detail)
        data = resp.json()
    if not isinstance(data, dict):
        return {}
    summary = data.get("summary")
    return summary if isinstance(summary, dict) else data


# --------------------------------------------------------------------------
# OAuth 刷新
# --------------------------------------------------------------------------
#
# 实测（2026-10-21）：`POST https://auth.x.ai/oauth2/token` +
# `grant_type=refresh_token` 可用，返回新的 `access_token`（有效期 21600s = 6h）。
#
#   data: grant_type=refresh_token&refresh_token=<rt>&client_id=<cid>
#   → {access_token, refresh_token?, expires_in, token_type, id_token?}
#
# **失效的 refresh token 返回 HTTP 400 + `invalid_grant`**
# （`Invalid or unknown refresh token`）—— 这是永久性错误，重试无用。
#
# **刷新不重置媒体配额** —— 实测刷新后仍受同一预算约束。
OAUTH_TOKEN_URL = "https://auth.x.ai/oauth2/token"
OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OAUTH_SCOPES = ("openid", "profile", "email", "offline_access",
                "grok-cli:access", "api:access",
                "conversations:read", "conversations:write")


def refresh_access_token(
    refresh_token: str,
    *,
    client_id: str = OAUTH_CLIENT_ID,
    proxy: str | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """用 refresh_token 换新的 access_token。

    返回 `{"access_token", "refresh_token"?, "expires_in", "token_type",
    "id_token"?}`。

    **`invalid_grant` 是永久性错误**（HTTP 400，`Invalid or unknown refresh
    token`）—— refresh token 已死，重试无用，应把账号标为需重新授权。

    **刷新不重置媒体配额** —— 实测刷新后仍受同一预算约束。
    """
    rt = str(refresh_token or "").strip()
    if not rt:
        raise GrokWebError("缺少 refresh_token")
    from urllib.parse import urlparse

    from services.proxy_service import is_transient_tls_error

    form = {"grant_type": "refresh_token", "refresh_token": rt,
            "client_id": client_id}
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Accept": "application/json"}
    host = urlparse(OAUTH_TOKEN_URL).netloc
    resp = None
    last: Exception | None = None
    for attempt in range(1, 9):
        try:
            resp = _session(host, proxy).post(
                OAUTH_TOKEN_URL, data=form, headers=headers, timeout=timeout)
            break
        except requests.RequestException as exc:
            last = exc
            if attempt < 8 and is_transient_tls_error(str(exc), method="post"):
                _SESSIONS.pop(f"{host}|{proxy or ''}", None)
                time.sleep(0.4 * attempt)
                continue
            raise GrokWebError(f"刷新请求失败: {exc}") from exc
    if resp is None:
        raise GrokWebError(f"刷新请求失败: {last}")
    text = resp.text or ""
    if resp.status_code != 200:
        kind = "invalid_grant" if "invalid_grant" in text else "error"
        raise GrokWebError(f"刷新失败（{resp.status_code}/{kind}）：{text[:200]}",
                           status=resp.status_code, body=text[:300])
    try:
        data = resp.json()
    except ValueError as exc:
        raise GrokWebError(f"刷新响应不是 JSON：{text[:200]}") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise GrokWebError(f"刷新响应缺少 access_token：{text[:200]}")
    return data


# --------------------------------------------------------------------------
# 媒体配额（真正的配额端点）
# --------------------------------------------------------------------------
#
# **`POST /rest/media/imagine/quota_info`** —— 实测 2026-10-21 打通。
#
# **必须用 SSO cookie**（`Cookie: sso=<token>`）；CLI 签发的 Bearer 一律 403。
#
# 响应（实测）::
#
#   {"image": null,
#    "imagePro": {"available": true, "remainingQueries": 4,
#                 "windowSizeSeconds": 86400},
#    "imageEdit": null,
#    "video": null,
#    "video720p": {"available": true, "remainingQueries": 1,
#                  "windowSizeSeconds": 86400}}
#
# **窗口是 86400 秒 = 24 小时** —— 这解释了一切：
# 为什么 3.5 小时后仍未恢复，为什么 bundle 里 `formatAvailableAt` 的
# 「+30 分钟」对不上（那是 credit-quota 的显示逻辑，不是 media quota）。
#
# 桶的划分：`image` / `imagePro` / `imageEdit` / `video` / `video720p`。
# 实测 `image` / `imageEdit` / `video` 为 null，只有 `imagePro` 与
# `video720p` 有数据 —— 说明当前账号走的是 Pro 桶。
#
# 其它相关端点（实测）：
#
#   GET  /rest/media/imagine/generation_estimate
#        → {"estimate": null, "shouldShow": false,
#           "imagineFreeCreditActive": false,
#           "imagineFreeCreditAllowance": "0",
#           "imagineFreeCreditRemaining": "0"}
#   POST /rest/models/imagine/overrides  → {"modelMapOverrides": []}
#   GET  /rest/usage/free-usage-gates
#        → {"chat":{"allowance":"0","remaining":"0"},
#           "imagine":{...},"voice":{...},"build":{...}}
#   GET  /rest/tasks/usage
#        → {"frequentUsage":0,"frequentLimit":2,
#           "occasionalUsage":0,"occasionalLimit":10}
#   GET  /rest/assets/storage-usage
#        → {"totalStorageBytes":"1073741824","usedStorageBytes":"0", ...}
#   POST /rest/media/reference/list  → {"references": []}
#
# **`POST /rest/rate-limits` 不可用**：GET 返回 501 Method Not Allowed，
# POST 返回 404 "Model not found." —— 它不是配额端点。
QUOTA_INFO_PATH = "/rest/media/imagine/quota_info"
GENERATION_ESTIMATE_PATH = "/rest/media/imagine/generation_estimate"
IMAGINE_OVERRIDES_PATH = "/rest/models/imagine/overrides"
MEDIA_QUOTA_BUCKETS = ("image", "imagePro", "imageEdit", "video", "video720p")


def quota_info(
    *,
    cookies: Any = None,
    bearer: str | None = None,
    proxy: str | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """读媒体配额（`POST /rest/media/imagine/quota_info`）。

    **必须用 SSO cookie** —— CLI Bearer 一律 403。返回原始响应::

        {"image": null,
         "imagePro": {"available": true, "remainingQueries": 4,
                      "windowSizeSeconds": 86400},
         "imageEdit": null, "video": null,
         "video720p": {"available": true, "remainingQueries": 1,
                       "windowSizeSeconds": 86400}}

    **窗口是 24 小时（86400 秒）。**
    """
    jar, token = _auth(cookies, bearer)
    if not jar and not token:
        raise GrokWebError("缺少认证：需要 cookies（quota_info 只认 SSO cookie）")
    resp = _stream_post(
        f"{WEB_BASE}{QUOTA_INFO_PATH}",
        headers=build_headers(bearer=token),
        cookies=jar,
        body={},
        proxy=proxy,
        timeout=timeout,
    )
    with resp:
        if resp.status_code != 200:
            detail = (resp.text or "")[:300]
            raise GrokWebError(f"配额查询 HTTP {resp.status_code}：{detail}",
                               status=resp.status_code, body=detail)
        data = resp.json()
    return data if isinstance(data, dict) else {}


def remaining_media(quota: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把 :func:`quota_info` 的响应压成「有数据的桶」。

    只保留值非 null 的桶，并补一个 `windowHours`：:

        {"imagePro": {"available": true, "remainingQueries": 4,
                      "windowSizeSeconds": 86400, "windowHours": 24.0},
         "video720p": {...}}
    """
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(quota, dict):
        return out
    for bucket in MEDIA_QUOTA_BUCKETS:
        value = quota.get(bucket)
        if not isinstance(value, dict):
            continue
        item = dict(value)
        try:
            item["windowHours"] = round(float(item.get("windowSizeSeconds") or 0) / 3600, 2)
        except (TypeError, ValueError):
            item["windowHours"] = None
        out[bucket] = item
    return out


# --------------------------------------------------------------------------
# 生图失败的五种形态
# --------------------------------------------------------------------------
#
# 判定生图成功**只能看** `cardAttachment.image_chunk.imageUrl` 是否存在。
# HTTP 200、正文声称"已生成"、agent 说"这是结果" —— **全都不可信**。
#
#   ① 请求层 429        {"error":{"code":8,"message":"Too many requests"}}
#   ② 流内限流          renderToolRateLimited + systemErrCode 1006
#   ③ 请求体不完整      200、无图、正文 "I generated an image with the prompt: ..."
#   ④ **agent 幻觉**    200、无图、正文 "Here's the apple recolored to ..."
#   ⑤ 模型报错          200、无图、正文 "Oops, something broke. Talk to me later?"
#
# **形态 ④ 最危险**：`imagine-agent-mode` 在配额耗尽时会用自然语言描述一张
# 并不存在的图，冒号后面什么都没有。只解析正文的客户端会误判成功。
#
# 实测（170 帧完整流）：`assets.grok.com` 出现 0 次、`cardAttachment` 0 次，
# 只有 `modelResponse`。
AGENT_HALLUCINATION_MARKERS = (
    "here's the", "here is the", "here you go", "i've recolored",
    "i've changed", "i've edited", "preserved:",
)
MODEL_BROKEN_MARKER = "Oops, something broke"
QUOTA_EXHAUSTED_MARKER = "image generation limit"


def claims_success_without_image(
    text: str,
    images: Sequence[Any] | None = None,
) -> bool:
    """检测**形态 ④：agent 幻觉** —— 正文声称给了图，但没有图。

    实测反例（`imagine-agent-mode` 配额耗尽时）::

        "Here's the apple recolored to bright green, with the original shape,
         size, lighting, texture, and white background preserved:"

    冒号后面什么都没有，整个 170 帧流里 `cardAttachment` 出现 0 次。

    返回 True 表示**看起来成功但其实没有图** —— 调用方应视为失败。
    """
    if images:
        return False
    body = str(text or "").strip().lower()
    if not body:
        return False
    return any(marker in body for marker in AGENT_HALLUCINATION_MARKERS)


def model_broken(text: str) -> bool:
    """**形态 ⑤**：模型自身报错（实测 `imagine-image-edit` 稳定复现）。"""
    return MODEL_BROKEN_MARKER.lower() in str(text or "").lower()


def quota_exhausted_hint(text: str) -> bool:
    """正文里是否出现配额耗尽的显式提示。

    实测 agent 在追问后会明说::

        "It looks like you've hit the image generation limit for this session,
         so I can't create or edit images right now."
    """
    return QUOTA_EXHAUSTED_MARKER.lower() in str(text or "").lower()


def download_image(
    key: str,
    *,
    bearer: str | None = None,
    cookies: Any = None,
    proxy: str | None = None,
    timeout: float = 120.0,
) -> bytes:
    """下载 `assets.grok.com` 上的图片。

    **必须带认证** —— 实测无认证返回 403（0 字节）；带 Bearer 或 SSO cookie
    返回 200 + `image/jpeg`（351,813 字节，784×1168）。

    `key` 是 :func:`extract_images` 返回的 `image_url`（已是绝对地址）。
    """
    url = asset_url(key)
    if not url:
        raise GrokWebError("缺少图片 key")
    jar, token = _auth(cookies, bearer)
    if not jar and not token:
        raise GrokWebError("下载图片需要 bearer 或 cookies")
    headers: dict[str, str] = {"Accept": "image/*,*/*"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = _stream_get(
            url,
            headers=headers,
            cookies=jar,
            proxy=proxy,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise GrokWebError(f"图片下载失败: {exc}") from exc
    if resp.status_code != 200:
        raise GrokWebError(f"图片下载 HTTP {resp.status_code}", status=resp.status_code)
    return resp.content


def generate_image(
    prompt: str,
    *,
    cookies: Any = None,
    bearer: str | None = None,
    proxy: str | None = None,
    count: int = 1,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    mode: str | None = None,
    model: str = IMAGE_MODEL,
    image_gen_config: dict[str, Any] | None = None,
    image_edit_config: dict[str, Any] | None = None,
    download: bool = False,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """调用网页 Imagine 生图。

    **实测可用（2026-10-19）**：`model=build` + `kind=CONVERSATION_KIND_IMAGINE`
    + `mediaGenInput.textToImage` → 200，图片在 `cardAttachment` 里，
    最终帧 `progress=100` 的 `imageUrl` 可下载（784×1168 JPEG）。

    模型名选择：

    * `build`（默认）—— 直接出图，实测成功
    * `imagine-agent-mode` —— 走 MCP 工具 `imagine_text_to_image`，也出图，
      但会先跑沙箱命令，慢；额度受限
    * `grok-4.7` / `grok-4.5` / `grok-4.20-*` —— **403 Model is not found**
    * `grok-3` —— 通过校验但 `modelNotAvailable`

    `download=True` 时顺带取回图片字节（需要 bearer），放进 `images[].content`。
    """
    result = ask(
        prompt, cookies=cookies, bearer=bearer, proxy=proxy, timeout=timeout,
        model=model, image=True, image_count=count, aspect_ratio=aspect_ratio,
        resolution=resolution, image_mode=mode,
        image_gen_config=image_gen_config,
        image_edit_config=image_edit_config,
    )
    # 失败帧（`systemErrCode` 且无 imageUrl）单独收集，不混进成功列表
    failed = [img for img in result["images"]
              if not img.get("image_url") and img.get("system_err_code")]
    images = [img for img in result["images"] if img.get("image_url")]
    # 同一张图会有 progress=50 与 progress=100 两帧，按 asset_id 去重取最终态
    final: dict[str, dict[str, Any]] = {}
    for img in images:
        key = str(img.get("asset_id") or img.get("image_url"))
        prev = final.get(key)
        if prev is None or int(img.get("progress") or 0) >= int(prev.get("progress") or 0):
            final[key] = img
    images = list(final.values())

    if download and images:
        token = str(bearer or "").strip()
        if token:
            for img in images:
                try:
                    img["content"] = download_image(
                        img["image_url"], bearer=token, proxy=proxy
                    )
                except GrokWebError as exc:
                    img["download_error"] = str(exc)

    # 静默降级：HTTP 200、无 error、无图、只有文字。
    # 实测额度耗尽后服务端不再报错，而是把生图请求当普通对话回。
    silent = not images and not failed and not result["rejected"] \
        and not result["rate_limited"] and bool(result["text"].strip())

    return {
        "images": images,
        "failed": failed,
        "rejected": result["rejected"],
        "rate_limited": result["rate_limited"],
        "silent_degrade": silent,
        "warning": IMAGE_SILENT_DEGRADE_HINT if silent else None,
        "text": result["text"],
        "response_id": result["response_id"],
        "conversation_id": result["conversation_id"],
    }


def reconnect(response_id: str, *, cookies: Any = None, bearer: str | None = None,
              proxy: str | None = None,
              timeout: float = 300.0) -> Iterator[dict[str, Any]]:
    """断流后重新挂上同一条流（不会重复生成）。"""
    jar, token = _auth(cookies, bearer)
    if not jar and not token:
        raise GrokWebError("缺少认证：需要 bearer 或 cookies")
    url = f"{WEB_BASE}{RECONNECT_PATH.format(response_id=response_id)}"
    resp = _stream_get(url, headers=build_headers(bearer=token), cookies=jar,
                       proxy=proxy, timeout=timeout)
    with resp:
        if resp.status_code >= 400:
            raise GrokWebError(f"重连失败 HTTP {resp.status_code}",
                               status=resp.status_code)
        for chunk in iter_ndjson(resp.iter_lines()):
            response, _ = unwrap(chunk)
            if response:
                yield response
