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
) -> dict[str, Any]:
    """构造 `/rest/app-chat/conversations/new` 的请求体。

    `search=False` 走 `toolOverrides.webSearch = False` —— **不是** `disableSearch`。
    bundle 里 `disableSearch` 恒为 `false`，从未置 `true`；`buildToolOverrides`
    只在开关关闭时下发 `{webSearch:false}`。
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


def web_credits(
    *,
    bearer: str | None = None,
    cookies: Any = None,
    proxy: str | None = None,
    timeout: float = 60.0,
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

    原因在 JWT 里：`aud` 与 `client_id` 都是 `b1a00492-...`（**Grok CLI 客户端**），
    而该端点只认 **grok.com 网页应用**的 OAuth 客户端。即：

    * 同一 token 在 `/rest/app-chat/*` 上可用（那套走 `conversations:read/write`）
    * 在 `/rest/grok/credits` 上被判 `bad-credentials`

    所以**用 CLI 凭证拿不到网页的生图额度**。可观测的替代只有：
    :func:`free_usage_gates`（实测全 0/0）、:func:`billing`（CLI 账本，
    **不含生图**）、或直接看生图响应的 `renderToolRateLimited` / 静默降级。
    """
    jar, token = _auth(cookies, bearer)
    if not jar and not token:
        raise GrokWebError("缺少认证：需要 bearer 或 cookies")
    resp = _stream_get(
        f"{WEB_BASE}{CREDITS_PATH}",
        headers=build_headers(bearer=token),
        cookies=jar,
        proxy=proxy,
        timeout=timeout,
    )
    with resp:
        if resp.status_code != 200:
            detail = (resp.text or "")[:300]
            raise GrokWebError(
                f"网页额度端点拒绝（{resp.status_code}）：{detail}",
                status=resp.status_code,
                body=detail,
            )
        data = resp.json()
    if not isinstance(data, dict):
        return {}
    return data.get("config") if isinstance(data.get("config"), dict) else data


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


def download_image(
    key: str,
    *,
    bearer: str | None = None,
    proxy: str | None = None,
    timeout: float = 120.0,
) -> bytes:
    """下载 `assets.grok.com` 上的图片。

    **必须带 Bearer** —— 实测无认证返回 403（0 字节），带 Bearer 返回
    200 + `image/jpeg`（351,813 字节，784×1168）。

    `key` 是 :func:`extract_images` 返回的 `image_url`（已是绝对地址）。
    """
    url = asset_url(key)
    if not url:
        raise GrokWebError("缺少图片 key")
    token = str(bearer or "").strip()
    if not token:
        raise GrokWebError("下载图片需要 bearer")
    try:
        resp = _stream_get(
            url,
            headers={"Authorization": f"Bearer {token}",
                     "User-Agent": "grok-cli/1.0.62",
                     "Accept": "image/*,*/*"},
            cookies=None,
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
