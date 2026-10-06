from __future__ import annotations

import base64
import copy
import json
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from services.log_service import LogCursorError, LogService, _line_within_retention


@pytest.fixture
def service(tmp_path: Path) -> LogService:
    return LogService(tmp_path / "logs.jsonl")


def _row(index: int, **fields: object) -> dict:
    return {
        "id": f"row-{index}",
        "type": "call",
        "time": "2026-10-06 10:00:00",
        "summary": f"event-{index}",
        "detail": {"index": index},
        **fields,
    }


def _write(service: LogService, rows: list[dict], *, trailing_newline: bool = True) -> None:
    content = "\n".join(service._serialize_item(row) for row in rows)
    service.path.write_text(content + ("\n" if trailing_newline and rows else ""), encoding="utf-8")


def _drain(service: LogService, **filters: object) -> list[dict]:
    items, seen = [], set()
    cursor = ""
    while True:
        page = service.list_page(cursor=cursor, **filters)
        items.extend(page["items"])
        if not page["has_more"]:
            assert page["next_cursor"] == ""
            return items
        cursor = page["next_cursor"]
        assert page["items"] and cursor and cursor not in seen
        seen.add(cursor)


def test_default_200_same_second_pagination(service: LogService) -> None:
    rows = [_row(index) for index in range(503)]
    _write(service, rows)
    first = service.list_page()
    assert len(first["items"]) == 200
    assert first["has_more"]
    assert first["items"] == service.list()
    actual = _drain(service)
    assert [item["id"] for item in actual] == [item["id"] for item in reversed(rows)]
    assert len({item["id"] for item in actual}) == 503


@pytest.mark.parametrize("trailing_newline", [True, False])
def test_appends_preserve_original_snapshot(service: LogService, trailing_newline: bool) -> None:
    rows = [_row(index) for index in range(11)]
    _write(service, rows, trailing_newline=trailing_newline)
    first = service.list_page(limit=3)
    for index in range(5):
        service.add("call", f"appended-{index}")
    actual = first["items"][:]
    cursor = first["next_cursor"]
    while cursor:
        page = service.list_page(limit=2, cursor=cursor)
        actual.extend(page["items"])
        cursor = page["next_cursor"]
    assert [item["id"] for item in actual] == [item["id"] for item in reversed(rows)]
    assert len(service.list(limit=30)) == 16
    assert service.list()[0]["summary"] == "appended-4"


def test_reader_releases_lock_during_concurrent_append(service: LogService) -> None:
    _write(service, [_row(index) for index in range(10)])
    iterator = service._reverse_lines
    entered, written = threading.Event(), threading.Event()
    failures = []

    def append() -> None:
        if not entered.wait(3):
            failures.append("reader did not enter")
            return
        try:
            service.add("account", "concurrent")
        except Exception as exc:
            failures.append(exc)
        finally:
            written.set()

    def rows(file, upper):
        assert not service._lock.locked()
        entered.set()
        assert written.wait(3), "the reader held the write lock"
        yield from iterator(file, upper)

    writer = threading.Thread(target=append)
    writer.start()
    try:
        with mock.patch.object(service, "_reverse_lines", side_effect=rows):
            first = service.list_page(limit=4)
    finally:
        writer.join(4)
    assert not writer.is_alive() and not failures
    actual = first["items"] + service.list_page(limit=20, cursor=first["next_cursor"])["items"]
    assert [item["id"] for item in actual] == [f"row-{index}" for index in reversed(range(10))]
    assert service.list()[0]["summary"] == "concurrent"


def test_filters_apply_before_limit_and_has_more(service: LogService) -> None:
    rows = [_row(index, type="account" if index % 3 == 0 else "call") for index in range(31)]
    _write(service, rows)
    page = service.list_page(type="account", limit=3)
    assert page["has_more"]
    assert [item["id"] for item in _drain(service, type="account", limit=3)] == [
        row["id"] for row in reversed(rows) if row["type"] == "account"
    ]
    assert service.list_page(type="missing") == {"items": [], "has_more": False, "next_cursor": ""}
    _write(service, [_row(0, type="account"), _row(1), _row(2)])
    assert service.list_page(type="call", limit=2)["has_more"] is False


