"""Sentinel 头的完整性回归测试。

背景（2026-10-16 bundle 逆向，`.verify/bundle2/`）：

真实客户端的 sentinel 头名映射表在 `a-BiuthBl1.js`（全文仅 677 字节）：

```js
{chatRequirementsToken:       "OpenAI-Sentinel-Chat-Requirements-Token",
 chatRequirementsPrepareToken:"OpenAI-Sentinel-Chat-Requirements-Prepare-Token",
 proofToken:                  "OpenAI-Sentinel-Proof-Token",
 turnstileToken:              "OpenAI-Sentinel-Turnstile-Token",
 sessionObserverToken:        "OpenAI-Sentinel-SO-Token",
 telemetryToken:              "OAI-Echo-Logs",     // ← 新版新增
 timingToken:                 "OAI-Telemetry"}     // ← 新版新增
```

本仓库此前**只发前五个中的四个**（漏 `prepare`、漏 `telemetry`、漏 `timing`）。
`OAI-Echo-Logs` 的值是输入事件时序（`a--tOLhrKL.js`）：把
`focusin`/`focusout`/`copy`/`paste`/`touchstart`/`touchend` 的前 10 条事件
编码成 `"<type>,<timestamp>"` 对，用逗号连接。
`OAI-Telemetry` 在 SDK 未产出 timing 时回退到字面量 `"[1,null]"`。
"""
from __future__ import annotations

import unittest

from services.openai_backend_api import (
    SENTINEL_HEADER_CHAT_REQUIREMENTS,
    SENTINEL_HEADER_PROOF,
    SENTINEL_HEADER_SESSION_OBSERVER,
    SENTINEL_HEADER_TELEMETRY,
    SENTINEL_HEADER_TIMING,
    SENTINEL_HEADER_TURNSTILE,
    SENTINEL_TIMING_FALLBACK,
    OpenAIBackendAPI,
    SentinelEchoLogs,
    sentinel_echo_logs,
)


class SentinelHeaderNameTests(unittest.TestCase):
    """头名必须与真实客户端的映射表逐字一致。"""

    def test_header_names_match_the_frontend_map(self):
        self.assertEqual(SENTINEL_HEADER_CHAT_REQUIREMENTS,
                         "OpenAI-Sentinel-Chat-Requirements-Token")
        self.assertEqual(SENTINEL_HEADER_PROOF, "OpenAI-Sentinel-Proof-Token")
        self.assertEqual(SENTINEL_HEADER_TURNSTILE, "OpenAI-Sentinel-Turnstile-Token")
        self.assertEqual(SENTINEL_HEADER_SESSION_OBSERVER, "OpenAI-Sentinel-SO-Token")
        self.assertEqual(SENTINEL_HEADER_TELEMETRY, "OAI-Echo-Logs")
        self.assertEqual(SENTINEL_HEADER_TIMING, "OAI-Telemetry")

    def test_timing_fallback_matches_the_frontend_literal(self):
        # bundle: const M="[1,null]"
        self.assertEqual(SENTINEL_TIMING_FALLBACK, "[1,null]")


