import base64
import hashlib
import json
import math
import mimetypes
import re
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from curl_cffi import requests
from fastapi import HTTPException
from services.proxy_service import proxy_settings
from utils.log import logger
from utils.ssrf import UnsafeUrlError, assert_safe_url

# 模型 id 统一定义在 utils/image_models.py；这里仅为兼容旧 import 而再导出。
# 网页图片链路的对外模型名与上游聊天模型解耦：仍发 auto + picture_v2，
# 实际图像版本和档位由服务端选择，不凭请求参数或 UI 文案判定 2.6 已启用。
# 老 id 继续接受，避免打挂在用的客户端，但不再对外列出。
from utils.image_models import (  # noqa: E402
    CODEX_IMAGE_MODEL,
    CODEX_PLAN_PREFIXES as IMAGE_MODEL_PLAN_TYPES,
    LEGACY_WEB_IMAGE_MODELS,
    PREFIXED_CODEX_IMAGE_MODELS,
    WEB_IMAGE_MODEL,
    WEB_IMAGE_MODEL_26,
    all_web_image_models,
)

BASE_IMAGE_MODELS = {WEB_IMAGE_MODEL, WEB_IMAGE_MODEL_26, CODEX_IMAGE_MODEL} | LEGACY_WEB_IMAGE_MODELS
IMAGE_MODELS = BASE_IMAGE_MODELS | PREFIXED_CODEX_IMAGE_MODELS
PUBLIC_IMAGE_MODELS = {WEB_IMAGE_MODEL, WEB_IMAGE_MODEL_26, CODEX_IMAGE_MODEL} | PREFIXED_CODEX_IMAGE_MODELS
OUTPUT_DIR = Path(__file__).resolve().parent / "output"

SUPPORTED_JSON_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
MAX_JSON_IMAGE_BYTES = 10 * 1024 * 1024
MAX_JSON_EDIT_IMAGES = 10
DATA_URL_IMAGE_RE = re.compile(r"^data:(?P<mime>[-+./\w]+);base64,(?P<data>.*)$", re.DOTALL)
REMOTE_IMAGE_TIMEOUT_SECONDS = 20


def _image_extension(mime_type: str) -> str:
    image_type = mime_type.split("/", 1)[1].split(";", 1)[0].lower() if "/" in mime_type else "png"
    return "jpg" if image_type == "jpeg" else image_type or "png"


def _decode_json_image_string(value: str, index: int, filename: str | None = None, mime_type: str | None = None) -> tuple[bytes, str, str]:
    text = value.strip()
    if not text:
        raise HTTPException(status_code=400, detail={"error": "image file is empty"})
    match = DATA_URL_IMAGE_RE.match(text)
    if match:
        resolved_mime = (match.group("mime") or "image/png").lower()
        encoded = match.group("data")
    else:
        if text.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail={"error": "remote image URLs are not supported"})
        resolved_mime = (mime_type or "image/png").lower()
        encoded = text
    if resolved_mime == "image/jpg":
        resolved_mime = "image/jpeg"
    if resolved_mime not in SUPPORTED_JSON_IMAGE_MIME_TYPES:
        raise HTTPException(status_code=400, detail={"error": "unsupported image mime type"})
    try:
        image_data = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid base64 image data"}) from exc
    if not image_data:
        raise HTTPException(status_code=400, detail={"error": "image file is empty"})
    if len(image_data) > MAX_JSON_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail={"error": "image file is too large"})
    return image_data, filename or f"image_{index}.{_image_extension(resolved_mime)}", resolved_mime


def _extract_json_image_value(item: object) -> tuple[str, str | None, str | None]:
    if isinstance(item, str):
        return item, None, None
    if not isinstance(item, dict):
        raise HTTPException(status_code=400, detail={"error": "image entry must be a base64 string or object"})
    filename = str(item.get("filename") or item.get("file_name") or "").strip() or None
    mime_type = str(item.get("mime_type") or item.get("mimeType") or "").strip() or None
    value = item.get("b64_json") or item.get("base64")
    if not value:
        image_url = item.get("image_url") or item.get("url")
        if isinstance(image_url, dict):
            filename = filename or str(image_url.get("filename") or image_url.get("file_name") or "").strip() or None
            mime_type = mime_type or str(image_url.get("mime_type") or image_url.get("mimeType") or "").strip() or None
            value = image_url.get("url") or image_url.get("image_url")
        else:
            value = image_url
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail={"error": "image entry must include image data"})
    return value, filename, mime_type


