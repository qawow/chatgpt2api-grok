from __future__ import annotations

import base64
import json
import queue
import threading
import time
import unittest
from dataclasses import FrozenInstanceError
from typing import Iterator
from unittest import mock

from utils.helper import (
    SseFrame,
    SseProtocolError,
    SseStreamTimeoutError,
    V1DeltaDecoder,
    anthropic_sse_stream,
    build_chat_image_markdown_content,
    iter_sse_frames,
    iter_sse_payloads,
    sse_json_stream,
)


class FakeResponse:
    """A closeable in-memory stream: no sleeping, sockets or leaked readers."""

    def __init__(
        self,
        lines: list[bytes | str | None | Exception],
        delay: float = 0.0,
        hang_after: int | None = None,
    ):
        self._lines = lines
        self._delay = delay
        self._hang_after = hang_after
        self.closed = threading.Event()
        self.finished = threading.Event()
        self.blocked = threading.Event()
        self.close_count = 0

    def iter_lines(self) -> Iterator[bytes | str | None]:
        try:
            for idx, line in enumerate(self._lines):
                if self.closed.is_set():
                    return
                if self._hang_after is not None and idx >= self._hang_after:
                    self.blocked.set()
                    self.closed.wait()
                    return
                if self._delay and self.closed.wait(self._delay):
                    return
                if isinstance(line, Exception):
                    raise line
                yield line
            if self._hang_after is not None and self._hang_after >= len(self._lines):
                self.blocked.set()
                self.closed.wait()
        finally:
            self.finished.set()

    def close(self) -> None:
        self.close_count += 1
        self.closed.set()