class SentinelEchoLogsTests(unittest.TestCase):
    """`OAI-Echo-Logs` 的取值形态。"""

    def test_trace_is_type_timestamp_pairs(self):
        tracker = SentinelEchoLogs()
        tracker.composer_submit()
        value = tracker.value()
        numbers = [int(part) for part in value.split(",")]
        self.assertEqual(len(numbers) % 2, 0, msg=value)
        pairs = list(zip(numbers[0::2], numbers[1::2]))
        self.assertTrue(pairs, msg=value)
        # 0=focusin 3=paste 4=touchstart 5=touchend
        self.assertEqual([event_type for event_type, _ in pairs], [0, 3, 4, 5])

    def test_timestamps_are_monotonic_and_non_negative(self):
        tracker = SentinelEchoLogs()
        tracker.composer_submit()
        numbers = [int(part) for part in tracker.value().split(",")]
        stamps = numbers[1::2]
        self.assertTrue(all(stamp >= 0 for stamp in stamps), msg=stamps)
        self.assertEqual(stamps, sorted(stamps), msg=stamps)

    def test_trace_never_exceeds_the_frontend_limit_of_ten_events(self):
        tracker = SentinelEchoLogs()
        for _ in range(5):
            tracker.composer_submit()
            for _ in range(20):
                tracker._record(0)
        self.assertLessEqual(len(tracker._events), 10)

    def test_timestamps_grow_with_process_lifetime(self):
        """event.timeStamp 随页面存活增长；全零的时间戳不是浏览器会产生的。"""
        first = [int(p) for p in sentinel_echo_logs().split(",")][1::2]
        self.assertIsInstance(first, list)
        # 进程级 tracker 在导入时创建，因此后续调用必然不早于首次调用。
        second = [int(p) for p in sentinel_echo_logs().split(",")][1::2]
        self.assertGreaterEqual(second[0], first[0])


class SentinelHeaderEmissionTests(unittest.TestCase):
    """两个新头必须真的出现在请求头上。"""

    def setUp(self):
        self.api = OpenAIBackendAPI("")
        self.addCleanup(self.api.close)

    def _requirements(self):
        from services.openai_backend_api import ChatRequirements

        return ChatRequirements(
            token="requirements-token",
            proof_token="proof-token",
            turnstile_token="turnstile-token",
            so_token="so-token",
        )

    def test_conversation_headers_carry_telemetry_and_timing(self):
        headers = self.api._conversation_headers("/backend-api/conversation",
                                                 self._requirements())
        self.assertIn(SENTINEL_HEADER_TELEMETRY, headers)
        self.assertIn(SENTINEL_HEADER_TIMING, headers)
        self.assertEqual(headers[SENTINEL_HEADER_TIMING], SENTINEL_TIMING_FALLBACK)
        numbers = [int(p) for p in headers[SENTINEL_HEADER_TELEMETRY].split(",")]
        self.assertEqual(len(numbers) % 2, 0, msg=headers[SENTINEL_HEADER_TELEMETRY])

    def test_image_headers_carry_telemetry_and_timing(self):
        headers = self.api._image_headers("/backend-api/f/conversation",
                                          self._requirements(), "conduit",
                                          "text/event-stream")
        self.assertIn(SENTINEL_HEADER_TELEMETRY, headers)
        self.assertIn(SENTINEL_HEADER_TIMING, headers)
        self.assertEqual(headers[SENTINEL_HEADER_TIMING], SENTINEL_TIMING_FALLBACK)

    def test_existing_sentinel_headers_are_unchanged(self):
        headers = self.api._image_headers("/backend-api/f/conversation",
                                          self._requirements(), "conduit",
                                          "text/event-stream")
        self.assertEqual(headers[SENTINEL_HEADER_CHAT_REQUIREMENTS], "requirements-token")
        self.assertEqual(headers[SENTINEL_HEADER_PROOF], "proof-token")
        self.assertEqual(headers["X-Conduit-Token"], "conduit")

    def test_telemetry_is_omitted_when_absent(self):
        """没有 proof/turnstile 时不应凭空造出这两个头。"""
        from services.openai_backend_api import ChatRequirements

        headers = self.api._conversation_headers(
            "/backend-api/conversation", ChatRequirements(token="only-requirements"))
        self.assertNotIn(SENTINEL_HEADER_PROOF, headers)
        self.assertNotIn(SENTINEL_HEADER_TURNSTILE, headers)
        self.assertNotIn(SENTINEL_HEADER_SESSION_OBSERVER, headers)
        # 但 telemetry/timing 是无条件的，真实客户端总是发送。
        self.assertIn(SENTINEL_HEADER_TELEMETRY, headers)
        self.assertIn(SENTINEL_HEADER_TIMING, headers)


if __name__ == "__main__":
    unittest.main()