def normalize_json_edit_images(image: object = None, images: object = None) -> list[tuple[bytes, str, str]]:
    raw_images = images if images is not None else image
    if raw_images is None:
        raise HTTPException(status_code=400, detail={"error": "image file is required"})
    entries = raw_images if isinstance(raw_images, list) else [raw_images]
    if not entries:
        raise HTTPException(status_code=400, detail={"error": "image file is required"})
    if len(entries) > MAX_JSON_EDIT_IMAGES:
        raise HTTPException(status_code=400, detail={"error": f"images supports up to {MAX_JSON_EDIT_IMAGES} items"})
    normalized = []
    for index, item in enumerate(entries, start=1):
        value, filename, mime_type = _extract_json_image_value(item)
        normalized.append(_decode_json_image_string(value, index, filename, mime_type))
    return normalized


def new_uuid() -> str:
    return str(uuid.uuid4())


def split_image_model(model: object) -> tuple[str | None, str | None]:
    normalized = str(model or "").strip().lower()
    if not normalized:
        return None, None
    if normalized in BASE_IMAGE_MODELS or normalized in all_web_image_models():
        return None, normalized
    for plan_type in IMAGE_MODEL_PLAN_TYPES:
        prefix = f"{plan_type}-"
        if normalized.startswith(prefix):
            base_model = normalized[len(prefix):]
            if base_model == CODEX_IMAGE_MODEL:
                return plan_type, base_model
    return None, None


def is_supported_image_model(model: object) -> bool:
    _, base_model = split_image_model(model)
    return base_model is not None


def is_codex_image_model(model: object) -> bool:
    _, base_model = split_image_model(model)
    return base_model == CODEX_IMAGE_MODEL


def is_image_chat_request(body: dict[str, object]) -> bool:
    model = str(body.get("model") or "").strip()
    modalities = body.get("modalities")
    if is_supported_image_model(model):
        return True
    return isinstance(modalities, list) and "image" in {str(item or "").strip().lower() for item in modalities}


_UPSTREAM_BODY_LOG_LIMIT = 500


class UpstreamHTTPError(RuntimeError):
    """Raised when an upstream HTTP call returns a non-2xx status.

    Carries structured fields (status_code, body, retry_after) so callers can
    branch on status code instead of string-matching on str(exc). The full
    body is preserved on the instance; the formatted message truncates it
    to keep log lines reasonable.
    """

    def __init__(
        self,
        context: str,
        status_code: int,
        body: Any,
        retry_after: int | None = None,
    ) -> None:
        self.context = context
        self.status_code = status_code
        self.body = body
        self.retry_after = retry_after
        if isinstance(body, (dict, list)):
            try:
                body_str = json.dumps(body, ensure_ascii=False)
            except (TypeError, ValueError):
                body_str = repr(body)
        else:
            body_str = str(body)
        if len(body_str) > _UPSTREAM_BODY_LOG_LIMIT:
            body_str = body_str[:_UPSTREAM_BODY_LOG_LIMIT] + "…[truncated]"
        super().__init__(f"{context} failed: status={status_code}, body={body_str}")


def ensure_ok(response: requests.Response, context: str) -> None:
    if 200 <= response.status_code < 300:
        return
    body: Any = response.text
    try:
        body = response.json()
    except Exception:
        pass
    retry_after_header = response.headers.get("Retry-After") if hasattr(response, "headers") else None
    retry_after: int | None = None
    if retry_after_header is not None:
        ra_str = str(retry_after_header).strip()
        if ra_str.isdigit():
            retry_after = int(ra_str)
    raise UpstreamHTTPError(context, response.status_code, body, retry_after=retry_after)


