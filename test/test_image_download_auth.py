from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from services.openai_backend_api import (
    OpenAIBackendAPI,
    is_file_stream_denied,
    needs_chatgpt_file_auth,
)
from utils.helper import UpstreamHTTPError


class ImageDownloadAuthTests(unittest.TestCase):
    def test_estuary_and_files_urls_need_auth(self) -> None:
        self.assertTrue(
            needs_chatgpt_file_auth(
                "https://chatgpt.com/backend-api/estuary/content?id=file_1&sig=abc"
            )
        )
        self.assertTrue(
            needs_chatgpt_file_auth("https://chatgpt.com/backend-api/files/file_1/download")
        )
        self.assertTrue(
            needs_chatgpt_file_auth(
                "https://chatgpt.com/backend-api/conversation/c1/attachment/a1/download"
            )
        )
        self.assertFalse(
            needs_chatgpt_file_auth("https://files.oaiusercontent.com/file_1?se=1&sig=abc")
        )
        self.assertFalse(
            needs_chatgpt_file_auth(
                "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
            )
        )

    def test_file_stream_denied_detects_upstream_body(self) -> None:
        exc = UpstreamHTTPError(
            "image_download",
            403,
            {"detail": "File stream access denied."},
        )
        self.assertTrue(is_file_stream_denied(exc))
        self.assertFalse(is_file_stream_denied(UpstreamHTTPError("image_download", 403, "nope")))
        self.assertFalse(is_file_stream_denied(RuntimeError("image_download failed: HTTP 403")))

    def test_estuary_download_uses_authenticated_session(self) -> None:
        auth = MagicMock()
        auth.headers = {}
        resource = MagicMock()
        resource.headers = {}
        ok = MagicMock()
        ok.status_code = 200
        ok.content = b"PNG"
        ok.headers = {}
        auth.get.return_value = ok

        with (
            patch("services.openai_backend_api.account_service.get_account", return_value={}),
            patch(
                "services.openai_backend_api.create_cffi_session",
                side_effect=[auth, resource],
            ),
        ):
            backend = OpenAIBackendAPI(access_token="tok")
            try:
                images = backend.download_image_bytes(
                    ["https://chatgpt.com/backend-api/estuary/content?id=file_1&sig=abc"]
                )
            finally:
                backend.close()

        self.assertEqual(images, [b"PNG"])
        auth.get.assert_called()
        resource.get.assert_not_called()
        headers = auth.get.call_args.kwargs.get("headers") or {}
        self.assertTrue(str(headers.get("Authorization") or "").startswith("Bearer "))

    def test_cdn_403_file_stream_denied_retries_auth_session(self) -> None:
        auth = MagicMock()
        auth.headers = {}
        resource = MagicMock()
        resource.headers = {}
        denied = UpstreamHTTPError(
            "image_download",
            403,
            {"detail": "File stream access denied."},
        )
        resource.get.side_effect = denied
        ok = MagicMock()
        ok.status_code = 200
        ok.content = b"PNG"
        ok.headers = {}
        auth.get.return_value = ok

        with (
            patch("services.openai_backend_api.account_service.get_account", return_value={}),
            patch(
                "services.openai_backend_api.create_cffi_session",
                side_effect=[auth, resource],
            ),
        ):
            backend = OpenAIBackendAPI(access_token="tok")
            try:
                images = backend.download_image_bytes(
                    ["https://files.oaiusercontent.com/file_1?se=1&sig=abc"]
                )
            finally:
                backend.close()

        self.assertEqual(images, [b"PNG"])
        resource.get.assert_called()
        auth.get.assert_called()