class SseIdleTimeoutTests(unittest.TestCase):
    def assert_reader_stopped(self, response: FakeResponse) -> None:
        self.assertTrue(response.closed.is_set())
        self.assertTrue(response.finished.wait(0.3), "SSE reader did not stop after close")

    def test_idle_timeout_raises_and_closes(self):
        resp = FakeResponse([b'data: {"ok":1}', b"", b"data: keep"], hang_after=2)
        started = time.monotonic()
        with self.assertRaises(SseStreamTimeoutError) as ctx:
            list(iter_sse_payloads(resp, idle_timeout_secs=0.03, total_timeout_secs=1.0))
        self.assertIn("idle timeout", str(ctx.exception).lower())
        self.assertLess(time.monotonic() - started, 0.8)
        self.assert_reader_stopped(resp)

    def test_yields_complete_payloads_before_timeout(self):
        resp = FakeResponse([b"data: one", b"", b"data: two"], hang_after=2)
        stream = iter_sse_payloads(resp, idle_timeout_secs=0.03, total_timeout_secs=1.0)
        self.assertEqual(next(stream), "one")
        with self.assertRaises(SseStreamTimeoutError):
            next(stream)
        self.assert_reader_stopped(resp)

    def test_total_timeout_raises_and_closes(self):
        resp = FakeResponse([b"data: a", b"", b"data: b"], hang_after=2)
        started = time.monotonic()
        with self.assertRaises(SseStreamTimeoutError) as ctx:
            list(iter_sse_payloads(resp, idle_timeout_secs=1.0, total_timeout_secs=0.04))
        self.assertIn("total timeout", str(ctx.exception).lower())
        self.assertLess(time.monotonic() - started, 0.8)
        self.assert_reader_stopped(resp)

    def test_comment_heartbeats_reset_idle_activity(self):
        resp = FakeResponse([b": keep"] * 6 + [b"data: ready", b""], delay=0.01)
        frames = list(iter_sse_frames(resp, idle_timeout_secs=0.04, total_timeout_secs=1.0))
        self.assertEqual(frames, [SseFrame(data="ready")])
        self.assert_reader_stopped(resp)

    def test_heartbeats_do_not_extend_total_budget(self):
        resp = FakeResponse([b": keep", b""] * 100, delay=0.008)
        with self.assertRaisesRegex(SseStreamTimeoutError, "total timeout"):
            list(iter_sse_frames(resp, idle_timeout_secs=0.04, total_timeout_secs=0.065))
        self.assert_reader_stopped(resp)

    def test_timeout_uses_monotonic_not_wall_clock(self):
        resp = FakeResponse([b"data: ok", b""])
        with mock.patch("utils.helper.time.time", side_effect=AssertionError("wall clock was used")):
            frames = list(iter_sse_frames(resp, idle_timeout_secs=0.5, total_timeout_secs=1.0))
        self.assertEqual(frames, [SseFrame(data="ok")])
        self.assert_reader_stopped(resp)

    def test_closing_frame_generator_unblocks_reader(self):
        resp = FakeResponse([b"data: one", b""], hang_after=2)
        stream = iter_sse_frames(resp, idle_timeout_secs=1.0, total_timeout_secs=2.0)
        self.assertEqual(next(stream), SseFrame(data="one"))
        stream.close()
        self.assert_reader_stopped(resp)

    def test_closing_payload_adapter_closes_frame_generator(self):
        resp = FakeResponse([b"data: one", b""], hang_after=2)
        stream = iter_sse_payloads(resp, idle_timeout_secs=1.0, total_timeout_secs=2.0)
        self.assertEqual(next(stream), "one")
        stream.close()
        self.assert_reader_stopped(resp)

    def test_reader_error_is_preserved_and_closed(self):
        failure = RuntimeError("synthetic transport failure")
        resp = FakeResponse([b"data: ok", b"", failure])
        stream = iter_sse_frames(resp, idle_timeout_secs=0.5, total_timeout_secs=1.0)
        self.assertEqual(next(stream), SseFrame(data="ok"))
        with self.assertRaises(RuntimeError) as ctx:
            next(stream)
        self.assertIs(ctx.exception, failure)
        self.assert_reader_stopped(resp)

    def test_invalid_budgets_are_rejected_and_closed(self):
        for name in ("idle_timeout_secs", "total_timeout_secs"):
            for bad in (0, -1, float("inf"), float("nan")):
                with self.subTest(name=name, value=bad):
                    resp = FakeResponse([])
                    kwargs = {"idle_timeout_secs": 0.5, "total_timeout_secs": 1.0, name: bad}
                    with self.assertRaises(ValueError):
                        list(iter_sse_frames(resp, **kwargs))
                    self.assertTrue(resp.closed.is_set())

    def test_bounded_queue_and_stop_release_backpressured_reader(self):
        class BurstResponse(FakeResponse):
            def __init__(self):
                super().__init__([])
                self.emitted = 0
                self.prefetched = threading.Event()

            def iter_lines(self):
                try:
                    for index in range(10_000):
                        if self.closed.is_set():
                            return
                        self.emitted += 1
                        if self.emitted >= 64:
                            self.prefetched.set()
                        yield b"data: x" if index % 2 == 0 else b""
                finally:
                    self.finished.set()

        real_queue = queue.Queue
        queues = []

        def make_queue(*args, **kwargs):
            result = real_queue(*args, **kwargs)
            queues.append(result)
            return result

        resp = BurstResponse()
        with mock.patch("queue.Queue", side_effect=make_queue):
            stream = iter_sse_frames(resp, idle_timeout_secs=1.0, total_timeout_secs=2.0)
            self.assertEqual(next(stream), SseFrame(data="x"))
            try:
                self.assertTrue(resp.prefetched.wait(0.3))
                self.assertGreater(queues[0].maxsize, 0)
                self.assertLess(resp.emitted, 200, "reader ran past bounded backpressure")
            finally:
                stream.close()
        self.assert_reader_stopped(resp)