@pytest.mark.parametrize("changed", [{"type": "account"}, {"start_date": "2026-10-05"}, {"end_date": "2026-10-07"}])
def test_changed_filters_reject_cursor(service: LogService, changed: dict) -> None:
    _write(service, [_row(index) for index in range(3)])
    cursor = service.list_page(limit=1)["next_cursor"]
    with pytest.raises(LogCursorError):
        service.list_page(cursor=cursor, **changed)
    assert not issubclass(LogCursorError, ValueError)


@pytest.mark.parametrize("mutation", ["delete", "trim", "replace", "unlink", "truncate", "rewrite"])
def test_file_mutations_stale_cursor(service: LogService, mutation: str) -> None:
    recent = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _write(service, [_row(0, time="2000-01-01 00:00:00"), _row(1, time=recent), _row(2, time=recent)])
    cursor = service.list_page(limit=1)["next_cursor"]
    if mutation == "delete":
        assert service.delete(["row-1"]) == {"removed": 1}
    elif mutation == "trim":
        assert service.trim(30) == {"removed": 1, "kept": 2}
    elif mutation == "replace":
        replacement = service.path.with_suffix(".replacement")
        replacement.write_bytes(service.path.read_bytes())
        replacement.replace(service.path)
    elif mutation == "unlink":
        service.path.unlink()
    elif mutation == "truncate":
        service.path.write_bytes(b"")
    else:
        service.path.write_bytes(service.path.read_bytes().replace(b"event-1", b"other-1"))
    with pytest.raises(LogCursorError):
        service.list_page(cursor=cursor)


