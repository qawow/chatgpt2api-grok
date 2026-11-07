"""Offline regression tests for frame-counted ChatGPT web image resumption."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from curl_cffi import requests

from services.image_task_control import ImageTaskControl
from services.openai_backend_api import (
    ChatRequirements,
    OpenAIBackendAPI,
    ToolQuotaError,
    is_resumable_image_stream_error,
)
from services.protocol import conversation
from services.protocol.conversation import ConversationRequest
from utils.helper import SseProtocolError, SseStreamTimeoutError, UpstreamHTTPError, sse_json_stream
from utils.image_models import WEB_IMAGE_MODEL

CID = "unit-image-conversation"
FILE_ID = "file_00000000aaaabbbbccccddddeeeeffff"


def _http_error(status: int) -> requests.exceptions.HTTPError:
    """A transport error carrying an upstream HTTP status."""
    response = Mock()
    response.status_code = status
    error = requests.exceptions.HTTPError(f"HTTP {status}")
    error.response = response
    error.status_code = status
    return error


def wire(event: str, data: object, *, multiline: bool = False) -> list[bytes]:
    payload = data if data == "[DONE]" else json.dumps(data, ensure_ascii=True)
    values = [payload]
    if multiline:
        split = payload.index(",") + 1
        values = [payload[:split], payload[split:]]
    lines = [f"event: {event}".encode()] if event else []
    lines.extend(f"data: {value}".encode("utf-8") for value in values)
    return lines + [b""]


class FakeSseResponse:
    def __init__(self, lines=(), error=None, *, status=200, body=None):
        self.lines = list(lines)
        self.error = error
        self.status_code = status
        self.body = {} if body is None else body
        self.text = json.dumps(self.body)
        self.closed = False
        self.close_count = 0

    def iter_lines(self):
        if self.closed:
            raise RuntimeError("Resumed response was closed before it could be read")
        yield from self.lines
        if self.error is not None:
            raise self.error

    def json(self):
        return self.body

    def close(self):
        self.closed = True
        self.close_count += 1


def api_stub(initial: FakeSseResponse) -> OpenAIBackendAPI:
    # Do not initialize account identity, sessions, bootstrap, or live configuration.
    api = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
    api.access_token = "unit-test-only-access-token"
    api.base_url = "https://example.invalid"
    api.task_control = None
    api.progress_callback = None
    api._headers = Mock(side_effect=lambda _path, headers=None: dict(headers or {}))
    api._bootstrap = Mock()
    api._get_chat_requirements = Mock(return_value=ChatRequirements(
        token="unit-requirements", proof_token="unit-proof", turnstile_token="unit-turnstile", so_token="unit-so",
    ))
    api._prepare_image_conversation = Mock(return_value="prepare-conduit-not-resume-token")
    api._upload_image = Mock()
    api._start_image_generation = Mock(return_value=initial)
    api.session = Mock()
    return api


def token_frame(token: str):
    return wire("message", {"type": "resume_conversation_token", "token": token, "conversation_id": CID})


def tool_output():
    return {
        "conversation_id": CID,
        "message": {
            "author": {"role": "tool"},
            "metadata": {"async_task_type": "image_gen"},
            "content": {"content_type": "multimodal_text", "parts": [{
                "content_type": "image_asset_pointer", "asset_pointer": f"file-service://{FILE_ID}",
            }]},
        },
    }


class WebImageTransportTests(unittest.TestCase):
    def test_resume_counts_complete_frames_uses_latest_token_and_keeps_decoder(self):
        initial = FakeSseResponse(
            wire("delta_encoding", "v1")
            + wire("delta", {"c": 0, "p": "", "o": "add", "v": {
                "conversation_id": CID,
                "message": {"author": {"role": "assistant"}, "content": {"content_type": "text", "parts": ["hi"]}},
            }})
            + token_frame("unit-resume-one")
            + wire("delta", {"p": "/message/content/parts/0", "o": "append", "v": " there"}, multiline=True)
            + token_frame("unit-resume-two/ +="),
            requests.exceptions.RequestException("unit connection reset"),
        )
        resumed = FakeSseResponse(wire("delta", {"v": "!"}) + wire("message", "[DONE]"))
        api = api_stub(initial)
        api.session.post.return_value = resumed

        payloads = list(api._stream_picture_conversation("draw a cat", WEB_IMAGE_MODEL, []))

        self.assertEqual(api._start_image_generation.call_count, 1)
        api._prepare_image_conversation.assert_called_once()
        api._get_chat_requirements.assert_called_once()
        api.session.post.assert_called_once()
        url = api.session.post.call_args.args[0]
        kwargs = api.session.post.call_args.kwargs
        self.assertEqual(url, "https://example.invalid/backend-api/f/conversation/resume")
        self.assertEqual(kwargs["json"], {"conversation_id": CID, "offset": 5})
        self.assertEqual(kwargs["headers"]["X-Conduit-Token"], "unit-resume-two/ +=")
        self.assertNotIn("x-oai-mcp-form-version", kwargs["headers"])
        self.assertEqual(kwargs["headers"]["X-Oai-Turn-Trace-Id"], api._start_image_generation.call_args.kwargs["turn_trace_id"])
        self.assertTrue(kwargs["stream"])
        self.assertTrue(initial.closed)
        self.assertTrue(resumed.closed)
        self.assertEqual(payloads[-1], "[DONE]")
        decoded = [json.loads(item) for item in payloads if item != "[DONE]"]
        messages = [item["message"] for item in decoded if isinstance(item, dict) and "message" in item]
        self.assertEqual(messages[-1]["content"]["parts"], ["hi there!"])
        public = "\n".join(payloads)
        self.assertNotIn("unit-resume-one", public)
        self.assertNotIn("unit-resume-two", public)
        checkpoints = [item for item in decoded if item.get("type") == "conversation_checkpoint"]
        self.assertEqual(checkpoints, [{"type": "conversation_checkpoint", "conversation_id": CID}] * 2)

    def test_normal_eof_is_not_rewritten_to_done_or_reposted(self):
        initial = FakeSseResponse(token_frame("unit-resume"))
        api = api_stub(initial)
        payloads = list(api._stream_picture_conversation("cat", WEB_IMAGE_MODEL, []))
        self.assertNotIn("[DONE]", payloads)
        api.session.post.assert_not_called()
        self.assertEqual(api._start_image_generation.call_count, 1)
        self.assertTrue(initial.closed)

    def test_missing_token_propagates_transport_error_with_known_conversation(self):
        error = requests.exceptions.RequestException("unit reset without token")
        initial = FakeSseResponse(wire("message", {"conversation_id": CID}), error)
        api = api_stub(initial)
        with self.assertRaises(requests.exceptions.RequestException) as caught:
            list(api._stream_picture_conversation("cat", WEB_IMAGE_MODEL, []))
        self.assertIs(caught.exception, error)
        self.assertEqual(caught.exception.conversation_id, CID)
        api.session.post.assert_not_called()
        self.assertEqual(api._start_image_generation.call_count, 1)
        self.assertTrue(initial.closed)

    def test_transient_resume_failure_returns_original_transport_failure_and_closes_response(self):
        error = requests.exceptions.RequestException("unit reset")
        initial = FakeSseResponse(token_frame("unit-resume"), error)
        unsupported = FakeSseResponse(status=503, body={"detail": "unit transient resume failure"})
        api = api_stub(initial)
        api.session.post.return_value = unsupported
        with self.assertRaises(requests.exceptions.RequestException) as caught:
            list(api._stream_picture_conversation("cat", WEB_IMAGE_MODEL, []))
        self.assertIs(caught.exception, error)
        self.assertEqual(caught.exception.conversation_id, CID)
        self.assertIsInstance(caught.exception.__cause__, UpstreamHTTPError)
        self.assertTrue(initial.closed)
        self.assertTrue(unsupported.closed)
        self.assertEqual(api._start_image_generation.call_count, 1)
        api.session.post.assert_called_once()

    def test_resume_authentication_or_quota_error_is_not_hidden(self):
        for status in (400, 401, 403, 404, 405, 410, 422, 429):
            with self.subTest(status=status):
                initial = FakeSseResponse(token_frame("unit-resume"), requests.exceptions.RequestException("unit reset"))
                failed = FakeSseResponse(status=status, body={"detail": "unit explicit error"})
                api = api_stub(initial)
                api.session.post.return_value = failed
                with self.assertRaises(UpstreamHTTPError) as caught:
                    list(api._stream_picture_conversation("cat", WEB_IMAGE_MODEL, []))
                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(caught.exception.conversation_id, CID)
                self.assertTrue(failed.closed)
                self.assertEqual(api._start_image_generation.call_count, 1)
                api._get_chat_requirements.assert_called_once()
                api.session.post.assert_called_once()

    def test_second_stream_failure_cannot_resume_a_second_time(self):
        initial = FakeSseResponse(token_frame("unit-resume"), requests.exceptions.RequestException("first reset"))
        error = requests.exceptions.RequestException("second reset")
        resumed = FakeSseResponse(token_frame("unit-new-resume"), error)
        api = api_stub(initial)
        api.session.post.return_value = resumed
        with self.assertRaises(requests.exceptions.RequestException) as caught:
            list(api._stream_picture_conversation("cat", WEB_IMAGE_MODEL, []))
        self.assertIs(caught.exception, error)
        self.assertTrue(initial.closed)
        self.assertTrue(resumed.closed)
        api.session.post.assert_called_once()
        self.assertEqual(api._start_image_generation.call_count, 1)

    def test_resume_and_initial_stream_share_one_total_budget(self):
        initial = FakeSseResponse()
        resumed = FakeSseResponse()
        api = api_stub(initial)
        api.session.post.return_value = resumed
        clock = [100.0]
        budgets = []

        def body(response, *, state, total_timeout_secs):
            budgets.append(total_timeout_secs)
            if response is initial:
                state.conversation_id, state.resume_token, state.offset = CID, "unit-resume", 2
                yield json.dumps({"type": "conversation_checkpoint", "conversation_id": CID})
                clock[0] += 2.0
                raise requests.exceptions.RequestException("unit reset")
            yield "[DONE]"

        with (
            patch("services.openai_backend_api.config", SimpleNamespace(image_sse_total_timeout_secs=5.0)),
            patch("services.openai_backend_api.time.monotonic", side_effect=lambda: clock[0]),
            patch.object(api, "_iter_image_sse_payloads", side_effect=body),
        ):
            list(api._stream_picture_conversation("cat", WEB_IMAGE_MODEL, []))
        self.assertEqual(budgets, [5.0, 3.0])
        self.assertEqual(api.session.post.call_args.kwargs["timeout"], 3.0)
        self.assertTrue(resumed.closed)

    def test_task_cancellation_does_not_resume(self):
        initial = FakeSseResponse()
        api = api_stub(initial)
        api.task_control = ImageTaskControl(30.0)

        def body(_response, *, state, total_timeout_secs):
            state.conversation_id, state.resume_token = CID, "unit-resume"
            yield json.dumps({"conversation_id": CID})
            api.task_control.cancelled.set()
            raise SseStreamTimeoutError("unit idle timeout")

        with patch.object(api, "_iter_image_sse_payloads", side_effect=body):
            with self.assertRaises(TimeoutError) as caught:
                list(api._stream_picture_conversation("cat", WEB_IMAGE_MODEL, []))
        self.assertNotIsInstance(caught.exception, SseStreamTimeoutError)
        api.session.post.assert_not_called()
        self.assertTrue(initial.closed)

    def test_resumable_statuses_match_the_web_client(self):
        """捕获客户端（bundle 418851）只在 408/409/425/429/502/504 时 resume。"""
        from services.openai_backend_api import RESUMABLE_HTTP_STATUSES

        self.assertEqual(RESUMABLE_HTTP_STATUSES, frozenset({408, 409, 425, 429, 502, 504}))
        for status in RESUMABLE_HTTP_STATUSES:
            with self.subTest(status=status):
                self.assertTrue(is_resumable_image_stream_error(_http_error(status)))
        for status in (400, 401, 403, 404, 405, 422, 500, 501, 503):
            with self.subTest(status=status):
                self.assertFalse(is_resumable_image_stream_error(_http_error(status)))

    def test_resume_backoff_follows_client_curve(self):
        """300ms × 1.5^attempt，上限 30s，抖动区间 [0.5, 1.0]。"""
        import random as _random

        from services.openai_backend_api import (
            RESUME_MAX_DELAY_SECS,
            RESUME_MIN_DELAY_SECS,
            RESUME_RETRY_FACTOR,
            resume_backoff_secs,
        )

        self.assertEqual(RESUME_MIN_DELAY_SECS, 0.3)
        self.assertEqual(RESUME_RETRY_FACTOR, 1.5)
        rng = _random.Random(7)
        previous_cap = 0.0
        for attempt in range(1, 13):
            nominal = min(RESUME_MIN_DELAY_SECS * (RESUME_RETRY_FACTOR ** attempt), RESUME_MAX_DELAY_SECS)
            value = resume_backoff_secs(attempt, rng=rng)
            with self.subTest(attempt=attempt):
                self.assertGreaterEqual(value, nominal * 0.5 - 1e-9)
                self.assertLessEqual(value, nominal + 1e-9)
            previous_cap = nominal
        self.assertEqual(previous_cap, RESUME_MAX_DELAY_SECS)  # cap reached by attempt 12


    def test_exhausted_shared_budget_does_not_resume(self):
        initial = FakeSseResponse()
        api = api_stub(initial)
        clock = [100.0]

        def body(_response, *, state, total_timeout_secs):
            state.conversation_id, state.resume_token = CID, "unit-resume"
            yield json.dumps({"conversation_id": CID})
            clock[0] += 2.0
            raise SseStreamTimeoutError("unit total timeout")

        with (
            patch("services.openai_backend_api.config", SimpleNamespace(image_sse_total_timeout_secs=1.0)),
            patch("services.openai_backend_api.time.monotonic", side_effect=lambda: clock[0]),
            patch.object(api, "_iter_image_sse_payloads", side_effect=body),
        ):
            with self.assertRaises(SseStreamTimeoutError):
                list(api._stream_picture_conversation("cat", WEB_IMAGE_MODEL, []))
        api.session.post.assert_not_called()
        self.assertTrue(initial.closed)

    def test_unknown_encoding_and_delta_before_encoding_are_not_transport_errors(self):
        for lines in (wire("delta_encoding", "v2"), wire("delta", {"v": {}})):
            with self.subTest(lines=lines):
                initial = FakeSseResponse(lines)
                api = api_stub(initial)
                with self.assertRaises(SseProtocolError):
                    list(api._stream_picture_conversation("cat", WEB_IMAGE_MODEL, []))
                api.session.post.assert_not_called()
                self.assertTrue(initial.closed)

    def test_v1_tool_quota_refusal_cannot_be_converted_to_resume(self):
        initial = FakeSseResponse(wire("delta_encoding", "v1") + wire("delta", {"v": {
            "message": {
                "author": {"role": "tool"},
                "content": {"content_type": "system_error", "name": "ChatGPTAgentToolRateLimitException", "parts": [
                    "You have reached the image generation request limit. Reset in 2 hours.",
                ]},
            },
        }}))
        api = api_stub(initial)
        with self.assertRaises(ToolQuotaError):
            list(api._stream_picture_conversation("cat", WEB_IMAGE_MODEL, []))
        api.session.post.assert_not_called()
        self.assertTrue(initial.closed)

    def test_initial_http_failure_closes_response_and_retains_web_payload_shape(self):
        failed = FakeSseResponse(status=500, body={"detail": "unit failure"})
        api = api_stub(failed)
        api.locale = SimpleNamespace(offset_min=0, timezone="UTC")
        api._client_contextual_info = Mock(return_value={})
        api.session.post.return_value = failed
        with self.assertRaises(UpstreamHTTPError):
            OpenAIBackendAPI._start_image_generation(
                api, "draw a cat", api._get_chat_requirements(), "unit-conduit", WEB_IMAGE_MODEL,
                turn_trace_id="unit-trace",
            )
        self.assertTrue(failed.closed)
        kwargs = api.session.post.call_args.kwargs
        self.assertEqual(kwargs["json"]["action"], "next")
        self.assertEqual(kwargs["json"]["model"], "auto")
        self.assertEqual(kwargs["json"]["system_hints"], ["picture_v2"])
        self.assertEqual(kwargs["headers"]["x-oai-mcp-form-version"], "2")
        self.assertEqual(kwargs["headers"]["X-Oai-Turn-Trace-Id"], "unit-trace")

    def test_resume_encoding_declaration_resets_defaults_and_channel_baseline(self):
        initial = FakeSseResponse(
            wire("delta_encoding", "v1")
            + wire("delta", {"v": {"conversation_id": CID, "message": {
                "author": {"role": "assistant"}, "content": {"content_type": "text", "parts": ["before"]},
            }}})
            + wire("delta", {"p": "/message/content/parts/0", "o": "append", "v": " reset"})
            + token_frame("unit-resume"),
            requests.exceptions.RequestException("unit reset"),
        )
        resumed = FakeSseResponse(
            wire("delta_encoding", "v1") + wire("delta", {"v": tool_output()}) + wire("message", "[DONE]"),
        )
        api = api_stub(initial)
        api.session.post.return_value = resumed
        payloads = list(api._stream_picture_conversation("cat", WEB_IMAGE_MODEL, []))
        self.assertEqual(json.loads(payloads[-2]), tool_output())
        self.assertEqual(api.session.post.call_args.kwargs["json"], {"conversation_id": CID, "offset": 4})
        self.assertEqual(api._start_image_generation.call_count, 1)
        self.assertTrue(resumed.closed)

    def test_transient_resume_http_codes_can_return_to_same_conversation_poll(self):
        for status in (408, 409, 425):
            with self.subTest(status=status):
                error = requests.exceptions.RequestException("unit reset")
                initial = FakeSseResponse(token_frame("unit-resume"), error)
                failed = FakeSseResponse(status=status, body={"detail": "unit transient failure"})
                api = api_stub(initial)
                api.session.post.return_value = failed
                with self.assertRaises(requests.exceptions.RequestException) as caught:
                    list(api._stream_picture_conversation("cat", WEB_IMAGE_MODEL, []))
                self.assertIs(caught.exception, error)
                self.assertEqual(caught.exception.conversation_id, CID)
                self.assertEqual(caught.exception.__cause__.status_code, status)
                self.assertTrue(failed.closed)
                api.session.post.assert_called_once()
                self.assertEqual(api._start_image_generation.call_count, 1)

    def test_invalid_resume_control_does_not_reuse_previous_conversation_id(self):
        initial = FakeSseResponse(
            wire("message", {"conversation_id": CID}) + wire("message", {
                "type": "resume_conversation_token", "conversation_id": None, "token": "unit-resume",
            }),
            requests.exceptions.RequestException("unit reset"),
        )
        api = api_stub(initial)
        with self.assertRaises(SseProtocolError):
            list(api._stream_picture_conversation("cat", WEB_IMAGE_MODEL, []))
        api.session.post.assert_not_called()
        self.assertTrue(initial.closed)

    def test_v1_utf16_progress_survives_final_public_sse_utf8_encoding(self):
        initial = FakeSseResponse(
            wire("delta_encoding", "v1")
            + wire("delta", {"v": {"conversation_id": CID, "message": {
                "author": {"role": "assistant"}, "content": {"content_type": "text", "parts": ["a🙂b"]},
            }}})
            + wire("delta", {"p": "/message/content/parts/0", "o": "truncate", "v": 2})
            + wire("delta", {"o": "append", "v": "\ude42"})
            + wire("message", "[DONE]"),
        )
        api = api_stub(initial)
        events = list(conversation.iter_conversation_payloads(
            api._stream_picture_conversation("cat", WEB_IMAGE_MODEL, []),
        ))
        deltas = [event["delta"] for event in events if event.get("type") == "conversation.delta"]
        self.assertIn("a\ud83d", deltas)
        chunks = list(sse_json_stream({"choices": [{"delta": {"content": value}}]} for value in deltas))
        for chunk in chunks:
            chunk.encode("utf-8", errors="strict")
        decoded = [json.loads(chunk[6:]) for chunk in chunks if chunk.startswith("data: {")]
        self.assertEqual(decoded[-1]["choices"][0]["delta"]["content"], "a🙂")
        api.session.post.assert_not_called()

    def test_caller_close_closes_active_stream_without_resume_or_prompt_retry(self):
        initial = FakeSseResponse(token_frame("unit-resume") * 80)
        api = api_stub(initial)
        stream = api._stream_picture_conversation("cat", WEB_IMAGE_MODEL, [])
        self.assertEqual(json.loads(next(stream))["conversation_id"], CID)
        stream.close()
        self.assertTrue(initial.closed)
        api.session.post.assert_not_called()
        self.assertEqual(api._start_image_generation.call_count, 1)

    def test_full_frame_resume_to_image_collector_posts_prompt_once(self):
        initial = FakeSseResponse(
            wire("delta_encoding", "v1") + token_frame("unit-resume") + wire("delta", {"v": tool_output()}, multiline=True),
            requests.exceptions.RequestException("unit reset"),
        )
        resumed = FakeSseResponse(wire("message", "[DONE]"))
        api = api_stub(initial)
        api.session.post.return_value = resumed
        api.resolve_conversation_image_urls = Mock(return_value=["https://images.example.invalid/unit"])
        api.download_image_bytes = Mock(return_value=[b"unit-original-image-bytes"])
        with (
            patch.object(conversation, "format_image_result", return_value={"data": [{"url": "http://example.test/result.png"}]}),
            patch.object(conversation, "_remove_image_conversation_later"),
        ):
            outputs = list(conversation.stream_image_outputs(api, ConversationRequest(model=WEB_IMAGE_MODEL, prompt="cat")))
        self.assertEqual(outputs[-1].kind, "result")
        self.assertEqual(outputs[-1].conversation_id, CID)
        self.assertEqual(api._start_image_generation.call_count, 1)
        api.session.post.assert_called_once()
        self.assertEqual(api.session.post.call_args.kwargs["json"], {"conversation_id": CID, "offset": 3})
        self.assertEqual(api.resolve_conversation_image_urls.call_args.args[1], [FILE_ID])
        api.download_image_bytes.assert_called_once()


if __name__ == "__main__":
    unittest.main()
