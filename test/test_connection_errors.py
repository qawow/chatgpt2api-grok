from __future__ import annotations

import unittest
from unittest.mock import patch

from services.protocol.conversation import (
    ConversationRequest,
    ImageGenerationError,
    ImageOutput,
    _generate_single_image,
    image_stream_error_message,
    is_clarifying_image_followup,
    is_proxy_unreachable_error,
    is_soft_prepare_auth_error,
    is_token_invalid_error,
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

    def test_openssl_invalid_library_skips_sticky_retry(self) -> None:
        from services.protocol.conversation import is_openssl_invalid_library_error

        err = (
            "curl: (35) TLS connect error: error:00000000:invalid library (0):"
            "OPENSSL_internal:invalid library (0)"
        )
        self.assertTrue(is_openssl_invalid_library_error(err))
        self.assertTrue(is_upstream_connection_error(err))
        self.assertEqual(
            image_stream_error_message(err),
            "upstream image connection failed, please retry later",
        )

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

    def test_token_invalid_includes_parse_and_http_401(self) -> None:
        self.assertTrue(
            is_token_invalid_error(
                'chat_requirements_prepare failed: status=401, body={"error": '
                '{"message": "Could not parse your authentication token. Please try signing in again.", '
                '"code": "unauthorized_unknown"}}'
            )
        )
        self.assertTrue(is_token_invalid_error("token invalidated (chat_requirements_prepare)"))
        self.assertTrue(is_soft_prepare_auth_error("token invalidated (chat_requirements_prepare)"))
        self.assertFalse(is_soft_prepare_auth_error("token invalidated (/backend-api/me)"))
        self.assertFalse(is_token_invalid_error("content policy violation"))
        self.assertEqual(
            image_stream_error_message("token invalidated (chat_requirements_prepare)"),
            "upstream session expired, please retry",
        )

    def test_clarifying_followup_is_not_policy(self) -> None:
        question = "你更喜欢她的眼睛偏梦幻紫还是更明亮通透的紫？"
        self.assertTrue(is_clarifying_image_followup(question))
        self.assertTrue(is_clarifying_image_followup("Which do you prefer, A or B?"))
        self.assertFalse(is_clarifying_image_followup("This violates our content policy."))
        self.assertFalse(is_clarifying_image_followup('{"size":"1920x1088","n":1}'))

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

    def test_openssl_invalid_library_retries_same_proxy(self) -> None:
        created: list[str | None] = []
        err = (
            "curl: (35) TLS connect error: error:00000000:invalid library (0):"
            "OPENSSL_internal:invalid library (0)"
        )

        class FakeBackend:
            def __init__(self, access_token: str = "", *, force_proxy: str | None = None) -> None:
                self.access_token = access_token
                self.force_proxy = force_proxy
                self.progress_callback = None
                created.append(force_proxy)

            def close(self) -> None:
                return None

        def fake_stream(backend, request, index, total):
            if created.count(backend.force_proxy) < 2:
                raise OSError(err)
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
                return_value=[("global", "socks5h://live.example:1080"), ("direct", "")],
            ),
            patch("services.protocol.conversation.time.sleep") as slept,
        ):
            accounts.get_available_access_token.return_value = "token-a"
            accounts.get_account.return_value = {"email": "a@example.com"}
            outputs = _generate_single_image(
                ConversationRequest(model="gpt-image-2", prompt="cat"),
                1,
                1,
            )

        self.assertEqual(created, ["socks5h://live.example:1080", "socks5h://live.example:1080"])
        self.assertEqual(outputs[0].kind, "result")
        slept.assert_called()

    def test_prepare_401_does_not_remove_account(self) -> None:
        class FakeBackend:
            def __init__(self, access_token: str = "", *, force_proxy: str | None = None) -> None:
                self.access_token = access_token
                self.progress_callback = None

            def close(self) -> None:
                return None

        def fake_stream(backend, request, index, total):
            raise RuntimeError("token invalidated (chat_requirements_prepare)")
            yield  # pragma: no cover

        def get_token(**kwargs):
            excluded = kwargs.get("excluded_tokens") or set()
            if "token-a" in excluded:
                raise RuntimeError("no available ChatGPT account")
            return "token-a"

        with (
            patch("services.protocol.conversation.account_service") as accounts,
            patch("services.protocol.conversation.OpenAIBackendAPI", FakeBackend),
            patch("services.protocol.conversation.stream_image_outputs", fake_stream),
            patch(
                "services.protocol.conversation.proxy_settings.list_egress_candidates",
                return_value=[("global", "socks5h://live.example:1080")],
            ),
        ):
            accounts.get_available_access_token.side_effect = get_token
            accounts.get_account.return_value = {"email": "a@example.com"}
            accounts.refresh_access_token.return_value = None
            # /me 没有确认吊销（TLS/device-id 抖动导致的假 401）→ 只换号，不删号。
            accounts.confirm_token_revoked.return_value = False
            with self.assertRaises(ImageGenerationError) as ctx:
                _generate_single_image(
                    ConversationRequest(model="gpt-image-2", prompt="cat"),
                    1,
                    1,
                )

        self.assertIn("session expired", str(ctx.exception).lower())
        accounts.remove_invalid_token.assert_not_called()
        accounts.mark_image_result.assert_called_with("token-a", False)

    def test_prepare_401_removes_account_when_me_confirms_revoke(self) -> None:
        """prepare 401 且 /me 也 401 → 确认吊销，必须清掉这行，否则下次还会选中它。"""

        class FakeBackend:
            def __init__(self, access_token: str = "", *, force_proxy: str | None = None) -> None:
                self.access_token = access_token
                self.progress_callback = None

            def close(self) -> None:
                return None

        def fake_stream(backend, request, index, total):
            raise RuntimeError("token invalidated (chat_requirements_prepare)")
            yield  # pragma: no cover

        def get_token(**kwargs):
            excluded = kwargs.get("excluded_tokens") or set()
            if "token-a" in excluded:
                raise RuntimeError("no available ChatGPT account")
            return "token-a"

        with (
            patch("services.protocol.conversation.account_service") as accounts,
            patch("services.protocol.conversation.OpenAIBackendAPI", FakeBackend),
            patch("services.protocol.conversation.stream_image_outputs", fake_stream),
            patch(
                "services.protocol.conversation.proxy_settings.list_egress_candidates",
                return_value=[("global", "socks5h://live.example:1080")],
            ),
        ):
            accounts.get_available_access_token.side_effect = get_token
            accounts.get_account.return_value = {"email": "a@example.com"}
            accounts.refresh_access_token.return_value = None
            accounts.confirm_token_revoked.return_value = True
            with self.assertRaises(ImageGenerationError):
                _generate_single_image(
                    ConversationRequest(model="gpt-image-2", prompt="cat"),
                    1,
                    1,
                )

        accounts.confirm_token_revoked.assert_called_with("token-a")
        self.assertEqual(
            [call.args[0] for call in accounts.remove_invalid_token.call_args_list],
            ["token-a"],
        )

    def test_prepare_401_fails_over_to_another_account(self) -> None:
        created: list[str] = []

        class FakeBackend:
            def __init__(self, access_token: str = "", *, force_proxy: str | None = None) -> None:
                self.access_token = access_token
                self.progress_callback = None
                created.append(access_token)

            def close(self) -> None:
                return None

        def fake_stream(backend, request, index, total):
            if backend.access_token == "token-a":
                raise RuntimeError("token invalidated (chat_requirements_prepare)")
            yield ImageOutput(
                kind="result",
                model=request.model,
                index=index,
                total=total,
                data=[{"url": "http://example.test/ok.png"}],
            )

        def get_token(**kwargs):
            excluded = kwargs.get("excluded_tokens") or set()
            if "token-a" in excluded:
                return "token-b"
            return "token-a"

        with (
            patch("services.protocol.conversation.account_service") as accounts,
            patch("services.protocol.conversation.OpenAIBackendAPI", FakeBackend),
            patch("services.protocol.conversation.stream_image_outputs", fake_stream),
            patch(
                "services.protocol.conversation.proxy_settings.list_egress_candidates",
                return_value=[("global", "socks5h://live.example:1080")],
            ),
        ):
            accounts.get_available_access_token.side_effect = get_token
            accounts.get_account.side_effect = lambda token: {"email": f"{token}@example.com"}
            accounts.refresh_access_token.return_value = None
            accounts.confirm_token_revoked.return_value = False
            outputs = _generate_single_image(
                ConversationRequest(model="gpt-image-2", prompt="cat"),
                1,
                1,
            )

        self.assertEqual(created, ["token-a", "token-b"])
        self.assertEqual(outputs[0].kind, "result")
        accounts.remove_invalid_token.assert_not_called()

    def test_progress_events_do_not_block_openssl_retry(self) -> None:
        created: list[str | None] = []
        err = (
            "curl: (35) TLS connect error: error:00000000:invalid library (0):"
            "OPENSSL_internal:invalid library (0)"
        )

        class FakeBackend:
            def __init__(self, access_token: str = "", *, force_proxy: str | None = None) -> None:
                self.access_token = access_token
                self.force_proxy = force_proxy
                self.progress_callback = None
                created.append(force_proxy)

            def close(self) -> None:
                return None

        def fake_stream(backend, request, index, total):
            yield ImageOutput(kind="progress", model=request.model, index=index, total=total, text="working")
            if created.count(backend.force_proxy) < 2:
                raise RuntimeError(err)
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
                return_value=[("global", "socks5h://live.example:1080"), ("direct", "")],
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

        self.assertEqual(created, ["socks5h://live.example:1080", "socks5h://live.example:1080"])
        self.assertEqual(outputs[-1].kind, "result")

    def test_file_ids_without_urls_do_not_surface_followup_as_policy(self) -> None:
        from services.protocol.conversation import stream_image_outputs

        class Backend:
            def resolve_conversation_image_urls(self, *args, **kwargs):
                return []

        def fake_events(*args, **kwargs):
            yield {
                "type": "conversation.completed",
                "conversation_id": "conv-1",
                "file_ids": ["file_000000009b60822fbf5a3299f1a290b4"],
                "sediment_ids": [],
                "text": "你更喜欢她的眼睛偏梦幻紫还是更明亮通透的紫？",
                "tool_invoked": True,
                "turn_use_case": "image gen",
            }

        with patch("services.protocol.conversation.conversation_events", fake_events):
            with self.assertRaises(RuntimeError) as ctx:
                list(stream_image_outputs(
                    Backend(),
                    ConversationRequest(model="gpt-image-2", prompt="cat"),
                ))
        self.assertIn("download url unresolved", str(ctx.exception))
        self.assertTrue(is_upstream_connection_error(str(ctx.exception)))

    def test_poll_timeout_switches_account_and_continues(self) -> None:
        """续传：轮询超时后排除超时号，下一棒换号继续并拿到结果。"""
        from services.openai_backend_api import ImagePollTimeoutError

        created: list[str] = []

        class FakeBackend:
            def __init__(self, access_token: str = "", *, force_proxy: str | None = None) -> None:
                self.access_token = access_token
                self.progress_callback = None
                created.append(access_token)

            def close(self) -> None:
                return None

        def fake_stream(backend, request, index, total):
            if backend.access_token == "token-a":
                yield ImageOutput(kind="progress", model=request.model, index=index, total=total, text="working")
                raise ImagePollTimeoutError("poll timed out")
            yield ImageOutput(
                kind="result",
                model=request.model,
                index=index,
                total=total,
                data=[{"url": "http://example.test/ok.png"}],
            )

        def get_token(**kwargs):
            excluded = kwargs.get("excluded_tokens") or set()
            if "token-a" in excluded:
                return "token-b"
            return "token-a"

        with (
            patch("services.protocol.conversation.account_service") as accounts,
            patch("services.protocol.conversation.OpenAIBackendAPI", FakeBackend),
            patch("services.protocol.conversation.stream_image_outputs", fake_stream),
            patch(
                "services.protocol.conversation.proxy_settings.list_egress_candidates",
                return_value=[("global", "socks5h://live.example:1080")],
            ),
        ):
            accounts.get_available_access_token.side_effect = get_token
            accounts.get_account.side_effect = lambda token: {"email": f"{token}@example.com"}
            outputs = _generate_single_image(
                ConversationRequest(model="gpt-image-2", prompt="cat"),
                1,
                1,
            )

        self.assertEqual(created, ["token-a", "token-b"])
        self.assertEqual(outputs[-1].kind, "result")
        self.assertEqual(outputs[-1].data[0]["url"], "http://example.test/ok.png")

    def test_trailing_stream_error_keeps_collected_result(self) -> None:
        """断流保结果：结果已到手后流报错，直接返回结果而不是对外报错。"""
        created: list[str] = []

        class FakeBackend:
            def __init__(self, access_token: str = "", *, force_proxy: str | None = None) -> None:
                self.access_token = access_token
                self.progress_callback = None
                created.append(access_token)

            def close(self) -> None:
                return None

        def fake_stream(backend, request, index, total):
            yield ImageOutput(
                kind="result",
                model=request.model,
                index=index,
                total=total,
                data=[{"url": "http://example.test/ok.png"}],
            )
            raise RuntimeError("curl: (56) Recv failure: Connection reset by peer")

        with (
            patch("services.protocol.conversation.account_service") as accounts,
            patch("services.protocol.conversation.OpenAIBackendAPI", FakeBackend),
            patch("services.protocol.conversation.stream_image_outputs", fake_stream),
            patch(
                "services.protocol.conversation.proxy_settings.list_egress_candidates",
                return_value=[("global", "socks5h://live.example:1080")],
            ),
        ):
            accounts.get_available_access_token.return_value = "token-a"
            accounts.get_account.return_value = {"email": "a@example.com"}
            outputs = _generate_single_image(
                ConversationRequest(model="gpt-image-2", prompt="cat"),
                1,
                1,
            )

        self.assertEqual(created, ["token-a"])
        self.assertEqual(outputs[-1].kind, "result")
        self.assertEqual(outputs[-1].data[0]["url"], "http://example.test/ok.png")