class SseFrameProtocolTests(unittest.TestCase):
    def frames(self, lines: list[bytes | str | None]) -> list[SseFrame]:
        resp = FakeResponse(lines)
        result = list(iter_sse_frames(resp, idle_timeout_secs=0.5, total_timeout_secs=1.0))
        self.assertTrue(resp.closed.is_set())
        self.assertTrue(resp.finished.wait(0.3))
        return result

    def test_frame_defaults_and_immutability(self):
        self.assertEqual(SseFrame(), SseFrame(event="message", data="", id=""))
        frame = SseFrame(data="value")
        with self.assertRaises(FrozenInstanceError):
            frame.data = "changed"

    def test_named_multiline_event_and_sticky_id(self):
        frames = self.frames([
            b"event: delta", b"id: 42", b'data: {"c":0,',
            b'data: "v":1}', b"", b"data: next", b"",
        ])
        self.assertEqual(frames, [
            SseFrame(event="delta", data='{"c":0,\n"v":1}', id="42"),
            SseFrame(data="next", id="42"),
        ])

    def test_payload_adapter_keeps_string_interface(self):
        resp = FakeResponse([b"event: delta", b"data: one", b"data: two", b"", b"data: [DONE]", b""])
        self.assertEqual(
            list(iter_sse_payloads(resp, idle_timeout_secs=0.5, total_timeout_secs=1.0)),
            ["one\ntwo", "[DONE]"],
        )

    def test_only_one_optional_space_is_removed(self):
        self.assertEqual(self.frames([b"data:  leading and trailing  ", b"data:\ttab\t", b""]), [
            SseFrame(data=" leading and trailing  \n\ttab\t"),
        ])

    def test_empty_data_fields_are_events_but_empty_blocks_are_not(self):
        self.assertEqual(self.frames([b"", b": comment", b"", b"data:", b"", b"data", b""]), [
            SseFrame(data=""), SseFrame(data=""),
        ])

    def test_comments_and_id_only_blocks_do_not_emit_frames(self):
        self.assertEqual(self.frames([
            b": heartbeat", b"event: ignored", b"id: kept", b"retry: 300", b"unknown: x", b"",
            b"data: body", b"",
        ]), [SseFrame(data="body", id="kept")])

    def test_crlf_and_text_lines(self):
        self.assertEqual(self.frames([
            "event: delta\r\n", "data: first\r\n", "data: second\r", "\r\n",
        ]), [SseFrame(event="delta", data="first\nsecond")])

    def test_bom_is_removed_only_from_first_line(self):
        self.assertEqual(self.frames([
            b"\xef\xbb\xbfdata: first", b"", b"\xef\xbb\xbfdata: ignored", b"", b"data: \xef\xbb\xbfbody", b"",
        ]), [SseFrame(data="first"), SseFrame(data="\ufeffbody")])

    def test_id_nul_is_ignored_and_empty_id_resets(self):
        self.assertEqual(self.frames([
            b"id: initial", b"data: a", b"", b"id: bad\0id", b"data: b", b"",
            b"id", b"event:", b"data: c", b"",
        ]), [SseFrame(data="a", id="initial"), SseFrame(data="b", id="initial"), SseFrame(data="c")])

    def test_unterminated_final_event_is_not_dispatched(self):
        self.assertEqual(self.frames([b"data: incomplete"]), [])
        self.assertEqual(self.frames([b"data: complete", b"", b"data: incomplete"]), [SseFrame(data="complete")])

    def test_invalid_utf8_is_not_silently_ignored(self):
        resp = FakeResponse([b"data: bad\xffbytes", b""])
        with self.assertRaises(UnicodeDecodeError):
            list(iter_sse_frames(resp, idle_timeout_secs=0.5, total_timeout_secs=1.0))
        self.assertTrue(resp.closed.is_set())
        self.assertTrue(resp.finished.wait(0.3))

    def test_no_synthetic_http_response_frame(self):
        self.assertEqual(self.frames([None, b": keep", b"", b"HTTP/1.1 200 OK", b"", b"data: x", b""]), [
            SseFrame(data="x"),
        ])


