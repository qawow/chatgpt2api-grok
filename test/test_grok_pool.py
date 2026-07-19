"""Grok pool isolation + normalize + routing helpers."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.grok_account_service import GrokAccountService
from utils.grok_models import (
    is_grok_image_model,
    is_grok_text_model,
    resolve_grok_image_model,
)


class GrokModelHelpersTest(unittest.TestCase):
    def test_image_models(self):
        self.assertTrue(is_grok_image_model("grok-2-image"))
        self.assertTrue(is_grok_image_model("grok-imagine"))
        self.assertTrue(is_grok_image_model("GROK-2-Image-1212"))
        self.assertFalse(is_grok_image_model("gpt-image-2"))
        self.assertFalse(is_grok_image_model("codex-gpt-image-2"))
        self.assertFalse(is_grok_image_model("grok-4.5"))

    def test_text_models(self):
        self.assertTrue(is_grok_text_model("grok-4.5"))
        self.assertFalse(is_grok_text_model("grok-2-image"))
        self.assertFalse(is_grok_text_model("gpt-4o"))

    def test_resolve_default(self):
        self.assertEqual(resolve_grok_image_model(None), "grok-2-image")
        self.assertEqual(resolve_grok_image_model("grok-imagine"), "grok-imagine")


class GrokAccountServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "grok_accounts.json"
        self.svc = GrokAccountService(path=self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_normalize_cliproxy(self):
        item = {
            "type": "xai",
            "email": "u@example.com",
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "id_token": "id-1",
            "base_url": "https://api.x.ai/v1",  # must rewrite
            "headers": {"X-XAI-Token-Auth": "xai-grok-cli"},
        }
        normalized = self.svc.normalize_account(item)
        assert normalized is not None
        self.assertEqual(normalized["type"], "xai")
        self.assertEqual(normalized["provider"], "grok")
        self.assertIn("cli-chat-proxy.grok.com", normalized["base_url"])
        self.assertEqual(normalized["email"], "u@example.com")

    def test_reject_openai_type_without_grok_markers(self):
        item = {
            "type": "codex",
            "access_token": "openai-token",
            "base_url": "https://api.openai.com/v1",
        }
        self.assertIsNone(self.svc.normalize_account(item))

    def test_add_and_list_isolated_file(self):
        result = self.svc.add_account_items(
            [
                {
                    "type": "xai",
                    "access_token": "at-a",
                    "refresh_token": "rt",
                    "email": "a@x.ai",
                },
                {
                    "type": "xai",
                    "access_token": "at-b",
                    "email": "b@x.ai",
                },
            ]
        )
        self.assertEqual(result["added"], 2)
        self.assertEqual(self.svc.count(), 2)
        self.assertTrue(self.path.exists())
        # re-add same → skipped merge
        result2 = self.svc.add_account_items([{"access_token": "at-a", "type": "xai"}])
        self.assertEqual(result2["skipped"], 1)
        self.assertEqual(self.svc.count(), 2)

    def test_get_next_skips_disabled(self):
        self.svc.add_account_items(
            [
                {"access_token": "good", "type": "xai", "status": "正常"},
                {"access_token": "bad", "type": "xai", "disabled": True},
            ]
        )
        picked = {self.svc.get_next_account()["access_token"] for _ in range(4)}
        self.assertEqual(picked, {"good"})

    def test_delete(self):
        self.svc.add_account_items([{"access_token": "x", "type": "xai"}])
        out = self.svc.delete_accounts(["x"])
        self.assertEqual(out["removed"], 1)
        self.assertEqual(self.svc.count(), 0)


class GrokImageHandlerIsolationTest(unittest.TestCase):
    def test_handler_uses_only_grok_service(self):
        from services.protocol import grok_v1_image_generations

        fake_account = {
            "access_token": "grok-at",
            "base_url": "https://cli-chat-proxy.grok.com/v1",
            "headers": {},
        }
        with mock.patch(
            "services.protocol.grok_v1_image_generations.grok_account_service"
        ) as grok_svc, mock.patch(
            "services.protocol.grok_v1_image_generations.generate_image"
        ) as gen, mock.patch(
            "services.account_service.account_service.get_available_access_token"
        ) as chatgpt_pick, mock.patch(
            "services.image_storage_service.image_storage_service.save",
            side_effect=Exception("skip store"),
        ):
            grok_svc.get_next_account.return_value = fake_account
            gen.return_value = {
                "created": 1,
                "data": [{"b64_json": "QQ=="}],
                "_meta": {"upstream_path": "images/generations"},
            }
            result = grok_v1_image_generations.handle(
                {"prompt": "cube", "model": "grok-2-image", "n": 1}
            )
            self.assertEqual(result["data"][0]["b64_json"], "QQ==")
            chatgpt_pick.assert_not_called()
            grok_svc.get_next_account.assert_called()
            grok_svc.mark_result.assert_called()


if __name__ == "__main__":
    unittest.main()