def sse_json_stream(items) -> Iterator[str]:
    yield ": stream-open\n\n"
    try:
        for item in items:
            yield f"data: {json.dumps(item, ensure_ascii=True)}\n\n"
    except Exception as exc:
        logger.warning({
            "event": "sse_stream_error",
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        })
        error = exc.to_openai_error() if hasattr(exc, "to_openai_error") else {
            "error": {"message": str(exc), "type": exc.__class__.__name__}
        }
        yield f"data: {json.dumps(error, ensure_ascii=True)}\n\n"
    yield "data: [DONE]\n\n"


def anthropic_sse_stream(items) -> Iterator[str]:
    try:
        for item in items:
            event = str(item.get("type") or "message_delta") if isinstance(item, dict) else "message_delta"
            yield f"event: {event}\n"
            yield f"data: {json.dumps(item, ensure_ascii=True)}\n\n"
    except Exception as exc:
        logger.warning({
            "event": "anthropic_sse_stream_error",
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        })
        error = {"type": "error", "error": {"type": exc.__class__.__name__, "message": str(exc)}}
        yield "event: error\n"
        yield f"data: {json.dumps(error, ensure_ascii=True)}\n\n"


class SseStreamTimeoutError(TimeoutError):
    """Raised when an SSE stream stalls longer than the idle/wall budget."""


@dataclass(frozen=True)
class SseFrame:
    event: str = "message"
    data: str = ""
    id: str = ""


class SseProtocolError(ValueError):
    """An upstream SSE/delta payload cannot be decoded safely."""


class _DeltaUndefined:
    __slots__ = ()

    def __deepcopy__(self, memo):
        return self


_V1_UNDEFINED = _DeltaUndefined()


