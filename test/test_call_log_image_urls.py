"""/v1/chat/completions 与 /v1/responses 的调用日志必须带上图片 URL。

这两个端点把图片以 base64 内联返回（markdown data URI / image_generation_call），
公开载荷里没有 ``url`` 键，日志收集器因此一张图都拿不到，日志管理页就不显示。
处理器改为附带内部字段 ``_image_urls``：日志收集它，发给客户端前剥掉。
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from services import log_service as log_module
from services.log_service import LoggedCall, _collect_urls, _strip_internal_response_fields
from services.protocol import openai_v1_chat_complete as chat
from services.protocol.conversation import ImageOutput
from utils.helper import image_result_urls

IDENTITY = {"id": "admin", "name": "管理员", "role": "admin"}
STORED = "/images/2026/09/25/1_abc.png"
RESULT = {"created": 1, "data": [{"b64_json": "aGk=", "url": STORED, "revised_prompt": "cat"}]}


def _run(call: LoggedCall, handler):
    return asyncio.run(call.run(handler))


class ImageResultUrlsTests(unittest.TestCase):
    def test_collects_stored_urls_only(self) -> None:
        result = {"data": [
            {"b64_json": "x", "url": STORED},
            {"b64_json": "y", "url": "data:image/png;base64,eQ=="},
            {"b64_json": "z", "url": STORED},
            {"b64_json": "w"},
        ]}
        self.assertEqual(image_result_urls(result), [STORED])

    def test_internal_field_is_collected_and_stripped(self) -> None:
        payload = {"id": "c1", "choices": [], "_image_urls": [STORED]}
        self.assertEqual(_collect_urls(payload), [STORED])
        self.assertNotIn("_image_urls", _strip_internal_response_fields(payload))


class ChatCompletionLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logged: list[dict] = []
        patcher = mock.patch.object(
            log_module.log_service, "add",
            side_effect=lambda type, summary="", detail=None, **_: self.logged.append(detail or {}),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_non_stream_chat_logs_urls_and_hides_field(self) -> None:
        with mock.patch.object(chat, "collect_image_outputs", return_value=RESULT), \
                mock.patch.object(chat, "stream_image_outputs_with_pool", return_value=iter(())):
            call = LoggedCall(IDENTITY, "/v1/chat/completions", "gpt-image-2.5", "文生图")
            body = {"model": "gpt-image-2.5", "messages": [{"role": "user", "content": "cat"}]}
            response = _run(call, lambda: chat.image_chat_response(body))

        self.assertNotIn("_image_urls", response)
        self.assertIn("data:image/png;base64,aGk=", response["choices"][0]["message"]["content"])
        self.assertEqual(self.logged[-1].get("urls"), [STORED])

    def test_stream_chat_logs_urls(self) -> None:
        outputs = [ImageOutput(kind="result", model="gpt-image-2.5", index=1, total=1, data=RESULT["data"])]
        call = LoggedCall(IDENTITY, "/v1/chat/completions", "gpt-image-2.5", "文生图")
        chunks = list(call.stream(chat.stream_image_chat_completion(outputs, "gpt-image-2.5")))

        self.assertTrue(all("_image_urls" not in chunk for chunk in chunks))
        self.assertEqual(self.logged[-1].get("urls"), [STORED])

    def test_responses_completed_event_carries_urls(self) -> None:
        from services.protocol import openai_v1_response as responses

        outputs = [ImageOutput(kind="result", model="gpt-image-2.5", index=1, total=1, data=RESULT["data"])]
        events = list(responses.stream_image_response(outputs, "cat", "gpt-image-2.5", 0, None, "auto"))
        completed = responses.collect_response(iter(events))

        self.assertEqual(_collect_urls(completed), [STORED])
        self.assertNotIn("_image_urls", json.dumps(_strip_internal_response_fields(completed)))


if __name__ == "__main__":
    unittest.main()
