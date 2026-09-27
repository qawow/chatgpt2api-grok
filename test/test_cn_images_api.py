from __future__ import annotations

import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import cn_images
from services.captcha_solver import CaptchaError, obtain_captcha_token
from services.config import _normalize_doubao_settings, _normalize_zhitu360_settings
from services.doubao_backend import (
    DoubaoError,
    DoubaoImage,
    DoubaoResult,
    _message_payload,
    _parse_sse,
    SKILL_IMAGE_GENERATION,
)
from services.zhitu360_backend import (
    Zhitu360Error,
    ZhituImage,
    ZhituResult,
    parse_model,
    parse_ratio,
    size_to_ratio,
    is_zhitu_model,
)

AUTH_HEADERS = {"Authorization": "Bearer chatgpt2api"}


class ProtocolTests(unittest.TestCase):
    def test_doubao_payload_skill(self):
        body = _message_payload("一只猫")
        self.assertEqual(body["skill"]["skill_type"], SKILL_IMAGE_GENERATION)
        self.assertEqual(body["messages"][0]["content_type"], 2001)
        self.assertIn("一只猫", body["messages"][0]["content"])

    def test_parse_sse_urls(self):
        sse = (
            "data: {\"code\":0,\"message\":{\"content\":\"https://p3-imagex.byteimg.com/foo.png\"}}\n"
            "data: [DONE]\n"
        )
        result = _parse_sse(sse)
        self.assertTrue(any("byteimg.com" in img.url for img in result.images))

    def test_login_expired_raises_401(self):
        from services.doubao_backend import _raise_from_payload

        with self.assertRaises(DoubaoError) as ctx:
            _raise_from_payload({"code": 710012001, "msg": "登录已过期，请重新登录"})
        self.assertEqual(ctx.exception.status_code, 401)

    def test_zhitu_model_aliases(self):
        self.assertEqual(parse_model("即梦4.5"), "jimeng45")
        self.assertEqual(parse_model("hunyuan"), "hunyuan")
        self.assertTrue(is_zhitu_model("zhitu360"))
        self.assertFalse(is_zhitu_model("gpt-image-1"))
        self.assertFalse(is_zhitu_model(""))

    def test_zhitu_ratio_and_size(self):
        self.assertEqual(parse_ratio("1:1"), "1:1")
        self.assertEqual(size_to_ratio("1024x1024"), "1:1")
        self.assertEqual(size_to_ratio("1792x1024"), "16:9")
        with self.assertRaises(Zhitu360Error):
            parse_ratio("99:1")

    def test_zhitu_errno_mapping(self):
        from services.zhitu360_backend import _raise_errno

        with self.assertRaises(Zhitu360Error) as ctx:
            _raise_errno({"errno": 20601, "message": "用户未登录"})
        self.assertEqual(ctx.exception.status_code, 401)
        with self.assertRaises(Zhitu360Error) as ctx:
            _raise_errno({"errno": 20603, "message": "请购买会员"})
        self.assertEqual(ctx.exception.status_code, 402)

    def test_captcha_prefers_explicit(self):
        token = obtain_captcha_token(
            website_url="https://example.com",
            website_key="key",
            settings={},
            explicit="tok_abc",
        )
        self.assertEqual(token, "tok_abc")

    def test_captcha_missing_raises(self):
        with self.assertRaises(CaptchaError):
            obtain_captcha_token(website_url="https://example.com", website_key="", settings={})

    def test_settings_mask_ready(self):
        doubao = _normalize_doubao_settings({"cookies": "sessionid=x", "aid": "497858"})
        self.assertEqual(doubao["aid"], "497858")
        self.assertEqual(doubao["cookies"], "sessionid=x")
        zhitu = _normalize_zhitu360_settings({})
        self.assertTrue(zhitu["base_url"].endswith("image.360.com"))


class RouterTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(cn_images.create_router())
        self.client = TestClient(app)

    def test_requires_auth(self):
        self.assertEqual(self.client.get("/v1/doubao").status_code, 401)
        self.assertEqual(self.client.get("/v1/zhitu360").status_code, 401)

    def test_help(self):
        r = self.client.get("/v1/doubao", headers=AUTH_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertIn("/chat/completion", r.json()["upstream"])
        r = self.client.get("/v1/zhitu360", headers=AUTH_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertIn("jimeng", r.json()["models"])

    @mock.patch("api.cn_images.doubao_generate")
    def test_doubao_post(self, gen):
        gen.return_value = DoubaoResult(images=[DoubaoImage(url="https://p3-imagex.byteimg.com/a.png")])
        r = self.client.post(
            "/v1/doubao",
            headers=AUTH_HEADERS,
            json={"prompt": "猫", "cookies": "sessionid=1"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"][0]["url"], "https://p3-imagex.byteimg.com/a.png")
        gen.assert_called_once()

    @mock.patch("api.cn_images.zhitu_generate")
    def test_zhitu_post(self, gen):
        gen.return_value = ZhituResult(
            record_id="rid1",
            status=3,
            status_name="success",
            images=[ZhituImage(url="https://p0.ssl.qhimg.com/x.png")],
        )
        r = self.client.post(
            "/v1/zhitu360",
            headers=AUTH_HEADERS,
            json={"prompt": "猫", "model": "jimeng"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["record_id"], "rid1")
        self.assertEqual(r.json()["data"][0]["url"], "https://p0.ssl.qhimg.com/x.png")

    def test_doubao_blank_prompt(self):
        with mock.patch("api.cn_images.doubao_generate", side_effect=DoubaoError(status_code=400, detail={"error": "prompt is required"})):
            r = self.client.post("/v1/doubao", headers=AUTH_HEADERS, json={"prompt": ""})
            self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
