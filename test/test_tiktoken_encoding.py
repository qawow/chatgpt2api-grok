from __future__ import annotations

import unittest
from unittest import mock


class TiktokenEncodingTests(unittest.TestCase):
    def setUp(self) -> None:
        import utils.tiktoken_encoding as module

        self.module = module
        with module._LOCK:
            module._ENCODINGS.clear()
            module._FALLBACK = None

    def test_fallback_encode_is_stable(self) -> None:
        encoding = self.module.FallbackEncoding()
        self.assertEqual(len(encoding.encode("")), 0)
        self.assertGreaterEqual(len(encoding.encode("abcd")), 1)
        self.assertEqual(len(encoding.encode("abcd")), len(encoding.encode("abcd")))

    def test_count_survives_proxy_error(self) -> None:
        import requests

        fake = mock.MagicMock()
        fake.encoding_for_model.side_effect = requests.exceptions.ProxyError("socks refused")
        fake.get_encoding.side_effect = requests.exceptions.ProxyError("socks refused")
        with mock.patch.object(self.module, "_install_direct_download"):
            with mock.patch.dict("sys.modules", {"tiktoken": fake}):
                encoding = self.module.encoding_for_model("gpt-image-2")
        self.assertTrue(hasattr(encoding, "encode"))
        self.assertIsInstance(encoding.encode("hello image"), list)
        self.assertEqual(encoding.name, "fallback_utf8_approx")

    def test_conversation_wrapper_returns_encoder(self) -> None:
        from services.protocol.conversation import count_text_tokens, encoding_for_model

        encoding = encoding_for_model("gpt-image-2")
        self.assertTrue(hasattr(encoding, "encode"))
        n = count_text_tokens("a red cube", "gpt-image-2")
        self.assertIsInstance(n, int)
        self.assertGreaterEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
