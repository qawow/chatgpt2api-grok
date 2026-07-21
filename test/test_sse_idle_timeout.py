from __future__ import annotations

import time
import unittest
from typing import Iterator

from utils.helper import SseStreamTimeoutError, iter_sse_payloads


class FakeResponse:
    def __init__(self, lines: list[bytes | None], delay: float = 0.0, hang_after: int | None = None):
        self._lines = lines
        self._delay = delay
        self._hang_after = hang_after

    def iter_lines(self) -> Iterator[bytes | None]:
        for idx, line in enumerate(self._lines):
            if self._hang_after is not None and idx >= self._hang_after:
                time.sleep(10)
            if self._delay:
                time.sleep(self._delay)
            yield line


class SseIdleTimeoutTests(unittest.TestCase):
    def test_idle_timeout_raises(self):
        resp = FakeResponse([b"data: {\"ok\":1}", b"data: keep"], hang_after=1)
        with self.assertRaises(SseStreamTimeoutError) as ctx:
            list(iter_sse_payloads(resp, idle_timeout_secs=0.2, total_timeout_secs=5.0))
        self.assertIn("idle timeout", str(ctx.exception).lower())

    def test_yields_payloads_before_timeout(self):
        resp = FakeResponse([b"data: one", b"data: two", b""])
        payloads = list(iter_sse_payloads(resp, idle_timeout_secs=2.0, total_timeout_secs=5.0))
        self.assertEqual(payloads, ["one", "two"])

    def test_total_timeout_raises(self):
        # Reader hangs forever after the first line; idle budget is large so
        # the wall-clock total timeout must fire first.
        resp = FakeResponse([b"data: a", b"data: b"], hang_after=1)
        with self.assertRaises(SseStreamTimeoutError) as ctx:
            list(iter_sse_payloads(resp, idle_timeout_secs=5.0, total_timeout_secs=0.35))
        self.assertIn("total timeout", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
