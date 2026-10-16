from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from services import content_filter, proxy_service


def proxy_store(legacy="", runtime=None):
    return proxy_service.ProxySettingsStore(SimpleNamespace(
        get_proxy_settings=lambda: legacy,
        get_proxy_runtime_settings=lambda: runtime or {},
    ))


class BoundedImageFetchTests(unittest.TestCase):
    def fetch(self, response):
        session = mock.Mock()
        session.get.return_value = response
        store = proxy_store()
        with mock.patch.object(proxy_service, "create_cffi_session", return_value=session):
            try:
                return store.get_with_egress_fallback("https://fixture.invalid/image.png", max_response_bytes=8)
            finally:
                self.assertTrue(session.get.call_args.kwargs["stream"])
                session.close.assert_called_once()
                response.close.assert_called_once()

    def test_accepts_exact_limit_with_chunked_transfer(self):
        response = mock.Mock(status_code=200, headers={})
        response.iter_content.return_value = iter([b"1234", b"5678"])
        self.assertEqual(self.fetch(response).content, b"12345678")

    def test_stops_chunked_transfer_on_oversize(self):
        response = mock.Mock(status_code=200, headers={})

        def chunks():
            yield b"12345678"
            yield b"9"
            self.fail("oversize response must stop reading further chunks")

        response.iter_content.return_value = chunks()
        with self.assertRaisesRegex(ValueError, "byte limit"):
            self.fetch(response)

    def test_rejects_content_length_without_reading_body(self):
        response = mock.Mock(status_code=200, headers={"content-length": "9"})
        with self.assertRaisesRegex(ValueError, "byte limit"):
            self.fetch(response)
        response.iter_content.assert_not_called()


class ContentReviewProxyTests(unittest.TestCase):
    def test_review_uses_runtime_or_legacy_proxy(self):
        cases = (
            ("http://legacy.invalid:8080", {}, "http://legacy.invalid:8080"),
            ("", {"enabled": True, "egress_mode": "single_proxy", "proxy_url": "socks5://runtime.invalid:1080"}, "socks5h://runtime.invalid:1080"),
        )
        review_config = SimpleNamespace(sensitive_words=[], ai_review={
            "enabled": True, "base_url": "https://review.invalid", "api_key": "fixture-key", "model": "fixture-model",
        })
        for legacy, runtime, expected in cases:
            with self.subTest(expected=expected):
                response = mock.Mock(status_code=200)
                response.json.return_value = {"choices": [{"message": {"content": "ALLOW"}}]}
                session = mock.Mock()
                session.post.return_value = response
                with (
                    mock.patch.object(content_filter, "config", review_config),
                    mock.patch.object(content_filter, "proxy_settings", proxy_store(legacy, runtime)),
                    mock.patch.object(content_filter, "create_cffi_session", return_value=session) as factory,
                ):
                    content_filter.check_request("local proxy regression")
                self.assertEqual(factory.call_args.kwargs["proxy"], expected)
                session.close.assert_called_once()