class V1DeltaDecoderTests(unittest.TestCase):
    def test_root_defaults_and_inherited_path_op_channel(self):
        decoder = V1DeltaDecoder()
        self.assertEqual(decoder.apply_delta({"v": {"text": "a"}}), {"text": "a"})
        self.assertEqual(decoder.apply_delta({"p": "/text", "o": "append", "v": "b"}), {"text": "ab"})
        self.assertEqual(decoder.apply_delta({"v": "c"}), {"text": "abc"})

    def test_channels_are_independent_and_defaults_are_global(self):
        decoder = V1DeltaDecoder()
        decoder.apply_delta({"c": 0, "v": {"text": "zero"}})
        decoder.apply_delta({"c": 1, "p": "", "o": "add", "v": {"text": "one"}})
        self.assertEqual(decoder.apply_delta({"c": 0, "p": "/text", "o": "append", "v": "!"}), {"text": "zero!"})
        self.assertEqual(decoder.apply_delta({"c": 1, "v": "?"}), {"text": "one?"})
        self.assertEqual(decoder.apply_delta({"v": "!"}), {"text": "one?!"})

    def test_input_and_returned_snapshots_do_not_alias_state(self):
        decoder = V1DeltaDecoder()
        source = {"parts": ["a"]}
        first = decoder.apply_delta({"v": source})
        source["parts"].append("input mutation")
        first["parts"].append("output mutation")
        second = decoder.apply_delta({"p": "/parts/0", "o": "append", "v": "b"})
        self.assertEqual(second, {"parts": ["ab"]})
        third = decoder.apply_delta({"v": "c"})
        self.assertEqual(second, {"parts": ["ab"]})
        self.assertEqual(third, {"parts": ["abc"]})

    def test_missing_value_is_undefined_not_inherited_or_null(self):
        decoder = V1DeltaDecoder()
        decoder.apply_delta({"v": {"key": "old", "items": [1]}})
        self.assertEqual(decoder.apply_delta({"p": "/key", "o": "replace"}), {"items": [1]})
        self.assertEqual(decoder.apply_delta({"p": "/key", "v": None}), {"items": [1], "key": None})
        self.assertEqual(decoder.apply_delta({"p": "/items/0"}), {"items": [None], "key": None})
        self.assertIsNone(decoder.apply_delta({"p": "", "o": "replace"}))
        self.assertEqual(decoder.apply_delta({"p": "/new", "o": "add", "v": 2}), {"new": 2})

    def test_undefined_and_null_root_traversal_differ(self):
        decoder = V1DeltaDecoder()
        decoder.apply_delta({"v": None})
        with self.assertRaises(SseProtocolError):
            decoder.apply_delta({"p": "/x", "o": "add", "v": 1})
        decoder.apply_delta({"p": "", "o": "replace"})
        self.assertEqual(decoder.apply_delta({"p": "/x", "o": "add", "v": 1}), {"x": 1})

    def test_inherited_short_fields_override_top_level_long_fields(self):
        decoder = V1DeltaDecoder()
        decoder.apply_delta({"c": 2, "v": {"text": "a"}})
        decoder.apply_delta({"p": "/text", "o": "append", "v": "b"})
        self.assertEqual(decoder.apply_delta({
            "channel": 99, "path": "/ignored", "op": "remove", "value": "c",
        }), {"text": "abc"})

    def test_explicit_short_value_overrides_long_value(self):
        self.assertEqual(V1DeltaDecoder().apply_delta({"value": "long", "v": "short"}), "short")

    def test_json_pointer_escaping_and_numeric_container_creation(self):
        decoder = V1DeltaDecoder()
        self.assertEqual(decoder.apply_delta({"p": "/message/parts/0/text", "v": "ok"}), {
            "message": {"parts": [{"text": "ok"}]},
        })
        decoder = V1DeltaDecoder()
        self.assertEqual(decoder.apply_delta({"p": "/a~1b/~0key/01", "v": 7}), {
            "a/b": {"~key": {"01": 7}},
        })
        self.assertEqual(decoder.apply_delta({"p": "/items/2", "o": "replace", "v": "x"}), {
            "a/b": {"~key": {"01": 7}}, "items": [None, None, "x"],
        })

    def test_non_ascii_digits_remain_object_path_keys(self):
        self.assertEqual(V1DeltaDecoder().apply_delta({"p": "/1\u0662", "v": 1}), {"1\u0662": 1})
        with self.assertRaises(SseProtocolError):
            V1DeltaDecoder().apply_delta({"p": "/9007199254740993", "v": 1})

    def test_add_remove_replace_append_and_truncate_arrays(self):
        decoder = V1DeltaDecoder()
        decoder.apply_delta({"v": [1, 3]})
        self.assertEqual(decoder.apply_delta({"p": "/1", "o": "add", "v": 2}), [1, 2, 3])
        self.assertEqual(decoder.apply_delta({"p": "/0", "o": "replace", "v": 0}), [0, 2, 3])
        self.assertEqual(decoder.apply_delta({"p": "/1", "o": "remove"}), [0, 3])
        self.assertEqual(decoder.apply_delta({"p": "", "o": "append", "v": [4, 5]}), [0, 3, 4, 5])
        self.assertEqual(decoder.apply_delta({"v": 6}), [0, 3, 4, 5, 6])
        self.assertEqual(decoder.apply_delta({"o": "truncate", "v": 3}), [0, 3, 4])
        self.assertEqual(decoder.apply_delta({"v": 5}), [0, 3, 4, None, None])

    def test_array_splice_large_index_clamps_without_sparse_allocation(self):
        decoder = V1DeltaDecoder()
        decoder.apply_delta({"v": [1]})
        self.assertEqual(decoder.apply_delta({"p": "/1000000000", "o": "add", "v": 2}), [1, 2])
        self.assertEqual(decoder.apply_delta({"o": "remove"}), [1, 2])

    def test_append_object_is_shallow_and_non_container_is_replaced(self):
        decoder = V1DeltaDecoder()
        decoder.apply_delta({"v": {"nested": {"old": 1}, "keep": True, "empty": None}})
        self.assertEqual(decoder.apply_delta({"o": "append", "v": {"nested": {"new": 2}}}), {
            "nested": {"new": 2}, "keep": True, "empty": None,
        })
        self.assertEqual(decoder.apply_delta({"p": "/empty", "v": "filled"}), {
            "nested": {"new": 2}, "keep": True, "empty": "filled",
        })

    def test_string_append_coercion_and_utf16_truncate(self):
        decoder = V1DeltaDecoder()
        decoder.apply_delta({"v": "a"})
        self.assertEqual(decoder.apply_delta({"o": "append", "v": True}), "atrue")
        self.assertEqual(decoder.apply_delta({}), "atrueundefined")
        decoder.apply_delta({"o": "replace", "v": "a\U0001f642b"})
        self.assertEqual(decoder.apply_delta({"o": "truncate", "v": 3}), "a\U0001f642")
        self.assertEqual(decoder.apply_delta({"v": -1}), "")

    def test_nested_patches_are_relative_and_expand_long_fields(self):
        decoder = V1DeltaDecoder()
        decoder.apply_delta({"v": {"message": {"text": "a", "items": [1, 3]}}})
        self.assertEqual(decoder.apply_delta({"p": "/message", "o": "patch", "v": [
            {"p": "/text", "o": "append", "v": "b"},
            {"path": "/items", "op": "patch", "value": [
                {"path": "/1", "op": "add", "value": 2},
                {"p": "/0", "o": "remove"},
            ]},
        ]}), {"message": {"text": "ab", "items": [2, 3]}})

    def test_nested_patch_does_not_inherit_missing_op(self):
        decoder = V1DeltaDecoder()
        decoder.apply_delta({"v": {"text": "a"}})
        with self.assertRaises(SseProtocolError):
            decoder.apply_delta({"o": "patch", "v": [{"p": "/text", "v": "wrong"}]})
        self.assertEqual(decoder.apply_delta({"p": "/text", "o": "append", "v": "b"}), {"text": "ab"})

    def test_failed_patch_commits_neither_partial_state_nor_defaults(self):
        decoder = V1DeltaDecoder()
        decoder.apply_delta({"v": {"text": "a"}})
        with self.assertRaises(SseProtocolError):
            decoder.apply_delta({"c": 9, "p": "", "o": "patch", "v": [
                {"p": "/text", "o": "add", "v": "partial"},
                {"p": "/constructor", "o": "add", "v": "unsafe"},
            ]})
        # Missing c/p/o must still inherit channel 0/root/add, not failed patch.
        self.assertEqual(decoder.apply_delta({"v": {"text": "restored"}}), {"text": "restored"})
        self.assertEqual(decoder.apply_delta({"c": 9, "p": "", "o": "replace", "v": "new"}), "new")

    def test_failed_patch_preserves_existing_channel_state(self):
        decoder = V1DeltaDecoder()
        decoder.apply_delta({"v": {"text": "a"}})
        with self.assertRaises(SseProtocolError):
            decoder.apply_delta({"o": "patch", "v": [
                {"p": "/text", "o": "append", "v": "partial"},
                {"p": "/prototype", "o": "replace", "v": 1},
            ]})
        self.assertEqual(decoder.apply_delta({"p": "/text", "o": "append", "v": "b"}), {"text": "ab"})

    def test_unsafe_paths_and_append_keys_are_rejected(self):
        for name in ("__proto__", "constructor", "prototype"):
            for op in ("add", "remove", "replace", "append", "truncate"):
                with self.subTest(name=name, op=op):
                    decoder = V1DeltaDecoder()
                    decoder.apply_delta({"v": {}})
                    with self.assertRaises(SseProtocolError):
                        decoder.apply_delta({"p": "/" + name, "o": op, "v": 1})
            decoder = V1DeltaDecoder()
            decoder.apply_delta({"v": {"safe": 1}})
            with self.assertRaises(SseProtocolError):
                decoder.apply_delta({"o": "append", "v": {name: 2}})
            self.assertEqual(decoder.apply_delta({"p": "/safe", "o": "replace", "v": 3}), {"safe": 3})

    def test_invalid_channels_and_non_objects_are_rejected(self):
        for channel in (-1, 1.5, float("inf"), float("nan"), 2**53, True, False, "0", None, [], {}):
            with self.subTest(channel=channel):
                with self.assertRaises(SseProtocolError):
                    V1DeltaDecoder().apply_delta({"c": channel, "v": 1})
        for value in (None, [], "delta", 1):
            with self.subTest(value=value):
                with self.assertRaises(SseProtocolError):
                    V1DeltaDecoder().apply_delta(value)
        self.assertEqual(V1DeltaDecoder().apply_delta({"c": 1.0, "v": 1}), 1)
        self.assertEqual(V1DeltaDecoder().apply_delta({"c": 2**53 - 1, "v": 1}), 1)

    def test_unknown_op_invalid_path_and_patch_value_fail_atomically(self):
        for delta in (
            {"o": "unknown", "v": 1}, {"p": 1, "v": 1},
            {"o": "patch", "v": {}}, {"o": "patch", "v": [1]},
        ):
            with self.subTest(delta=delta):
                decoder = V1DeltaDecoder()
                decoder.apply_delta({"v": {"safe": 1}})
                with self.assertRaises(SseProtocolError):
                    decoder.apply_delta(delta)
                self.assertEqual(decoder.apply_delta({"p": "/safe", "o": "replace", "v": 2}), {"safe": 2})

    def test_huge_sparse_indices_and_truncate_growth_are_rejected(self):
        for delta in (
            {"p": "/items/1000000000", "o": "replace", "v": 1},
            {"p": "/items/1000000000/key", "o": "add", "v": 1},
            {"p": "/items", "o": "truncate", "v": 1000000000},
            {"p": "/items/" + "9" * 5000, "o": "replace", "v": 1},
        ):
            with self.subTest(op=delta["o"]):
                decoder = V1DeltaDecoder()
                decoder.apply_delta({"v": {"items": [1]}})
                with self.assertRaises(SseProtocolError):
                    decoder.apply_delta(delta)
                self.assertEqual(decoder.apply_delta({"p": "/items", "o": "append", "v": 2}), {"items": [1, 2]})

    def test_invalid_array_truncate_length_is_rejected(self):
        for length in (-1, 1.5, float("inf"), float("nan"), 2**32):
            with self.subTest(length=length):
                decoder = V1DeltaDecoder()
                decoder.apply_delta({"v": [1]})
                with self.assertRaises(SseProtocolError):
                    decoder.apply_delta({"o": "truncate", "v": length})
        decoder = V1DeltaDecoder()
        decoder.apply_delta({"v": [1]})
        with self.assertRaises(SseProtocolError):
            decoder.apply_delta({"o": "truncate"})
        self.assertEqual(decoder.apply_delta({"o": "truncate", "v": None}), [])


