from __future__ import annotations

import unittest
from unittest.mock import patch

from services.protocol.conversation import (
    ConversationRequest,
    ImageGenerationError,
    ImageOutput,
    _generate_single_image,
    image_stream_error_message,
    is_proxy_unreachable_error,
    is_upstream_connection_error,
)

DEAD_SOCKS_ERROR = (
    "ProxyError('Unable to connect to proxy', OSError("
    "'Tunnel connection failed: 502 upstream socks failed: [Errno 111] Connection refused'))"
)


class ConnectionErrorClassifierTests(unittest.TestCase):
    def test_upstream_connection_classifies_proxy_socks_failures(self) -> None:
        cases = [
            "ConnectionPool(host='openaipublic.blob.core.windows.net', port=443): Max retries exceeded with url: /encodings/o200k_base.tiktoken (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 502 upstream socks failed: [Errno 111] Connection refused')))",
            "HTTPSConnectionPool(host='chatgpt.com', port=443): Max retries exceeded ... (Caused by ProxyError('Tunnel connection failed: 502 upstream socks failed: [Errno 111] Connection refused'))",
            "curl: (35) TLS connect error",
            "curl: (28) Operation timed out",
            "upstream image connection failed, please retry later",
        ]
        for case in cases:
            self.assertTrue(is_upstream_connection_error(case), msg=case)

    def test_proxy_unreachable_skips_sticky_retry(self) -> None:
        self.assertTrue(is_proxy_unreachable_error(DEAD_SOCKS_ERROR))
        self.assertFalse(is_proxy_unreachable_error("curl: (35) TLS connect error"))

    def test_error_message_reports_upstream_connection_failure(self) -> None:
        msg = image_stream_error_message(
            "ProxyError: Tunnel connection failed: 502 upstream socks failed: [Errno 111] Connection refused"
        )
        self.assertEqual(msg, "upstream image connection failed, please retry later")

    def test_error_message_reports_timeout_distinctly(self) -> None:
        self.assertEqual(
            image_stream_error_message("curl: (28) Operation timed out"),
            "upstream connection timed out, please retry later",
        )

    def test_non_connection_errors_pass_through(self) -> None:
        self.assertFalse(is_upstream_connection_error("content policy violation"))
        self.assertFalse(is_upstream_connection_error("token_revoked"))
        self.assertEqual(image_stream_error_message("boom"), "boom")


class ImageConnectionFailoverTests(unittest.TestCase):
    def test_dead_account_proxy_falls_back_to_direct_same_account(self) -> None:
        created: list[tuple[str, str | None]] = []

        class FakeBackend:
            def __init__(self, access_token: str = "", *, force_proxy: str | None = None) -> None:
                self.access_token = access_token
                self.force_proxy = force_proxy
                self.progress_callback = None
                created.append((access_token, force_proxy))

            def close(self) -> None:
                return None

        def fake_stream(backend, request, index, total):
            if backend.force_proxy:
                raise OSError(DEAD_SOCKS_ERROR)
            yield ImageOutput(
                kind="result",
                model=request.model,
                index=index,
                total=total,
                data=[{"url": "http://example.test/ok.png"}],
            )

        with (
            patch("services.protocol.conversation.account_service") as accounts,
            patch("services.protocol.conversation.OpenAIBackendAPI", FakeBackend),
            patch("services.protocol.conversation.stream_image_outputs", fake_stream),
            patch(
                "services.protocol.conversation.proxy_settings.list_egress_candidates",
                return_value=[("account", "socks5h://dead.example:1080"), ("direct", "")],
            ),
            patch("services.protocol.conversation.time.sleep"),
        ):
            accounts.get_available_access_token.return_value = "token-a"
            accounts.get_account.return_value = {"email": "a@example.com", "proxy": "socks5h://dead.example:1080"}
            outputs = _generate_single_image(
                ConversationRequest(model="gpt-image-2", prompt="cat"),
                1,
                1,
            )

        self.assertEqual(created, [("token-a", "socks5h://dead.example:1080"), ("token-a", "")])
        self.assertEqual(outputs[0].data[0]["url"], "http://example.test/ok.png")
        accounts.mark_image_result.assert_called_with("token-a", True)

    def test_wrapped_connection_error_also_failovers_proxy(self) -> None:
        created: list[str | None] = []

        class FakeBackend:
            def __init__(self, access_token: str = "", *, force_proxy: str | None = None) -> None:
                self.access_token = access_token
                self.force_proxy = force_proxy
                self.progress_callback = None
                created.append(force_proxy)

            def close(self) -> None:
                return None

        def fake_stream(backend, request, index, total):
            if backend.force_proxy:
                raise ImageGenerationError(DEAD_SOCKS_ERROR)
            yield ImageOutput(
                kind="result",
                model=request.model,
                index=index,
                total=total,
                data=[{"url": "http://example.test/ok.png"}],
            )

        with (
            patch("services.protocol.conversation.account_service") as accounts,
            patch("services.protocol.conversation.OpenAIBackendAPI", FakeBackend),
            patch("services.protocol.conversation.stream_image_outputs", fake_stream),
            patch(
                "services.protocol.conversation.proxy_settings.list_egress_candidates",
                return_value=[("global", "socks5h://dead.example:1080"), ("direct", "")],
            ),
            patch("services.protocol.conversation.time.sleep"),
        ):
            accounts.get_available_access_token.return_value = "token-a"
            accounts.get_account.return_value = {"email": "a@example.com"}
            outputs = _generate_single_image(
                ConversationRequest(model="gpt-image-2", prompt="cat"),
                1,
                1,
            )

        self.assertEqual(created, ["socks5h://dead.example:1080", ""])
        self.assertEqual(outputs[0].kind, "result")


if __name__ == "__main__":
    unittest.main()