"""模型 id 单一来源（utils/image_models.py）的一致性测试。"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest import mock

from services.protocol import openai_v1_models
from utils import image_models as registry

ROOT = Path(__file__).resolve().parents[1]


def _list(accounts=(), grok=0, doubao_cookies="", zhitu_cookies=""):
    with mock.patch.object(openai_v1_models.account_service, "list_accounts", return_value=list(accounts)), \
            mock.patch.object(openai_v1_models.grok_account_service, "count", return_value=grok), \
            mock.patch.object(openai_v1_models.config, "get_doubao_settings", return_value={"cookies": doubao_cookies}), \
            mock.patch.object(openai_v1_models.config, "get_zhitu360_settings", return_value={"cookies": zhitu_cookies}):
        return [item["id"] for item in openai_v1_models.list_models()["data"]]


class CatalogTests(unittest.TestCase):
    def test_everything_listed_is_a_routable_image_model(self) -> None:
        ids = _list(accounts=[{"access_token": "t", "type": "Plus", "source_type": "codex"}],
                    grok=1, doubao_cookies="c", zhitu_cookies="c")
        self.assertTrue(ids)
        for model in ids:
            self.assertIsNotNone(registry.image_model_provider(model), model)

    def test_aliases_and_legacy_names_are_never_listed(self) -> None:
        ids = set(_list(accounts=[{"access_token": "t"}], grok=1, doubao_cookies="c", zhitu_cookies="c"))
        hidden = set(registry.LEGACY_WEB_IMAGE_MODELS) | set(registry.GROK_IMAGE_ALIASES) \
            | set(registry.ZHITU360_MODEL_ALIASES)
        self.assertEqual(ids & hidden, set())

    def test_cookie_backends_listed_only_when_configured(self) -> None:
        ids = _list(accounts=[{"access_token": "t"}])
        self.assertNotIn(registry.DOUBAO_IMAGE_MODEL, ids)
        self.assertNotIn(registry.DEFAULT_ZHITU360_MODEL, ids)
        ids = _list(accounts=[{"access_token": "t"}], doubao_cookies="c", zhitu_cookies="c")
        self.assertIn(registry.DOUBAO_IMAGE_MODEL, ids)
        self.assertTrue(set(registry.ZHITU360_IMAGE_MODELS) <= set(ids))

    def test_empty_everything_lists_nothing(self) -> None:
        self.assertEqual(_list(), [])


class RoutingTests(unittest.TestCase):
    def test_text_models_are_not_image_models(self) -> None:
        for model in ("gpt-5", "gpt-5-3", "auto", "grok-4.5", "", None):
            self.assertIsNone(registry.image_model_provider(model), model)

    def test_aliases_route_to_their_provider(self) -> None:
        self.assertEqual(registry.image_model_provider("gpt-image-2"), registry.PROVIDER_CHATGPT)
        self.assertEqual(registry.image_model_provider("grok-imagine"), registry.PROVIDER_GROK)
        self.assertEqual(registry.image_model_provider("即梦4"), registry.PROVIDER_ZHITU360)
        self.assertEqual(registry.image_model_provider("doubao-seedream"), registry.PROVIDER_DOUBAO)

    def test_error_text_names_current_models(self) -> None:
        self.assertIn(registry.WEB_IMAGE_MODEL, registry.TEXT_MODELS_DISABLED)
        # The legacy id as a standalone token (codex-gpt-image-2 is a current id).
        self.assertIsNone(re.search(r"(?<![\w-])gpt-image-2(?![\w.])", registry.TEXT_MODELS_DISABLED))


class FrontendMirrorTests(unittest.TestCase):
    """web/src/lib/models.ts 必须与后端注册表保持一致。"""

    def setUp(self) -> None:
        self.source = (ROOT / "web" / "src" / "lib" / "models.ts").read_text(encoding="utf-8")

    def _const(self, name: str) -> str:
        match = re.search(rf'export const {name} = "([^"]+)";', self.source)
        self.assertIsNotNone(match, name)
        return match.group(1)

    def test_scalar_ids_match(self) -> None:
        for name in ("WEB_IMAGE_MODEL", "CODEX_IMAGE_MODEL", "GROK_IMAGINE_IMAGE_MODEL",
                     "GROK_2_IMAGE_MODEL", "DOUBAO_IMAGE_MODEL"):
            self.assertEqual(self._const(name), getattr(registry, name), name)

    def test_zhitu_ids_match(self) -> None:
        match = re.search(r"export const ZHITU360_IMAGE_MODELS = \[([^\]]+)\]", self.source)
        self.assertIsNotNone(match)
        ids = tuple(re.findall(r'"([^"]+)"', match.group(1)))
        self.assertEqual(ids, registry.ZHITU360_IMAGE_MODELS)


if __name__ == "__main__":
    unittest.main()