def test_noop_mutations_preserve_cursor_and_original_bytes(service: LogService) -> None:
    _write(service, [_row(index, time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")) for index in range(3)])
    cursor = service.list_page(limit=1)["next_cursor"]
    before, inode = service.path.read_bytes(), service.path.stat().st_ino
    assert service.delete(["missing"]) == {"removed": 0}
    assert service.trim(30) == {"removed": 0, "kept": 3}
    assert service.path.read_bytes() == before and service.path.stat().st_ino == inode
    assert len(service.list_page(cursor=cursor)["items"]) == 2


@pytest.mark.parametrize("trailing_newline", [True, False])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_legacy_ids_utf8_blocks_and_delete(service: LogService, trailing_newline: bool, newline: str) -> None:
    service._BLOCK_SIZE = 17
    raws = [
        service._serialize_item({"type": "call", "summary": "\u4e2d\u6587" * 20, "time": "2026-10-06 10:00:00"}),
        "malformed {",
        "",
        "[]",
        service._serialize_item({"type": "account", "summary": "legacy", "time": "2026-10-06 10:00:00"}),
        service._serialize_item({"type": "call", "summary": "\u5c3e\u90e8", "time": "2026-10-06 10:00:00"}),
    ]
    service.path.write_bytes((newline.join(raws) + (newline if trailing_newline else "")).encode("utf-8"))
    expected = [service._legacy_id(raws[index], index) for index in (5, 4, 0)]
    with mock.patch.object(service, "_line_number_before", wraps=service._line_number_before) as counter:
        actual = _drain(service, limit=1)
    assert counter.call_count == 1
    assert [item["id"] for item in actual] == expected
    assert actual[-1]["summary"] == "\u4e2d\u6587" * 20
    assert all(item["time_basis"] == "local_timezone_unknown" for item in actual)
    assert all("occurred_at" not in item and "timestamp_ms" not in item for item in actual)
    assert service.delete([expected[1]]) == {"removed": 1}
    assert [item["id"] for item in service.list()] == [expected[0], expected[2]]
    assert service.delete([expected[0]]) == {"removed": 1}
    assert service.list()[0]["id"] == expected[2]


def test_invalid_utf8_and_nonobject_json_are_skipped(service: LogService) -> None:
    content = [service._serialize_item(_row(0)).encode(), b"\xff\xfe", b"null", b"true", b"{broken", service._serialize_item(_row(1)).encode()]
    service.path.write_bytes(b"\n".join(content))
    assert [item["id"] for item in _drain(service, limit=1)] == ["row-1", "row-0"]


@pytest.mark.parametrize("cursor", ["%not-base64", "e30", "bnVsbA", "W10", "\u4e2d\u6587", "a" * 5000, None, 1])
def test_malformed_cursor_is_value_error(service: LogService, cursor: object) -> None:
    with pytest.raises(ValueError):
        service.list_page(cursor=cursor)


def test_cursor_fields_and_midline_offset_are_validated(service: LogService) -> None:
    _write(service, [_row(index) for index in range(3)])
    cursor = service.list_page(limit=1)["next_cursor"]
    payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    for field, value in [("offset", True), ("offset", -1), ("offset", 2), ("v", 2), ("line_number", -1), ("snapshot", {}), ("filters", "x")]:
        changed = {**payload, field: value}
        invalid = base64.urlsafe_b64encode(json.dumps(changed).encode()).decode().rstrip("=")
        with pytest.raises(ValueError):
            service.list_page(cursor=invalid)


@pytest.mark.parametrize("limit", [0, -1, -200, 1.5, "2", None, True])
def test_nonpositive_or_noninteger_limits_fail(service: LogService, limit: object) -> None:
    _write(service, [_row(0)])
    with pytest.raises(ValueError):
        service.list(limit=limit)
    with pytest.raises(ValueError):
        service.list_page(limit=limit)


def test_empty_or_missing_file_has_empty_terminal_page(service: LogService) -> None:
    assert service.list_page() == {"items": [], "has_more": False, "next_cursor": ""}
    service.path.touch()
    assert service.list_page() == {"items": [], "has_more": False, "next_cursor": ""}


def test_day_bounds_include_whole_day(service: LogService) -> None:
    values = ["2026-10-05 23:59:59", "2026-10-06 00:00:00", "2026-10-06 12:30:00", "2026-10-06 23:59:59.999999", "2026-10-07 00:00:00"]
    _write(service, [_row(index, time=value) for index, value in enumerate(values)])
    rows = _drain(service, start_date="2026-10-06", end_date="2026-10-06", limit=1)
    assert [item["id"] for item in rows] == ["row-3", "row-2", "row-1"]


def test_naive_timestamps_compare_exact_wall_clock(service: LogService) -> None:
    values = ["2026-10-06 12:00:00.100", "2026-10-06 12:00:00.200", "2026-10-06 12:00:00.201"]
    _write(service, [_row(index, time=value) for index, value in enumerate(values)])
    result = service.list_page(start_date="2026-10-06T12:00:00.100", end_date="2026-10-06T12:00:00.200")
    assert [item["id"] for item in result["items"]] == ["row-1", "row-0"]


def test_new_rows_keep_milliseconds_for_naive_bounds(service: LogService) -> None:
    _write(service, [
        _row(0, time="2026-10-06 12:00:00", occurred_at="2026-10-06T04:00:00.123Z"),
        _row(1, time="2026-10-06 12:00:00", occurred_at="2026-10-06T04:00:00.124Z"),
    ])
    page = service.list_page(start_date="2026-10-06T12:00:00.123", end_date="2026-10-06T12:00:00.123")
    assert [item["id"] for item in page["items"]] == ["row-0"]


def test_aware_timestamps_compare_instants_without_inventing_legacy_timezone(service: LogService) -> None:
    _write(service, [
        _row(0, time="2026-10-06 12:00:00", occurred_at="2026-10-06T04:00:00.123Z"),
        _row(1, time="2026-10-06T06:00:00.123+02:00"),
        _row(2, time="2026-10-06 12:00:00.123"),
        _row(3, time="2026-10-06 12:00:00", occurred_at="2026-10-06T04:00:00.124Z"),
    ])
    page = service.list_page(start_date="2026-10-06T12:00:00.123+08:00", end_date="2026-10-06T04:00:00.123Z")
    assert [item["id"] for item in page["items"]] == ["row-1", "row-0"]
    assert all(item["time_basis"] == "offset_aware" for item in page["items"])


@pytest.mark.parametrize("value", ["2026-02-30", "yesterday", "2026-10-06T25:00:00"])
def test_invalid_dates_fail_even_for_empty_file(service: LogService, value: str) -> None:
    with pytest.raises(ValueError):
        service.list_page(start_date=value)
    with pytest.raises(ValueError):
        service.list_page(end_date=value)


def test_new_time_fields_are_utc_and_add_never_deduplicates(service: LogService) -> None:
    before = datetime.now(timezone.utc)
    for _ in range(3):
        service.add("call", "same call", {"status": "failed"})
        service.add("account", "same state", {"status": "blocked"})
    after = datetime.now(timezone.utc)
    rows = service.list()
    assert len(rows) == 6 and len({row["id"] for row in rows}) == 6
    for row in rows:
        occurred_at = datetime.fromisoformat(row["occurred_at"])
        assert row["occurred_at"].endswith("Z") and occurred_at.utcoffset() == timedelta(0)
        assert before - timedelta(milliseconds=1) <= occurred_at <= after
        assert row["timestamp_ms"] == int(occurred_at.timestamp() * 1000)
        assert isinstance(row["timestamp_ms"], int)
        datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
    assert stat.S_IMODE(service.path.stat().st_mode) == 0o600


def _sensitive_detail() -> dict:
    return {
        "account_email": "fake-account@example.invalid",
        "nested": [{"password": "fake-password", "access_token": "fake-token"}],
        "proxy_url": "http://fake-proxy-user:fake-proxy-password@proxy.invalid:3128",
        "error": "HTTP 403 curl: (56) <!DOCTYPE html><html><title>Just a moment</title>" + "body" * 4000 + "</html>",
        "request": "Authorization: Bearer fake-bearer-secret password=fake-inline-password",
    }


def _assert_redacted(value: object) -> None:
    text = json.dumps(value)
    for secret in ("fake-password", "fake-token", "fake-proxy-user", "fake-proxy-password", "fake-bearer-secret", "fake-inline-password"):
        assert secret not in text
    assert "[REDACTED]" in text


def test_write_redacts_recursively_without_mutating_input(service: LogService) -> None:
    detail = _sensitive_detail()
    original = copy.deepcopy(detail)
    service.add("call", "password=fake-inline-password", detail)
    assert detail == original
    stored = json.loads(service.path.read_text(encoding="utf-8"))
    _assert_redacted(stored)
    assert stored["detail"]["failure_kind"] == "challenge"
    assert stored["detail"]["http_status"] == 403
    assert stored["detail"]["curl_code"] == 56
    assert len(stored["detail"]["error"]) <= 1600
    assert "<html" not in stored["detail"]["error"].lower()
    assert stored["detail"]["account_email"] == "fake-account@example.invalid"
    assert isinstance(stored["detail"]["nested"], list)


def test_read_redacts_history_without_rewriting_file(service: LogService) -> None:
    _write(service, [_row(0, detail=_sensitive_detail())])
    service.path.chmod(0o444)
    before, metadata = service.path.read_bytes(), service.path.stat()
    row = service.list()[0]
    _assert_redacted(row)
    assert row["detail"]["failure_kind"] == "challenge"
    assert row["detail"]["http_status"] == 403 and row["detail"]["curl_code"] == 56
    assert "<html" not in row["detail"]["error"].lower()
    assert service.path.read_bytes() == before
    current = service.path.stat()
    assert (current.st_mode, current.st_mtime_ns, current.st_ino) == (metadata.st_mode, metadata.st_mtime_ns, metadata.st_ino)


def test_existing_diagnostics_survive_and_nested_errors_are_enriched(service: LogService) -> None:
    detail = {"error": "HTTP 403", "failure_kind": "challenge", "http_status": 403, "nested": [{"error_message": "curl: (28) timed out"}]}
    service.add("account", "failed", detail)
    actual = service.list()[0]["detail"]
    assert actual["failure_kind"] == "challenge" and actual["http_status"] == 403
    assert actual["nested"][0]["failure_kind"] == "timeout" and actual["nested"][0]["curl_code"] == 28


def test_large_modern_file_reads_only_bounded_tail_blocks(service: LogService) -> None:
    _write(service, [_row(index, summary="x" * 512) for index in range(5000)])
    total_size = service.path.stat().st_size
    with mock.patch.object(Path, "read_text", side_effect=AssertionError("whole file read")), \
            mock.patch.object(service, "_line_number_before", side_effect=AssertionError("modern logs scanned for line numbers")), \
            mock.patch.object(service, "_read_at", wraps=service._read_at) as reader:
        page = service.list_page()
    sizes = [call.args[2] for call in reader.call_args_list]
    assert len(page["items"]) == 200 and page["has_more"]
    assert max(sizes) <= service._BLOCK_SIZE
    assert sum(sizes) < total_size // 4


@pytest.mark.parametrize("stored_history", [True, False])
def test_html_in_summary_is_bounded_and_diagnosed(service: LogService, stored_history: bool) -> None:
    summary = "HTTP 403 <!DOCTYPE html><html>" + "x" * 4000 + "Just a moment</html>"
    if stored_history:
        _write(service, [_row(0, summary=summary)])
    else:
        service.add("call", summary)
    row = service.list()[0]
    assert len(row["summary"]) <= 1000 and "<html" not in row["summary"].lower()
    assert row["detail"]["failure_kind"] == "challenge" and row["detail"]["http_status"] == 403


def test_valid_snapshot_can_resume_with_a_fresh_service_instance(service: LogService) -> None:
    _write(service, [_row(index) for index in range(5)])
    cursor = service.list_page(limit=2)["next_cursor"]
    fresh_service = LogService(service.path)
    assert [row["id"] for row in fresh_service.list_page(cursor=cursor)["items"]] == ["row-2", "row-1", "row-0"]


def test_partial_tail_is_deferred_until_refresh(service: LogService) -> None:
    _write(service, [_row(index) for index in range(4)])
    raw = service._serialize_item(_row(4)).encode("utf-8")
    split = len(raw) // 2
    with service.path.open("ab") as file:
        file.write(raw[:split])
    first = service.list_page(limit=2)
    with service.path.open("ab") as file:
        file.write(raw[split:] + b"\n")
    remaining = service.list_page(cursor=first["next_cursor"])
    assert [row["id"] for row in first["items"] + remaining["items"]] == ["row-3", "row-2", "row-1", "row-0"]
    assert service.list()[0]["id"] == "row-4"


def test_replacement_while_reading_rejects_mixed_results(service: LogService) -> None:
    _write(service, [_row(index) for index in range(4)])
    original = service._reverse_lines

    def rows(file, upper):
        replacement = service.path.with_suffix(".replacement")
        replacement.write_bytes(service.path.read_bytes())
        replacement.replace(service.path)
        yield from original(file, upper)

    with mock.patch.object(service, "_reverse_lines", side_effect=rows), pytest.raises(LogCursorError):
        service.list_page(limit=2)


def test_trim_preserves_existing_legacy_ids(service: LogService) -> None:
    recent = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    raws = [
        service._serialize_item({"time": "2000-01-01 00:00:00", "summary": "old"}),
        service._serialize_item({"time": recent, "summary": "recent"}),
    ]
    service.path.write_text("\n".join(raws), encoding="utf-8")
    legacy_id = service.list()[0]["id"]
    assert service.trim(30) == {"removed": 1, "kept": 1}
    assert service.list()[0]["id"] == legacy_id
    assert service.delete([legacy_id]) == {"removed": 1}


def test_retention_parses_actual_timestamps_and_keeps_malformed_time() -> None:
    assert _line_within_retention('{"time":"garbage"}', "2026-10-06T00:00:00Z")
    assert not _line_within_retention('{"time":"2026-10-06T01:00:00+02:00"}', "2026-10-06T00:00:00Z")
    assert _line_within_retention('{"time":"2026-10-05 20:00:00","occurred_at":"2026-10-06T00:00:00Z"}', "2026-10-06T00:00:00Z")
