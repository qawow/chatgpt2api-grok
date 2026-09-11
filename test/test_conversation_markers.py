from __future__ import annotations

import unittest

from services.protocol.conversation import (
    assistant_message_text,
    iter_conversation_payloads,
)


class AssistantMessageTextTests(unittest.TestCase):
    def test_parts_text_returned(self) -> None:
        msg = {"author": {"role": "assistant"}, "content": {"content_type": "text", "parts": ["hello"]}}
        self.assertEqual(assistant_message_text(msg), "hello")

    def test_code_cell_text_returned(self) -> None:
        msg = {"author": {"role": "assistant"},
               "content": {"content_type": "code", "text": "print(1)"}}
        self.assertEqual(assistant_message_text(msg), "print(1)")

    def test_skipped_mainline_marker_filtered(self) -> None:
        # 生图回合的隐藏编排 cell（上游实测形态）
        msg = {
            "author": {"role": "assistant"},
            "content": {
                "content_type": "code",
                "language": "python3",
                "text": '{"skipped_mainline":true}',
            },
            "status": "in_progress",
        }
        self.assertEqual(assistant_message_text(msg), "")

    def test_marker_with_whitespace_filtered(self) -> None:
        msg = {"author": {"role": "assistant"},
               "content": {"content_type": "code", "text": '  {"skipped_mainline":false}'}}
        self.assertEqual(assistant_message_text(msg), "")


class PayloadMarkerStreamTests(unittest.TestCase):
    def test_marker_payload_produces_no_delta(self) -> None:
        payloads = [
            '{"v":{"message":{"id":"m1","author":{"role":"assistant"},'
            '"content":{"content_type":"code","text":"{\\"skipped_mainline\\":true}"},'
            '"status":"in_progress"}}}',
            '[DONE]',
        ]
        deltas = [
            str(ev.get("delta") or "")
            for ev in iter_conversation_payloads(iter(payloads))
            if ev.get("type") == "conversation.delta"
        ]
        self.assertTrue(all("skipped_mainline" not in d for d in deltas))


if __name__ == "__main__":
    unittest.main()
