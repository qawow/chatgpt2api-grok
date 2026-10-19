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

    前端只在 `messageTag` 为 `UNSET`/`FINAL` 时把文本累加进 `message`；
    其余标签（thinking / summary / tool_usage_card …）走不同的渲染通道。
    服务端已经把该给的部分放进 `message`，所以这里直接取。
    """
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

    return {
        "images": images,
        "failed": failed,
        "rejected": result["rejected"],
        "rate_limited": result["rate_limited"],
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