class V1DeltaDecoder:
    """Decode the web client's v1 deltas into independent JSON channel states.

    Matches Ydv6's short-field expansion and *global* previous-delta defaults.
    Missing values are JS undefined, not an inherited value or JSON null. The
    returned JSON projection omits undefined object members and maps undefined
    array entries / roots to None. A failed delta commits neither state nor
    defaults, including when a later operation in a nested patch fails.
    """

    _FIELDS = (("channel", "c"), ("path", "p"), ("op", "o"), ("value", "v"))
    _INDEX = re.compile(r"(?:0|[1-9][0-9]*)\Z")
    _UNSAFE_KEYS = frozenset(("__proto__", "constructor", "prototype"))
    # Limit allocations caused by sparse indices / growth, not existing input
    # arrays: 100k slots is ample for conversation frames, but not billions.
    _MAX_ARRAY_LENGTH = 100_000

    def __init__(self) -> None:
        self._states: dict[int, Any] = {}
        self._previous_delta = {
            "channel": 0,
            "path": "",
            "op": "add",
            "value": _V1_UNDEFINED,
        }

    def apply_delta(self, value: Any) -> Any:
        if not isinstance(value, dict):
            raise SseProtocolError("v1 delta must be an object")
        raw = deepcopy(value)
        # The actual decoder fills missing SHORT keys before expanding aliases.
        # Therefore an inherited short field also overrides an explicit long one.
        for long_name, short_name in self._FIELDS:
            if long_name != "value" and short_name not in raw:
                raw[short_name] = self._previous_delta[long_name]
        delta = self._expand_fields(raw)
        channel = delta.get("channel")
        if isinstance(channel, bool) or not isinstance(channel, (int, float)):
            raise SseProtocolError("v1 channel must be a non-negative safe integer")
        if isinstance(channel, float) and (not math.isfinite(channel) or not channel.is_integer()):
            raise SseProtocolError("v1 channel must be a non-negative safe integer")
        if channel < 0 or channel > 2**53 - 1:
            raise SseProtocolError("v1 channel must be a non-negative safe integer")
        channel = int(channel)
        delta["channel"] = channel
        root = {"__root": deepcopy(self._states.get(channel, _V1_UNDEFINED))}
        self._apply(root, delta)
        state = root.get("__root", _V1_UNDEFINED)
        result = self._project_json(state)
        self._states[channel] = state
        self._previous_delta = delta
        return result

    @classmethod
    def _expand_fields(cls, raw: dict[str, Any]) -> dict[str, Any]:
        result = dict(raw)
        for long_name, short_name in cls._FIELDS:
            if short_name in raw:
                result[long_name] = raw[short_name]
                result.pop(short_name, None)
        return result

    @classmethod
    def _safe_key(cls, key: Any) -> None:
        if isinstance(key, str) and key in cls._UNSAFE_KEYS:
            raise SseProtocolError("unsafe v1 path or append key")

    @classmethod
    def _path(cls, path: Any) -> list[str | int]:
        if path is None:
            path = ""
        if not isinstance(path, str):
            raise SseProtocolError("v1 path must be a string")
        if path == "":
            return ["__root"]
        if path.startswith("/"):
            path = path[1:]
        parts: list[str | int] = ["__root"]
        for part in path.split("/"):
            decoded = part.replace("~1", "/").replace("~0", "~")
            cls._safe_key(decoded)
            numeric = cls._INDEX.fullmatch(part) is not None
            if numeric and len(part) > 16:
                raise SseProtocolError("v1 numeric path exceeds the safe index range")
            index = int(part) if numeric else None
            if index is not None and index > 2**53 - 1:
                raise SseProtocolError("v1 numeric path exceeds the safe index range")
            parts.append(index if index is not None else decoded)
        return parts

    @staticmethod
    def _array_index(key: str | int) -> int:
        if not isinstance(key, int) or isinstance(key, bool) or key < 0:
            raise SseProtocolError("v1 array path requires a non-negative index")
        return key

    @classmethod
    def _check_growth(cls, array: list[Any], length: int) -> None:
        if length > len(array) and length > cls._MAX_ARRAY_LENGTH:
            raise SseProtocolError("v1 array expansion exceeds the 100000-slot safety limit")

    @classmethod
    def _get(cls, parent: Any, key: str | int) -> Any:
        if isinstance(parent, dict):
            return parent.get(str(key), _V1_UNDEFINED)
        if isinstance(parent, list):
            index = cls._array_index(key)
            return parent[index] if index < len(parent) else _V1_UNDEFINED
        raise SseProtocolError("v1 path traverses a non-container")

    @classmethod
    def _set(cls, parent: Any, key: str | int, value: Any) -> None:
        if isinstance(parent, dict):
            parent[str(key)] = value
        elif isinstance(parent, list):
            index = cls._array_index(key)
            cls._check_growth(parent, index + 1)
            if index >= len(parent):
                parent.extend([_V1_UNDEFINED] * (index + 1 - len(parent)))
            parent[index] = value
        else:
            raise SseProtocolError("v1 path targets a non-container")

    @classmethod
    def _js_string(cls, value: Any) -> str:
        if value is _V1_UNDEFINED:
            return "undefined"
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return ",".join("" if item is None or item is _V1_UNDEFINED else cls._js_string(item) for item in value)
        if isinstance(value, dict):
            return "[object Object]"
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            return str(int(value))
        return str(value)

    @classmethod
    def _js_number(cls, value: Any) -> float:
        if value is _V1_UNDEFINED:
            return math.nan
        if value is None:
            return 0.0
        if isinstance(value, (bool, int, float)):
            try:
                return float(value)
            except OverflowError:
                return math.inf if value > 0 else -math.inf
        if isinstance(value, list):
            return cls._js_number(cls._js_string(value))
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return 0.0
            try:
                if text.lower().startswith(("0x", "0o", "0b")):
                    return float(int(text, 0))
                return float(text)
            except (ValueError, OverflowError):
                return math.nan
        return math.nan

    @classmethod
    def _substring(cls, text: str, value: Any) -> str:
        length = cls._js_number(value)
        if math.isnan(length) or length <= 0:
            return ""
        if math.isinf(length):
            return text
        # JS substring counts UTF-16 code units, not Python Unicode code points.
        encoded = text.encode("utf-16-le", errors="surrogatepass")
        units = int(length)
        if units >= len(encoded) // 2:
            return text
        return encoded[: units * 2].decode("utf-16-le", errors="surrogatepass")

    @classmethod
    def _apply(cls, root: dict[str, Any], delta: dict[str, Any]) -> None:
        parts = cls._path(delta.get("path", ""))
        parent: Any = root
        for position, key in enumerate(parts[:-1]):
            child = cls._get(parent, key)
            if child is _V1_UNDEFINED:
                child = [] if isinstance(parts[position + 1], int) else {}
                cls._set(parent, key, child)
            if not isinstance(child, (dict, list)):
                raise SseProtocolError("v1 path traverses a non-container")
            parent = child
        key = parts[-1]
        current = cls._get(parent, key)
        op = delta.get("op")
        value = delta.get("value", _V1_UNDEFINED)
        if op == "patch":
            if not isinstance(value, list):
                raise SseProtocolError("v1 patch value must be an array of operations")
            patched = {"__root": current}
            for nested in value:
                if not isinstance(nested, dict):
                    raise SseProtocolError("v1 patch operation must be an object")
                # Nested patch operations expand aliases, but do NOT inherit.
                cls._apply(patched, cls._expand_fields(nested))
            cls._set(parent, key, patched.get("__root", _V1_UNDEFINED))
        elif op == "add":
            if isinstance(parent, list):
                index = cls._array_index(key)
                cls._check_growth(parent, len(parent) + 1)
                parent.insert(min(index, len(parent)), value)
            else:
                cls._set(parent, key, value)
        elif op == "remove":
            if isinstance(parent, list):
                index = cls._array_index(key)
                del parent[index : index + 1]
            else:
                parent.pop(str(key), None)
        elif op == "replace":
            cls._set(parent, key, value)
        elif op == "append":
            if isinstance(current, str):
                cls._set(parent, key, current + cls._js_string(value))
            elif isinstance(current, list):
                items = value if isinstance(value, list) else [value]
                cls._check_growth(current, len(current) + len(items))
                current.extend(items)
            elif isinstance(current, dict) and isinstance(value, dict):
                for name in value:
                    cls._safe_key(name)
                current.update(value)
            else:
                cls._set(parent, key, value)
        elif op == "truncate":
            if isinstance(current, str):
                cls._set(parent, key, cls._substring(current, value))
            elif isinstance(current, list):
                length = cls._js_number(value)
                if not math.isfinite(length) or not length.is_integer() or not 0 <= length <= 2**32 - 1:
                    raise SseProtocolError("v1 array length must be a valid unsigned integer")
                size = int(length)
                cls._check_growth(current, size)
                if size < len(current):
                    del current[size:]
                elif size > len(current):
                    current.extend([_V1_UNDEFINED] * (size - len(current)))
        else:
            raise SseProtocolError("unknown v1 delta operation")

    @classmethod
    def _project_json(cls, value: Any) -> Any:
        if value is _V1_UNDEFINED:
            return None
        if isinstance(value, dict):
            return {key: cls._project_json(item) for key, item in value.items() if item is not _V1_UNDEFINED}
        if isinstance(value, list):
            return [cls._project_json(item) for item in value]
        return deepcopy(value)


