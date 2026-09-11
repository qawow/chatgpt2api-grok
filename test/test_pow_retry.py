from __future__ import annotations

import unittest
from unittest.mock import patch

from services.openai_backend_api import (
    OpenAIBackendAPI,
    is_sentinel_proof_rejected,
)
from utils.helper import UpstreamHTTPError
from utils.pow import _fnv1a32, _pow_generate, build_pow_config


class PowAlgorithmTests(unittest.TestCase):
    def test_fnv1a32_matches_reference_vector(self) -> None:
        # 与注册引擎 _SentinelTokenGenerator._fnv1a32 的同一算法互证
        h = 2166136261
        for ch in "abc":
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= (h >> 16)
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= (h >> 13)
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= (h >> 16)
        self.assertEqual(_fnv1a32("abc"), f"{h & 0xFFFFFFFF:08x}")
        self.assertEqual(len(_fnv1a32("seed")), 8)

    def test_pow_generate_appends_sync_suffix(self) -> None:
        config = build_pow_config("ua-test", script_sources=["https://chatgpt.com/backend-api/sentinel/sdk.js"])
        answer, solved = _pow_generate("0.5", "ffffff", config)
        self.assertTrue(solved)
        self.assertTrue(answer.endswith("~S"))

    def test_pow_generate_respects_difficulty(self) -> None:
        config = build_pow_config("ua-test")
        answer, solved = _pow_generate("0.123456", "069976", config)
        self.assertTrue(solved)
        # 解必须真的满足校验：fnv(seed + answer_without_suffix) 前缀不超过 difficulty
        raw = answer[:-2]
        self.assertLessEqual(_fnv1a32("0.123456" + raw)[:6], "069976")

    def test_pow_generate_unsolvable_returns_fallback(self) -> None:
        config = build_pow_config("ua-test")
        answer, solved = _pow_generate("0.5", "00000000", config, limit=100)
        self.assertFalse(solved)
        self.assertTrue(answer.startswith("wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"))


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
            patch.object(api, "_get_chat_requirements", lambda **kw: object()),
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
