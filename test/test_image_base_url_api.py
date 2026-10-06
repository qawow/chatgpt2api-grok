import unittest
from types import SimpleNamespace
from unittest import mock

import api.support as api_support


class ImageBaseUrlApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake_config = SimpleNamespace(base_url="https://public.example.com")
        patcher = mock.patch.object(api_support, "config", self.fake_config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_prefers_configured_base_url(self) -> None:
        request = SimpleNamespace(
            url=SimpleNamespace(scheme="http", netloc="127.0.0.1:8000"),
            headers={"host": "127.0.0.1:8000"},
        )

        self.assertEqual(api_support.resolve_image_base_url(request), "https://public.example.com")

    def test_falls_back_to_request_host(self) -> None:
        self.fake_config.base_url = ""
        request = SimpleNamespace(
            url=SimpleNamespace(scheme="http", netloc="127.0.0.1:8000"),
            headers={"host": "internal.example:9000"},
        )

        self.assertEqual(api_support.resolve_image_base_url(request), "http://internal.example:9000")

    def test_falls_back_to_request_netloc_when_host_missing(self) -> None:
        self.fake_config.base_url = ""
        request = SimpleNamespace(
            url=SimpleNamespace(scheme="https", netloc="public.example.com"),
            headers={},
        )

        self.assertEqual(api_support.resolve_image_base_url(request), "https://public.example.com")

    def test_forwarded_host_beats_request_host_when_unset(self) -> None:
        self.fake_config.base_url = ""
        request = SimpleNamespace(
            url=SimpleNamespace(scheme="http", netloc="10.0.0.8:8000"),
            headers={
                "host": "10.0.0.8:8000",
                "x-forwarded-host": "ai.example.com",
                "x-forwarded-proto": "https",
            },
        )

        self.assertEqual(api_support.resolve_image_base_url(request), "https://ai.example.com")

    def test_forwarded_chain_uses_first_hop(self) -> None:
        self.fake_config.base_url = ""
        request = SimpleNamespace(
            url=SimpleNamespace(scheme="http", netloc="10.0.0.8:8000"),
            headers={
                "host": "10.0.0.8:8000",
                "x-forwarded-host": "edge.example.com, 10.0.0.8:8000",
                "x-forwarded-proto": "https,http",
            },
        )

        self.assertEqual(api_support.resolve_image_base_url(request), "https://edge.example.com")

    def test_configured_base_url_still_wins_over_forwarded(self) -> None:
        request = SimpleNamespace(
            url=SimpleNamespace(scheme="http", netloc="10.0.0.8:8000"),
            headers={
                "host": "10.0.0.8:8000",
                "x-forwarded-host": "proxy.example.com",
            },
        )

        self.assertEqual(api_support.resolve_image_base_url(request), "https://public.example.com")

    def test_forwarded_host_without_proto_uses_request_scheme(self) -> None:
        self.fake_config.base_url = ""
        request = SimpleNamespace(
            url=SimpleNamespace(scheme="https", netloc="10.0.0.8:8000"),
            headers={
                "host": "10.0.0.8:8000",
                "x-forwarded-host": "ai.example.com",
            },
        )

        self.assertEqual(api_support.resolve_image_base_url(request), "https://ai.example.com")


if __name__ == "__main__":
    unittest.main()