def iter_sse_frames(
    response: requests.Response,
    *,
    idle_timeout_secs: float | None = None,
    total_timeout_secs: float | None = None,
) -> Iterator[SseFrame]:
    """Yield complete SSE events while bounding reader memory and wait time.

    HTTP response objects, comments and data-less events are not emitted. The
    reader treats every incoming line (including heartbeats) as activity. It
    uses a bounded queue for backpressure and is stopped/closed on exhaustion,
    errors, timeout or generator.close(); join is bounded for broken transports.
    """
    import queue
    import threading

    stop_event = threading.Event()
    reader: threading.Thread | None = None
    try:
        if idle_timeout_secs is None or total_timeout_secs is None:
            try:
                from services.config import config as _cfg  # lazy: avoid import cycles

                if idle_timeout_secs is None:
                    idle_timeout_secs = float(_cfg.image_sse_idle_timeout_secs)
                if total_timeout_secs is None:
                    total_timeout_secs = float(_cfg.image_sse_total_timeout_secs)
            except Exception:
                if idle_timeout_secs is None:
                    idle_timeout_secs = 90.0
                if total_timeout_secs is None:
                    total_timeout_secs = 420.0
        idle_budget = float(idle_timeout_secs)
        total_budget = float(total_timeout_secs)
        if not math.isfinite(idle_budget) or idle_budget <= 0:
            raise ValueError("idle_timeout_secs must be positive and finite")
        if not math.isfinite(total_budget) or total_budget <= 0:
            raise ValueError("total_timeout_secs must be positive and finite")

        line_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=64)
        started = time.monotonic()
        last_activity = started

        def _put(kind: str, payload: object) -> bool:
            while not stop_event.is_set():
                try:
                    line_queue.put((kind, payload), timeout=0.05)
                    return True
                except queue.Full:
                    continue
            return False

        def _reader() -> None:
            nonlocal last_activity
            try:
                for raw_line in response.iter_lines():
                    if stop_event.is_set():
                        return
                    last_activity = time.monotonic()
                    if not _put("line", raw_line):
                        return
                _put("done", None)
            except Exception as exc:
                _put("error", exc)

        reader = threading.Thread(target=_reader, name="sse-reader", daemon=True)
        reader.start()
        data_lines: list[str] = []
        event_name = ""
        event_id = ""
        first_line = True
        while True:
            now = time.monotonic()
            remaining_total = total_budget - (now - started)
            if remaining_total <= 0:
                raise SseStreamTimeoutError(f"SSE stream exceeded total timeout ({total_budget:g}s)")
            try:
                kind, payload = line_queue.get_nowait()
            except queue.Empty:
                remaining_idle = idle_budget - (now - last_activity)
                if remaining_idle <= 0:
                    raise SseStreamTimeoutError(f"SSE stream idle timeout ({idle_budget:g}s) with no data")
                try:
                    kind, payload = line_queue.get(timeout=min(remaining_total, remaining_idle))
                except queue.Empty:
                    continue  # Recheck monotonic deadlines; prefer total if both elapsed.

            if kind == "done":
                return  # An unterminated final event is not a complete SSE frame.
            if kind == "error":
                raise payload  # type: ignore[misc]
            if payload is None:
                continue
            if isinstance(payload, bytes):
                line = payload.decode("utf-8")  # Reject corruption, never silently drop bytes.
            elif isinstance(payload, str):
                line = payload
            else:
                raise SseProtocolError("SSE iter_lines must yield bytes or text")
            if line.endswith("\n"):
                line = line[:-1]
            if line.endswith("\r"):
                line = line[:-1]
            if first_line:
                first_line = False
                if line.startswith("\ufeff"):
                    line = line[1:]
            if line == "":
                if data_lines:
                    frame = SseFrame(event=event_name or "message", data="\n".join(data_lines), id=event_id)
                    data_lines = []
                    event_name = ""
                    yield frame
                else:
                    event_name = ""
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "data":
                data_lines.append(value)
            elif field == "event":
                event_name = value
            elif field == "id" and "\0" not in value:
                event_id = value
    finally:
        stop_event.set()
        try:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        except Exception:
            pass  # Cleanup must not hide the original transport/protocol error.
        if reader is not None and reader.ident is not None:
            reader.join(timeout=0.1)


