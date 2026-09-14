from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "chatgpt2api")

from services.log_service import LogService, _line_within_retention


def _line(time_str: str, summary: str = "x", detail: object = None) -> str:
    return LogService._serialize_item({
        "id": "abc",
        "time": time_str,
        "type": "account",
        "summary": summary,
        "detail": detail if detail is not None else {"a": 1},
    })


class LineRetentionTests(unittest.TestCase):
    def test_keeps_recent_drops_old(self) -> None:
        cutoff = "2026-09-01 00:00:00"
        self.assertTrue(_line_within_retention(_line("2026-09-15 10:00:00"), cutoff))
        self.assertFalse(_line_within_retention(_line("2026-08-01 10:00:00"), cutoff))
        # Exact boundary counts as recent.
        self.assertTrue(_line_within_retention(_line(cutoff), cutoff))

    def test_unparseable_line_is_kept(self) -> None:
        self.assertTrue(_line_within_retention("not json at all", "2020-01-01 00:00:00"))
        self.assertTrue(_line_within_retention('{"nope": 1}', "2020-01-01 00:00:00"))

    def test_nested_time_does_not_shadow_top_level(self) -> None:
        # The top-level time is serialized before "detail", so the first match wins.
        line = _line("2026-08-01 00:00:00", detail={"time": "2099-01-01 00:00:00"})
        self.assertFalse(_line_within_retention(line, "2026-09-01 00:00:00"))


class LogTrimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(os.environ.get("TMPDIR") or "/tmp") / f"logtrim-{os.getpid()}"
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        self.path = self.tmpdir / "logs.jsonl"
        self.service = LogService(self.path)
        self.addCleanup(lambda: [p.unlink(missing_ok=True) for p in self.tmpdir.glob("*")] or self.tmpdir.rmdir())

    def _write(self, lines: list[str]) -> None:
        self.path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    def test_trim_removes_old_entries_only(self) -> None:
        now = datetime.now()
        old = (now - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
        recent = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        self._write([_line(old, "old-1"), _line(recent, "recent-1"), _line(old, "old-2")])
        result = self.service.trim(30)
        self.assertEqual(result, {"removed": 2, "kept": 1})
        kept = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([item["summary"] for item in kept], ["recent-1"])

    def test_trim_noop_when_all_recent(self) -> None:
        now = datetime.now()
        recent = now.strftime("%Y-%m-%d %H:%M:%S")
        self._write([_line(recent, "r")])
        result = self.service.trim(30)
        self.assertEqual(result, {"removed": 0, "kept": 1})
        # File untouched (no temp file left behind).
        self.assertEqual(sorted(p.name for p in self.tmpdir.iterdir()), ["logs.jsonl"])

    def test_disabled_retention_is_noop(self) -> None:
        now = datetime.now()
        ancient = (now - timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
        self._write([_line(ancient)])
        self.assertEqual(self.service.trim(0), {"removed": 0, "kept": 0})
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1)

    def test_trim_preserves_add_after_trim(self) -> None:
        now = datetime.now()
        old = (now - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
        self._write([_line(old, "old")])
        self.service.trim(30)
        self.service.add("account", "after-trim", {"k": "v"})
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["summary"], "after-trim")


if __name__ == "__main__":
    unittest.main()
