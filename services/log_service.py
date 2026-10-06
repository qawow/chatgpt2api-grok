from __future__ import annotations

import base64
import binascii
import hashlib
import itertools
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from services.config import DATA_DIR
from services.protocol.error_response import anthropic_error_response, openai_error_response
from utils.helper import anthropic_sse_stream, sse_json_stream
from utils.log_safety import redact_text, sanitize_log_value
from utils.network_diagnostics import error_details

LOG_TYPE_CALL = "call"
LOG_TYPE_ACCOUNT = "account"
# Fields handlers attach for logging only; stripped before anything reaches the
# client. _image_urls carries the stored image URLs for endpoints whose public
# payload embeds images inline (chat/completions and responses return base64
# markdown / image_generation_call results, which have no "url" key to collect).
INTERNAL_RESPONSE_KEYS = {"_account_email", "_conversation_id", "_image_urls"}


class LogCursorError(Exception):
    """The log snapshot or filters changed; the caller should refresh the list."""


@dataclass(frozen=True)
class _LogSnapshot:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    digest: str


def _parse_log_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _event_times(item: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    wall = _parse_log_datetime(item.get("time"))
    instant = _parse_log_datetime(item.get("occurred_at"))
    if instant is None or instant.tzinfo is None:
        instant = wall if wall is not None and wall.tzinfo is not None else None
    if wall is not None and instant is not None and len(str(item.get("time", ""))) == 19:
        wall = wall.replace(microsecond=instant.microsecond)
    return wall or instant, instant


def _date_bound(value: str) -> tuple[datetime, bool] | None:
    if not value:
        return None
    parsed = _parse_log_datetime(value)
    if parsed is None:
        raise ValueError("Log dates must be ISO dates or datetimes")
    return parsed, len(value) == 10 and value[4] == "-" and value[7] == "-"


def _with_error_diagnostics(value: Any) -> Any:
    if isinstance(value, dict):
        result = {key: _with_error_diagnostics(item) for key, item in value.items()}
        error = next((value[key] for key in (
            "error", "error_message", "exception", "last_error", "last_refresh_error", "last_token_refresh_error",
        ) if value.get(key)), None)
        if error is not None:
            for key, item in error_details(error).items():
                if key in {"failure_kind", "http_status", "curl_code"}:
                    result.setdefault(key, item)
        return result
    if isinstance(value, (list, tuple)):
        return [_with_error_diagnostics(item) for item in value]
    return value


def _sanitize_log_item(item: dict[str, Any]) -> dict[str, Any]:
    parsed = _with_error_diagnostics(item)
    summary = parsed.get("summary")
    if isinstance(summary, str) and ("<html" in summary.lower() or "<!doctype html" in summary.lower()):
        if isinstance(parsed.get("detail"), dict):
            for key, value in error_details(summary).items():
                if key in {"failure_kind", "http_status", "curl_code"}:
                    parsed["detail"].setdefault(key, value)
        parsed["summary"] = sanitize_log_value({"error": summary})["error"]
    if "summary" in parsed:
        parsed["summary"] = redact_text(parsed["summary"], limit=1000)
    return sanitize_log_value(parsed)


class LogService:
    _BLOCK_SIZE = 64 * 1024
    _ANCHOR_SIZE = 4096

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @staticmethod
    def _legacy_id(raw_line: str, line_number: int) -> str:
        payload = f"{line_number}:{raw_line}".encode("utf-8", errors="ignore")
        return hashlib.sha1(payload).hexdigest()[:24]

    @staticmethod
    def _decode_line(raw_line: str) -> dict[str, Any] | None:
        try:
            item = json.loads(raw_line)
        except (ValueError, RecursionError):
            return None
        return item if isinstance(item, dict) else None

    def _prepare_item(self, item: dict[str, Any], raw_line: str, line_number: int | None) -> dict[str, Any]:
        parsed = dict(item)
        if parsed.get("id"):
            parsed["id"] = str(parsed["id"])
        else:
            if line_number is None:
                raise ValueError("A legacy log ID requires its physical line number")
            parsed["id"] = self._legacy_id(raw_line, line_number)
        _, instant = _event_times(parsed)
        parsed["time_basis"] = "offset_aware" if instant is not None else "local_timezone_unknown"
        return _sanitize_log_item(parsed)

    def _parse_line(self, raw_line: str, line_number: int) -> dict[str, Any] | None:
        item = self._decode_line(raw_line)
        return self._prepare_item(item, raw_line, line_number) if item is not None else None

    @staticmethod
    def _serialize_item(item: dict[str, Any]) -> str:
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _matches_filters(item: dict[str, Any], *, type: str = "", start_date: str = "", end_date: str = "") -> bool:
        return LogService._matches_bounds(item, type, _date_bound(start_date), _date_bound(end_date))

    @staticmethod
    def _matches_bounds(item: dict[str, Any], type: str, start: tuple | None, end: tuple | None) -> bool:
        if type and item.get("type") != type:
            return False
        wall, instant = _event_times(item)
        for bound, is_start in ((start, True), (end, False)):
            if bound is None:
                continue
            requested, day_only = bound
            if day_only:
                if wall is None:
                    return False
                actual, expected = wall.date(), requested.date()
            elif requested.tzinfo is None:
                if wall is None:
                    return False
                actual, expected = wall.replace(tzinfo=None), requested
            else:
                # A historic local wall clock has no known UTC interpretation.
                if instant is None:
                    return False
                actual, expected = instant, requested
            if (is_start and actual < expected) or (not is_start and actual > expected):
                return False
        return True

    def add(self, type: str, summary: str = "", detail: dict[str, Any] | None = None, **data: Any) -> None:
        now = datetime.now(timezone.utc)
        item = {
            "id": uuid4().hex,
            "time": now.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "occurred_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "timestamp_ms": int(now.timestamp() * 1000),
            "time_basis": "offset_aware",
            "type": type,
            "summary": summary,
            "detail": detail if detail is not None else data,
        }
        line = (self._serialize_item(_sanitize_log_item(item)) + "\n").encode("utf-8")
        with self._lock:
            fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a+b") as file:
                if os.fstat(file.fileno()).st_mode & 0o777 != 0o600:
                    os.fchmod(file.fileno(), 0o600)
                if file.seek(0, os.SEEK_END):
                    file.seek(-1, os.SEEK_END)
                    if file.read(1) != b"\n":
                        line = b"\n" + line
                file.write(line)

    def list(self, type: str = "", start_date: str = "", end_date: str = "", limit: int = 200) -> list[dict[str, Any]]:
        return self.list_page(type, start_date, end_date, limit)["items"]

    @staticmethod
    def _read_at(file: BinaryIO, offset: int, size: int) -> bytes:
        file.seek(offset)
        content = file.read(size)
        if len(content) != size:
            raise LogCursorError("Log snapshot changed; refresh the log list")
        return content

    def _snapshot_digest(self, file: BinaryIO, size: int) -> str:
        # Append-only storage uses inode/size plus fixed prefix/tail anchors.
        # Mutations managed here replace the inode; appended bytes are ignored.
        digest = hashlib.sha256(str(size).encode("ascii"))
        amount = min(size, self._ANCHOR_SIZE)
        digest.update(self._read_at(file, 0, amount))
        if size > amount:
            digest.update(self._read_at(file, max(amount, size - self._ANCHOR_SIZE), min(size - amount, self._ANCHOR_SIZE)))
        return digest.hexdigest()

    def _snapshot(self, file: BinaryIO) -> _LogSnapshot:
        stat = os.fstat(file.fileno())
        return _LogSnapshot(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                            self._snapshot_digest(file, stat.st_size))

    def _validate_snapshot(self, file: BinaryIO, snapshot: _LogSnapshot) -> None:
        try:
            current = os.fstat(file.fileno())
            path_stat = self.path.stat()
        except FileNotFoundError as exc:
            raise LogCursorError("Log file changed; refresh the log list") from exc
        identity = (snapshot.dev, snapshot.ino)
        if ((current.st_dev, current.st_ino) != identity
                or (path_stat.st_dev, path_stat.st_ino) != identity
                or current.st_size < snapshot.size
                or (current.st_size == snapshot.size
                    and (current.st_mtime_ns != snapshot.mtime_ns or current.st_ctime_ns != snapshot.ctime_ns))
                or self._snapshot_digest(file, snapshot.size) != snapshot.digest):
            raise LogCursorError("Log snapshot changed; refresh the log list")

    @staticmethod
    def _encode_cursor(snapshot: _LogSnapshot, offset: int, line_number: int | None, filters: str) -> str:
        payload = {"v": 1, "snapshot": vars(snapshot), "offset": offset, "line_number": line_number, "filters": filters}
        return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[_LogSnapshot, int, int | None, str]:
        try:
            if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
                raise ValueError
            if any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in cursor):
                raise ValueError
            raw = cursor.encode("ascii")
            decoded = base64.b64decode(raw + b"=" * (-len(raw) % 4), altchars=b"-_", validate=True)
            if base64.urlsafe_b64encode(decoded).rstrip(b"=") != raw:
                raise ValueError
            payload = json.loads(decoded)
            if not isinstance(payload, dict) or set(payload) != {"v", "snapshot", "offset", "line_number", "filters"}:
                raise ValueError
            if not isinstance(payload["v"], int) or isinstance(payload["v"], bool) or payload["v"] != 1:
                raise ValueError
            data = payload["snapshot"]
            fields = {"dev", "ino", "size", "mtime_ns", "ctime_ns", "digest"}
            if not isinstance(data, dict) or set(data) != fields:
                raise ValueError
            if any(not isinstance(data[key], int) or isinstance(data[key], bool) or data[key] < 0 for key in fields - {"digest"}):
                raise ValueError
            for digest in (data["digest"], payload["filters"]):
                if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                    raise ValueError
            offset, line_number = payload["offset"], payload["line_number"]
            if not isinstance(offset, int) or isinstance(offset, bool) or not 0 < offset <= data["size"]:
                raise ValueError
            if line_number is not None and (not isinstance(line_number, int) or isinstance(line_number, bool)
                                            or not 0 <= line_number <= data["size"]):
                raise ValueError
            return _LogSnapshot(**data), offset, line_number, payload["filters"]
        except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError) as exc:
            raise ValueError("Malformed log cursor") from exc

    def _reverse_lines(self, file: BinaryIO, upper: int) -> Iterator[tuple[int, int, bytes]]:
        position = line_end = upper
        fragments: list[bytes] = []
        while position:
            amount = min(position, self._BLOCK_SIZE)
            position -= amount
            block = self._read_at(file, position, amount)
            right = len(block)
            while True:
                at = block.rfind(b"\n", 0, right)
                if at < 0:
                    fragments.append(block[:right])
                    break
                fragments.append(block[at + 1:right])
                raw_line = b"".join(reversed(fragments))
                fragments.clear()
                line_start = position + at + 1
                if line_start < upper:
                    yield line_start, line_end, raw_line.removesuffix(b"\r")
                line_end = position + at + 1
                right = at
        if upper:
            yield 0, line_end, b"".join(reversed(fragments)).removesuffix(b"\r")

    def _line_number_before(self, file: BinaryIO, offset: int) -> int:
        # Only legacy rows need this once per pagination chain. Modern IDs keep
        # the common path proportional to the tail scanned, not the file size.
        position = count = 0
        while position < offset:
            amount = min(offset - position, self._BLOCK_SIZE)
            count += self._read_at(file, position, amount).count(b"\n")
            position += amount
        return count

    def list_page(self, type: str = "", start_date: str = "", end_date: str = "", limit: int = 200,
                  cursor: str = "") -> dict[str, Any]:
        """Read newest-first within a fixed append-only file snapshot.

        Date-only bounds include the entire displayed local day. Naive ISO
        datetimes compare displayed wall clocks; offset-aware bounds compare
        actual instants and exclude legacy rows with unknown time zones.
        Appends are visible after refreshing, not partway through this cursor.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("Log limit must be a positive integer")
        if any(not isinstance(value, str) for value in (type, start_date, end_date, cursor)):
            raise ValueError("Log filters and cursor must be strings")
        type, start_date, end_date = type.strip(), start_date.strip(), end_date.strip()
        start, end = _date_bound(start_date), _date_bound(end_date)
        filters = hashlib.sha256(json.dumps([type, start_date, end_date]).encode("utf-8")).hexdigest()
        state = self._decode_cursor(cursor) if cursor else None
        if state is not None and state[3] != filters:
            raise LogCursorError("Log filters changed; refresh the log list")
        file = None
        try:
            with self._lock:
                try:
                    file = self.path.open("rb")
                except FileNotFoundError as exc:
                    if state is not None:
                        raise LogCursorError("Log file changed; refresh the log list") from exc
                    return {"items": [], "has_more": False, "next_cursor": ""}
                if state is None:
                    snapshot = self._snapshot(file)
                    upper, line_number = snapshot.size, None
                else:
                    snapshot, upper, line_number, _ = state
                    self._validate_snapshot(file, snapshot)
                    if upper < snapshot.size and self._read_at(file, upper - 1, 1) != b"\n":
                        raise ValueError("Malformed log cursor offset")
            items: list[dict[str, Any]] = []
            next_cursor = ""
            for offset, line_end, raw_bytes in self._reverse_lines(file, upper):
                current_number = line_number
                if line_number is not None:
                    line_number -= 1
                try:
                    raw_line = raw_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                item = self._decode_line(raw_line)
                if item is None or not self._matches_bounds(item, type, start, end):
                    continue
                if len(items) == limit:
                    next_cursor = self._encode_cursor(snapshot, line_end, current_number, filters)
                    break
                if not item.get("id") and current_number is None:
                    current_number = self._line_number_before(file, offset)
                    line_number = current_number - 1
                items.append(self._prepare_item(item, raw_line, current_number))
            with self._lock:
                self._validate_snapshot(file, snapshot)
            return {"items": items, "has_more": bool(next_cursor), "next_cursor": next_cursor}
        finally:
            if file is not None:
                file.close()

    def delete(self, ids: list[str]) -> dict[str, int]:
        target_ids = {str(item or "").strip() for item in ids if str(item or "").strip()}
        if not target_ids:
            return {"removed": 0}
        result = self._rewrite(lambda item: item is None or item["id"] not in target_ids)
        return {"removed": result["removed"]}

    def _rewrite(self, keep: Callable[[dict[str, Any] | None], bool]) -> dict[str, int]:
        removed = kept = 0
        with self._lock:
            if not self.path.exists():
                return {"removed": 0, "kept": 0}
            fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as dst, self.path.open("rb") as src:
                    for line_number, raw_bytes in enumerate(src):
                        try:
                            raw_line = raw_bytes.removesuffix(b"\n").removesuffix(b"\r").decode("utf-8")
                            item = self._parse_line(raw_line, line_number)
                        except UnicodeDecodeError:
                            item = None
                        if keep(item):
                            # Persist legacy IDs before removals change their line numbers.
                            dst.write((self._serialize_item(item) + "\n").encode("utf-8") if item is not None else raw_bytes)
                            kept += 1
                        else:
                            removed += 1
                    dst.flush()
                    os.fsync(dst.fileno())
                if removed:
                    os.replace(tmp_name, self.path)
            finally:
                Path(tmp_name).unlink(missing_ok=True)
        return {"removed": removed, "kept": kept}

    def trim(self, retention_days: int) -> dict[str, int]:
        """Stream retention cleanup to an owner-only atomic replacement."""
        if retention_days <= 0:
            return {"removed": 0, "kept": 0}
        cutoff = datetime.now().astimezone() - timedelta(days=int(retention_days))
        try:
            return self._rewrite(lambda item: item is None or _item_within_retention(item, cutoff))
        except OSError:
            return {"removed": 0, "kept": 0}


log_service = LogService(DATA_DIR / "logs.jsonl")


def _line_within_retention(raw_line: str, cutoff_key: str) -> bool:
    item = LogService._decode_line(raw_line)
    cutoff = _parse_log_datetime(cutoff_key)
    return item is None or cutoff is None or _item_within_retention(item, cutoff)


def _item_within_retention(item: dict[str, Any], cutoff: datetime) -> bool:
    wall, instant = _event_times(item)
    if instant is not None and cutoff.tzinfo is not None:
        return instant >= cutoff
    if wall is None:
        return True
    return wall.replace(tzinfo=None) >= cutoff.replace(tzinfo=None)


def _collect_urls(value: object) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "url" and isinstance(item, str):
                urls.append(item)
            elif key in {"urls", "_image_urls"} and isinstance(item, list):
                urls.extend(str(url) for url in item if isinstance(url, str))
            else:
                urls.extend(_collect_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_collect_urls(item))
    return urls


def _collect_account_emails(value: object) -> list[str]:
    emails: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"_account_email", "account_email"} and isinstance(item, str) and item.strip():
                emails.append(item.strip())
            else:
                emails.extend(_collect_account_emails(item))
    elif isinstance(value, list):
        for item in value:
            emails.extend(_collect_account_emails(item))
    return emails


def _collect_conversation_ids(value: object) -> list[str]:
    ids: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "_conversation_id" and isinstance(item, str) and item.strip():
                ids.append(item.strip())
            else:
                ids.extend(_collect_conversation_ids(item))
    elif isinstance(value, list):
        for item in value:
            ids.extend(_collect_conversation_ids(item))
    return ids


def _strip_internal_response_fields(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _strip_internal_response_fields(item)
            for key, item in value.items()
            if key not in INTERNAL_RESPONSE_KEYS
        }
    if isinstance(value, list):
        return [_strip_internal_response_fields(item) for item in value]
    return value


def _request_excerpt(text: object, limit: int = 1000) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _image_error_response(exc: Exception) -> JSONResponse:
    from services.protocol.conversation import public_image_error_message

    message = public_image_error_message(str(exc))
    if "no available image quota" in message.lower():
        return openai_error_response(
            {
                "error": {
                    "message": "no available image quota",
                    "type": "insufficient_quota",
                    "param": None,
                    "code": "insufficient_quota",
                }
            },
            429,
        )
    if hasattr(exc, "to_openai_error") and hasattr(exc, "status_code"):
        return JSONResponse(
            status_code=int(exc.status_code),
            content=exc.to_openai_error(),
            headers=getattr(exc, "headers", None),
        )
    return openai_error_response(message, 502)


def _protocol_error_response(exc: Exception, status_code: int, sse: str) -> JSONResponse:
    message = str(exc)
    if sse == "anthropic":
        return anthropic_error_response(message, status_code)
    return openai_error_response(message, status_code)


def _next_item(items):
    try:
        return True, next(items)
    except StopIteration:
        return False, None


@dataclass
class LoggedCall:
    identity: dict[str, object]
    endpoint: str
    model: str
    summary: str
    started: float = field(default_factory=time.time)
    request_text: str = ""
    request_shape: dict[str, int] | None = None

    async def run(self, handler, *args, sse: str = "openai"):
        from services.protocol.conversation import ImageGenerationError

        try:
            result = await run_in_threadpool(handler, *args)
        except ImageGenerationError as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""),
                     conversation_id=getattr(exc, "conversation_id", ""))
            return _image_error_response(exc)
        except HTTPException as exc:
            self.log("调用失败", status="failed", error=str(exc.detail))
            raise
        except Exception as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""))
            if self.endpoint.startswith("/v1/images"):
                return _image_error_response(exc)
            return _protocol_error_response(exc, 502, sse)

        if isinstance(result, dict):
            self.log("调用完成", result)
            return _strip_internal_response_fields(result)

        sender = anthropic_sse_stream if sse == "anthropic" else sse_json_stream
        try:
            has_first, first = await run_in_threadpool(_next_item, result)
        except ImageGenerationError as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""),
                     conversation_id=getattr(exc, "conversation_id", ""))
            return _image_error_response(exc)
        except HTTPException as exc:
            self.log("调用失败", status="failed", error=str(exc.detail))
            raise
        except Exception as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""))
            if self.endpoint.startswith("/v1/images"):
                return _image_error_response(exc)
            return _protocol_error_response(exc, 502, sse)
        if not has_first:
            self.log("流式调用结束")
            return StreamingResponse(sender(()), media_type="text/event-stream")
        return StreamingResponse(sender(self.stream(itertools.chain([first], result))), media_type="text/event-stream")

    def stream(self, items):
        urls: list[str] = []
        account_emails: list[str] = []
        conversation_ids: list[str] = []
        failed = False
        try:
            for item in items:
                urls.extend(_collect_urls(item))
                account_emails.extend(_collect_account_emails(item))
                conversation_ids.extend(_collect_conversation_ids(item))
                yield _strip_internal_response_fields(item)
        except Exception as exc:
            failed = True
            self.log(
                "流式调用失败",
                status="failed",
                error=str(exc),
                urls=urls,
                account_email=(account_emails[0] if account_emails else getattr(exc, "account_email", "")),
                conversation_id=(conversation_ids[0] if conversation_ids else getattr(exc, "conversation_id", "")),
            )
            if self.endpoint.startswith("/v1/images") and not hasattr(exc, "to_openai_error"):
                from services.protocol.conversation import ImageGenerationError, public_image_error_message

                raise ImageGenerationError(public_image_error_message(str(exc))) from exc
            raise
        finally:
            if not failed:
                self.log("流式调用结束", urls=urls, account_email=account_emails[0] if account_emails else "",
                         conversation_id=conversation_ids[0] if conversation_ids else "")

    def log(self, suffix: str, result: object = None, status: str = "success", error: str = "",
            urls: list[str] | None = None, account_email: str = "", conversation_id: str = "") -> None:
        detail = {
            "key_id": self.identity.get("id"),
            "key_name": self.identity.get("name"),
            "role": self.identity.get("role"),
            "endpoint": self.endpoint,
            "model": self.model,
            "started_at": datetime.fromtimestamp(self.started).strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "duration_ms": int((time.time() - self.started) * 1000),
            "status": status,
        }
        request_excerpt = _request_excerpt(self.request_text)
        if request_excerpt:
            detail["request_text"] = request_excerpt
        if self.request_shape:
            detail["request_shape"] = self.request_shape
        if error:
            detail["error"] = error
        email = str(account_email or "").strip()
        if not email:
            emails = _collect_account_emails(result)
            email = emails[0] if emails else ""
        if email:
            detail["account_email"] = email
        conv_id = str(conversation_id or "").strip()
        if not conv_id:
            conv_ids = _collect_conversation_ids(result)
            conv_id = conv_ids[0] if conv_ids else ""
        if conv_id:
            detail["conversation_id"] = conv_id
        collected_urls = [*(urls or []), *_collect_urls(result)]
        if collected_urls and not self.endpoint.startswith("/v1/search"):
            detail["urls"] = list(dict.fromkeys(collected_urls))
        log_service.add(LOG_TYPE_CALL, f"{self.summary}{suffix}", detail)