def iter_sse_payloads(
    response: requests.Response,
    *,
    idle_timeout_secs: float | None = None,
    total_timeout_secs: float | None = None,
) -> Iterator[str]:
    """Compatibility adapter: yield each complete frame's data as a string."""
    frames = iter_sse_frames(
        response,
        idle_timeout_secs=idle_timeout_secs,
        total_timeout_secs=total_timeout_secs,
    )
    try:
        for frame in frames:
            yield frame.data
    finally:
        frames.close()


def save_images_from_text(text: str, prefix: str) -> list[Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    matches = re.findall(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", text or "")
    saved_paths: list[Path] = []
    timestamp = int(time.time() * 1000)
    for index, data_url in enumerate(matches, start=1):
        header, encoded = data_url.split(",", 1)
        image_type = header.split(";")[0].removeprefix("data:image/").strip() or "png"
        extension = "jpg" if image_type == "jpeg" else image_type
        output_path = OUTPUT_DIR / f"{prefix}_{timestamp}_{index}.{extension}"
        output_path.write_bytes(base64.b64decode(encoded))
        saved_paths.append(output_path)
    return saved_paths


def anonymize_token(token: object) -> str:
    value = str(token or "").strip()
    if not value:
        return "token:empty"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"token:{digest}"


def extract_response_prompt(input_value: object) -> str:
    if isinstance(input_value, str):
        return input_value.strip()
    if isinstance(input_value, dict):
        role = str(input_value.get("role") or "").strip().lower()
        if role and role != "user":
            return ""
        return extract_prompt_from_message_content(input_value.get("content"))
    if not isinstance(input_value, list):
        return ""
    prompt_parts: list[str] = []
    for item in input_value:
        if isinstance(item, dict) and str(item.get("type") or "").strip() == "input_text":
            text = str(item.get("text") or "").strip()
            if text:
                prompt_parts.append(text)
            continue
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role and role != "user":
            continue
        prompt = extract_prompt_from_message_content(item.get("content"))
        if prompt:
            prompt_parts.append(prompt)
    return "\n".join(prompt_parts).strip()


def has_response_image_generation_tool(body: dict[str, object]) -> bool:
    tools = body.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and str(tool.get("type") or "").strip() == "image_generation":
                return True
    tool_choice = body.get("tool_choice")
    return isinstance(tool_choice, dict) and str(tool_choice.get("type") or "").strip() == "image_generation"


def extract_prompt_from_message_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip()
        if item_type == "text":
            text = str(item.get("text") or "").strip()
            if text:
                parts.append(text)
        elif item_type == "input_text":
            text = str(item.get("text") or item.get("input_text") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _message_image_url(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("url") or value.get("image_url") or "").strip()
    return str(value or "").strip()


def _decode_message_image_url(value: object) -> tuple[bytes, str] | None:
    source = _message_image_url(value)
    if source.startswith("data:"):
        header, _, data = source.partition(",")
        mime = header.split(";")[0].removeprefix("data:") or "image/png"
        return base64.b64decode(data), mime
    if not source.startswith(("http://", "https://")):
        return None
    parsed = urlparse(source)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    # SSRF protection: message-supplied image URLs must not reach internal
    # addresses, and redirect hops are re-validated before being followed.
    try:
        assert_safe_url(source)
    except UnsafeUrlError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "image_url must not point to a private or local network address"},
        ) from exc
    try:
        response = proxy_settings.get_with_egress_fallback(
            source,
            resource=True,
            upstream=True,
            headers={"Accept": "image/*,*/*;q=0.8", "User-Agent": "chatgpt2api vision fetcher"},
            timeout=REMOTE_IMAGE_TIMEOUT_SECONDS,
            ssrf_guard=True,
        )
    except UnsafeUrlError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "image_url must not point to a private or local network address"},
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"error": f"image_url fetch failed: {exc}"}) from exc
    if not 200 <= response.status_code < 300:
        raise HTTPException(status_code=400, detail={"error": f"image_url fetch failed: HTTP {response.status_code}"})
    content_length = str(response.headers.get("content-length") or "").strip()
    if content_length.isdigit() and int(content_length) > MAX_JSON_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail={"error": "image_url exceeds 10MB limit"})
    image_data = response.content
    if not image_data:
        raise HTTPException(status_code=400, detail={"error": "image_url returned empty content"})
    if len(image_data) > MAX_JSON_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail={"error": "image_url exceeds 10MB limit"})
    mime = str(response.headers.get("content-type") or "image/png").split(";", 1)[0].lower()
    guessed_mime = mimetypes.guess_type(parsed.path)[0] or ""
    if mime and not mime.startswith("image/") and mime not in {"application/octet-stream", "binary/octet-stream"}:
        raise HTTPException(status_code=400, detail={"error": "image_url must point to an image"})
    if not mime.startswith("image/") and guessed_mime.startswith("image/"):
        mime = guessed_mime
    if not mime.startswith("image/"):
        mime = "image/png"
    return image_data, mime