class SseJsonWireSafetyTests(unittest.TestCase):
    def text_states(self) -> list[str]:
        decoder = V1DeltaDecoder()
        original = decoder.apply_delta({"v": "a\U0001f642b"})
        truncated = decoder.apply_delta({"o": "truncate", "v": 2})
        repaired = decoder.apply_delta({"o": "append", "v": "\ude42"})
        self.assertEqual(original, "a\U0001f642b")
        self.assertEqual(truncated, "a\ud83d")
        # The decoder keeps the two UTF-16 code units internally. JSON's escaped
        # round trip, not a lossy decoder replacement, restores the scalar emoji.
        self.assertEqual(repaired, "a\ud83d\ude42")
        return [original, truncated, repaired]

    def decoded_data(self, chunks: list[str]) -> list[dict]:
        result = []
        for chunk in chunks:
            chunk.encode("utf-8", errors="strict")
            if not chunk.startswith("data: "):
                continue
            payload = chunk[6:].rstrip("\n")
            if payload != "[DONE]":
                result.append(json.loads(payload))
        return result

    def test_openai_wire_escapes_lone_surrogate_and_restores_completed_pair(self):
        states = self.text_states()
        chunks = list(sse_json_stream({"text": text} for text in states))
        decoded = self.decoded_data(chunks)
        self.assertEqual([item["text"] for item in decoded], ["a\U0001f642b", "a\ud83d", "a\U0001f642"])
        self.assertIn("\\ud83d\\ude42", "".join(chunks))
        self.assertTrue(all(chunk.isascii() for chunk in chunks))
        self.assertEqual(chunks[-1], "data: [DONE]\n\n")

    def test_anthropic_wire_escapes_lone_surrogate_and_restores_completed_pair(self):
        states = self.text_states()
        items = [{
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": text},
        } for text in states]
        chunks = list(anthropic_sse_stream(items))
        decoded = self.decoded_data(chunks)
        self.assertEqual([item["delta"]["text"] for item in decoded],
                         ["a\U0001f642b", "a\ud83d", "a\U0001f642"])
        self.assertEqual(chunks[0], "event: content_block_delta\n")
        self.assertIn("\\ud83d\\ude42", "".join(chunks))
        self.assertTrue(all(chunk.isascii() for chunk in chunks))

    def test_both_error_json_boundaries_escape_lone_surrogates(self):
        def failing_items():
            yield {"type": "message_delta", "delta": {"text": "before error"}}
            raise ValueError("synthetic error: \ud83d")

        for stream in (sse_json_stream, anthropic_sse_stream):
            with self.subTest(stream=stream.__name__):
                # Keep the test strictly in memory; logging is not the wire path.
                with mock.patch("utils.helper.logger.warning") as warning:
                    chunks = list(stream(failing_items()))
                warning.assert_called_once()
                decoded = self.decoded_data(chunks)
                self.assertEqual(decoded[-1]["error"]["message"], "synthetic error: \ud83d")
                self.assertEqual(decoded[-1]["error"]["type"], "ValueError")
                self.assertIn("\\ud83d", "".join(chunks))
                self.assertTrue(all(chunk.isascii() for chunk in chunks))


