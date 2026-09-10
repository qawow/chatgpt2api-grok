from __future__ import annotations

import unittest
from unittest.mock import patch

from services.openai_backend_api import (
    OpenAIBackendAPI,
    is_sentinel_proof_rejected,
)
from utils.helper import UpstreamHTTPError


class SentinelProofRejectedTests(unittest.TestCase):
    def test_conversation_403_empty_body_is_proof_rejection(self) -> None:
        err = UpstreamHTTPError("/backend-api/f/conversation", 403, "")
        self.assertTrue(is_sentinel_proof_rejected(err))

    def test_non_403_or_other_context_is_not(self) -> None:
        self.assertFalse(is_sentinel_proof_rejected(
            UpstreamHTTPError("/backend-api/f/conversation", 401, "")))
        self.assertFalse(is_sentinel_proof_rejected(
            UpstreamHTTPError("/backend-api/conversation/abc", 403, "")))
        self.assertFalse(is_sentinel_proof_rejected(RuntimeError("403")))

    def test_file_stream_denied_403_is_not_proof_rejection(self) -> None:
        err = UpstreamHTTPError(
            "/backend-api/files/file-x/content", 403, {"detail": "File stream access denied."})
        self.assertFalse(is_sentinel_proof_rejected(err))


class _FakeResponse:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _make_api() -> OpenAIBackendAPI:
    return OpenAIBackendAPI(access_token="fake-token-for-unit-test")


class PictureConversationPowRetryTests(unittest.TestCase):
    def _patched_api(self, start_side_effects):
        from unittest.mock import MagicMock
        api = _make_api()
        start_mock = MagicMock(side_effect=start_side_effects)
        patches = [
            patch.object(api, "_bootstrap", lambda: None),
            patch.object(api, "_get_chat_requirements", lambda: object()),
            patch.object(api, "_prepare_image_conversation", lambda *a, **k: "conduit"),
            patch.object(api, "_start_image_generation", start_mock),
            patch("services.openai_backend_api.iter_sse_payloads", lambda resp: iter(["chunk"])),
        ]
        return api, patches, start_mock

    def test_proof_rejection_resets_bootstrap_cache_and_retries_once(self) -> None:
        err = UpstreamHTTPError("/backend-api/f/conversation", 403, "")
        api, patches, start_mock = self._patched_api([err, _FakeResponse()])
        with patch("services.openai_backend_api.reset_pow_bootstrap_cache") as reset_mock:
            for p in patches:
                p.start()
            try:
                chunks = list(api._stream_picture_conversation("prompt", "gpt-image-2", []))
            finally:
                for p in patches:
                    p.stop()
        self.assertEqual(chunks, ["chunk"])
        reset_mock.assert_called_once()
        self.assertEqual(start_mock.call_count, 2)

    def test_second_rejection_raises(self) -> None:
        err = UpstreamHTTPError("/backend-api/f/conversation", 403, "")
        api, patches, start_mock = self._patched_api([err, err])
        with patch("services.openai_backend_api.reset_pow_bootstrap_cache") as reset_mock:
            for p in patches:
                p.start()
            try:
                with self.assertRaises(UpstreamHTTPError):
                    list(api._stream_picture_conversation("prompt", "gpt-image-2", []))
            finally:
                for p in patches:
                    p.stop()
        reset_mock.assert_called_once()
        self.assertEqual(start_mock.call_count, 2)

    def test_non_proof_error_does_not_retry(self) -> None:
        err = UpstreamHTTPError("/backend-api/f/conversation", 500, "boom")
        api, patches, start_mock = self._patched_api([err])
        with patch("services.openai_backend_api.reset_pow_bootstrap_cache") as reset_mock:
            for p in patches:
                p.start()
            try:
                with self.assertRaises(UpstreamHTTPError):
                    list(api._stream_picture_conversation("prompt", "gpt-image-2", []))
            finally:
                for p in patches:
                    p.stop()
        reset_mock.assert_not_called()
        self.assertEqual(start_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