def _decode_message_image_object(item: dict[str, object]) -> tuple[bytes, str] | None:
    data = item.get("data")
    if isinstance(data, (bytes, bytearray)):
        return bytes(data), str(item.get("mime") or item.get("mime_type") or "image/png")
    for key in ("image_url", "url"):
        image = _decode_message_image_url(item.get(key))
        if image:
            return image
    value = item.get("b64_json") or item.get("base64")
    if isinstance(value, str) and value.strip():
        image_data, _, mime = _decode_json_image_string(
            value,
            1,
            mime_type=str(item.get("mime") or item.get("mime_type") or item.get("mimeType") or "image/png"),
        )
        return image_data, mime
    source = item.get("source")
    if isinstance(source, dict) and str(source.get("type") or "") == "base64":
        encoded = str(source.get("data") or "")
        mime = str(source.get("media_type") or source.get("mime_type") or "image/png")
        image_data, _, resolved_mime = _decode_json_image_string(encoded, 1, mime_type=mime)
        return image_data, resolved_mime
    return None


def extract_image_from_message_content(content: object) -> list[tuple[bytes, str]]:
    if not isinstance(content, list):
        return []
    images = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip()
        if item_type == "image_url":
            image = _decode_message_image_url(item.get("image_url") or item.get("url") or item)
            if image:
                images.append(image)
        elif item_type in {"input_image", "image"}:
            image = _decode_message_image_object(item)
            if image:
                images.append(image)
    return images