class ImageMarkdownMimeTests(unittest.TestCase):
    def test_detected_mime_preserves_original_base64_without_transcoding(self):
        raw = b"synthetic media bytes"
        encoded = base64.b64encode(raw).decode("ascii")
        for mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            with self.subTest(mime=mime):
                with mock.patch("utils.image_media.sniff_image_media", return_value=(mime, ".ext")) as sniff:
                    result = build_chat_image_markdown_content({"data": [{"b64_json": encoded}]})
                sniff.assert_called_once_with(raw)
                self.assertEqual(result, f"![image_1](data:{mime};base64,{encoded})")

    def test_unknown_or_bad_base64_keeps_png_compatibility(self):
        encoded = base64.b64encode(b"unknown").decode("ascii")
        with mock.patch("utils.image_media.sniff_image_media", side_effect=ValueError("unknown media")):
            self.assertEqual(build_chat_image_markdown_content({"data": [{"b64_json": encoded}]}),
                             f"![image_1](data:image/png;base64,{encoded})")
        with mock.patch("utils.image_media.sniff_image_media") as sniff:
            self.assertEqual(build_chat_image_markdown_content({"data": [{"b64_json": "invalid!"}]}),
                             "![image_1](data:image/png;base64,invalid!)")
        sniff.assert_not_called()


if __name__ == "__main__":
    unittest.main()