def extract_chat_image(body: dict[str, object]) -> list[tuple[bytes, str]]:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return []
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        images = extract_image_from_message_content(message.get("content"))
        if images:
            return images
    return []


def extract_chat_prompt(body: dict[str, object]) -> str:
    direct_prompt = str(body.get("prompt") or "").strip()
    if direct_prompt:
        return direct_prompt
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    prompt_parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        prompt = extract_prompt_from_message_content(message.get("content"))
        if prompt:
            prompt_parts.append(prompt)
    return "\n".join(prompt_parts).strip()


def parse_image_count(raw_value: object) -> int:
    try:
        value = coerce_image_count(raw_value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
    if value < 1 or value > 4:
        raise HTTPException(status_code=400, detail={"error": "n must be between 1 and 4"})
    return value


def coerce_image_count(raw_value: object, default: int = 1) -> int:
    """Normalize scalar or singleton-form ``n``; reject ambiguous counts."""
    value: object = raw_value
    if isinstance(value, (list, tuple)):
        if len(value) != 1 or isinstance(value[0], (list, tuple, dict)):
            raise ValueError("n must be a single integer")
        value = value[0]
    # Keep the existing default for omitted/empty/zero form fields.
    if value is None or value == "" or value == 0:
        return default
    if not isinstance(value, (str, int, float)):
        raise ValueError("n must be an integer")
    try:
        count = int(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("n must be an integer") from exc
    if isinstance(value, float) and value != count:
        raise ValueError("n must be an integer")
    return count


def image_result_urls(image_result: dict[str, object]) -> list[str]:
    """Stored (http/relative) URLs of an image result, for call logs.

    Chat completions / responses embed images inline as base64, so the public
    payload has no ``url`` for the log collector to find even though every image
    was already saved by format_image_result. Handlers attach this list as the
    internal ``_image_urls`` field (stripped before the client sees it).
    """
    items = image_result.get("data") if isinstance(image_result.get("data"), list) else []
    urls: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if url and not url.startswith("data:") and url not in urls:
            urls.append(url)
    return urls


def build_chat_image_markdown_content(image_result: dict[str, object]) -> str:
    image_items = image_result.get("data") if isinstance(image_result.get("data"), list) else []
    markdown_images: list[str] = []
    for index, item in enumerate(image_items, start=1):
        if not isinstance(item, dict):
            continue
        b64_json = str(item.get("b64_json") or "").strip()
        if b64_json:
            mime_type = "image/png"
            try:
                from utils.image_media import sniff_image_media

                detected_mime, _ = sniff_image_media(base64.b64decode(b64_json, validate=True))
                if detected_mime.startswith("image/"):
                    mime_type = detected_mime
            except (ValueError, TypeError):
                pass  # Unknown media / bad base64 keeps the legacy PNG declaration.
            markdown_images.append(f"![image_{index}](data:{mime_type};base64,{b64_json})")
    return "\n\n".join(markdown_images) if markdown_images else "Image generation completed."
