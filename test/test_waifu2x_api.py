from __future__ import annotations

import base64
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import waifu2x as waifu2x_module
from services.waifu2x_backend import (
    ConvertResult,
    Waifu2xError,
    Waifu2xOptions,
    extract_filename,
    parse_error_html,
    parse_format,
    parse_noise,
    parse_options,
    parse_scale,
    parse_style,
    upscale,
)
from services.waifu2x_turnstile import captcha_required, obtain_turnstile_token, TurnstileError
from services.config import ConfigStore, _normalize_waifu2x_settings

AUTH_HEADERS = {"Authorization": "Bearer chatgpt2api"}
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x00IEND\xaeB`\x82"
)
ERROR_HTML = """
    <!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">
    <html>
        <head>
            <title>Error: 401 Unauthorized</title>
        </head>
        <body>
            <h1>Error: 401 Unauthorized</h1>
            <pre>Turnstile Error</pre>
        </body>
    </html>
"""


class Waifu2xProtocolTests(unittest.TestCase):
    def test_parse_aliases(self):
        """测试网站表单取值与人类可读别名都能解析。"""
        self.assertEqual(parse_style("art_scan"), "art_scan")
        self.assertEqual(parse_style("manga"), "art_scan")
        self.assertEqual(parse_noise("highest"), 3)
        self.assertEqual(parse_noise("-1"), -1)
        self.assertEqual(parse_scale("1.6x"), 1)
        self.assertEqual(parse_scale("1x"), -1)
        self.assertEqual(parse_scale(2), 2)
        self.assertEqual(parse_format("webp"), 1)
        options = parse_options(style="art", noise="medium", scale="2x", image_format="png")
        self.assertEqual(options, Waifu2xOptions(style="art", noise=1, scale=2, format=0))

    def test_parse_rejects_noop(self):
        """测试 noise 与 scale 都关闭时拒绝请求。"""
        with self.assertRaises(Waifu2xError) as ctx:
            parse_options(noise="none", scale="none")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_parse_error_html_matches_frontend(self):
        """测试 bottle 错误页解析与 ui.js 一致。"""
        self.assertIn("Turnstile Error", parse_error_html(ERROR_HTML, 401))

    def test_extract_filename_star(self):
        """测试 Content-Disposition filename* 解析。"""
        name = extract_filename("inline; filename*=utf-8''cat_waifu2x_art_scale.png", "fallback.png")
        self.assertEqual(name, "cat_waifu2x_art_scale.png")

    def test_captcha_required_matches_ui(self):
        """测试 Patreon meter 大于 0 时跳过 Turnstile。"""
        self.assertTrue(captcha_required({"turnstile_enabled": True, "logged_in": False, "meter": 0}))
        self.assertFalse(captcha_required({"turnstile_enabled": True, "logged_in": True, "meter": 12}))
        self.assertFalse(captcha_required({"turnstile_enabled": False, "recaptcha_enabled": False}))

    def test_upscale_rejects_private_url(self):
        """测试 url 走 SSRF 守卫，不把内网地址转交给上游。"""
        with self.assertRaises(Waifu2xError) as ctx:
            upscale(url="http://127.0.0.1/secret.png")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_upscale_requires_source(self):
        """测试既没有文件也没有 url 时返回 400。"""
        with self.assertRaises(Waifu2xError) as ctx:
            upscale()
        self.assertEqual(ctx.exception.status_code, 400)

    def test_obtain_token_prefers_explicit(self):
        """测试请求自带 token 时不调用打码平台。"""
        token = obtain_turnstile_token(site_key="0x1", settings={}, explicit="  abc  ")
        self.assertEqual(token, "abc")

    def test_obtain_token_without_solver_explains(self):
        """测试未配置打码密钥时给出可操作的错误。"""
        with self.assertRaises(TurnstileError) as ctx:
            obtain_turnstile_token(site_key="0x1", settings={})
        self.assertIn("Turnstile", str(ctx.exception))

    def test_saas_solver_polls_token(self):
        """测试 Capsolver 风格 createTask / getTaskResult 轮询。"""
        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        posts = []

        class _Session:
            def post(self, url, json=None, timeout=None):
                posts.append((url, json))
                if "createTask" in url:
                    return _Resp({"errorId": 0, "taskId": "t1"})
                return _Resp({"errorId": 0, "status": "ready", "solution": {"token": "tok-1"}})

        token = obtain_turnstile_token(
            site_key="0x4AAAAAABqlY7DKXMzoS81U",
            settings={"capsolver_key": "CAI-test", "timeout_sec": 30},
            session_factory=lambda **kwargs: _Session(),
        )
        self.assertEqual(token, "tok-1")
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][1]["task"]["type"], "AntiTurnstileTaskProxyLess")

    def test_settings_redact_secrets(self):
        """测试设置接口不会回传打码密钥明文。"""
        store = ConfigStore.__new__(ConfigStore)
        store.data = {"waifu2x": {"capsolver_key": "CAI-secret", "ses_id": "cookie"}}
        sanitized = store._sanitize_waifu2x_settings(_normalize_waifu2x_settings(store.data["waifu2x"]))
        self.assertEqual(sanitized["capsolver_key"], "********")
        self.assertTrue(sanitized["has_capsolver_key"])
        self.assertEqual(sanitized["ses_id"], "********")


class Waifu2xApiTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_upscale(**kwargs):
            self.calls.append(kwargs)
            return ConvertResult(
                content=PNG_BYTES,
                content_type="image/png",
                filename="tiny_waifu2x_art_noise1_scale.png",
                options=Waifu2xOptions(style="art", noise=1, scale=2, format=0),
            )

        self.upscale_patcher = mock.patch.object(waifu2x_module, "upscale", fake_upscale)
        self.upscale_patcher.start()
        self.addCleanup(self.upscale_patcher.stop)
        app = FastAPI()
        app.include_router(waifu2x_module.create_router())
        self.client = TestClient(app)

    def test_requires_auth(self):
        """测试未带密钥时拒绝访问。"""
        response = self.client.get("/v1/waifu2x")
        self.assertEqual(response.status_code, 401)

    def test_json_b64_roundtrip(self):
        """测试 JSON + base64 输入返回 b64_json。"""
        response = self.client.post(
            "/v1/waifu2x",
            headers=AUTH_HEADERS,
            json={
                "image": base64.b64encode(PNG_BYTES).decode("ascii"),
                "style": "art",
                "noise": "medium",
                "scale": "2x",
                "response_format": "b64_json",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["model"], "waifu2x")
        self.assertEqual(body["data"][0]["b64_json"], base64.b64encode(PNG_BYTES).decode("ascii"))
        self.assertEqual(self.calls[0]["style"], "art")
        self.assertEqual(self.calls[0]["noise"], 1)
        self.assertEqual(self.calls[0]["scale"], 2)

    def test_multipart_binary(self):
        """测试 multipart 默认返回图片字节。"""
        response = self.client.post(
            "/v1/images/upscale",
            headers=AUTH_HEADERS,
            files={"file": ("tiny.png", PNG_BYTES, "image/png")},
            data={"style": "photo", "noise": "low", "scale": "1.6x"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, PNG_BYTES)
        self.assertIn("image/png", response.headers.get("content-type", ""))
        self.assertEqual(self.calls[0]["style"], "photo")
        self.assertEqual(self.calls[0]["scale"], 1)

    def test_status_endpoint(self):
        """测试 status 透出上游 Turnstile 状态。"""
        fake_state = {
            "turnstile_enabled": True,
            "turnstile_site_key": "0x4AAAAAABqlY7DKXMzoS81U",
            "logged_in": False,
            "meter": 0,
            "meter_max": 100,
        }
        with mock.patch.object(waifu2x_module, "fetch_captcha_state", return_value=fake_state):
            response = self.client.get("/v1/waifu2x/status", headers=AUTH_HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["turnstile_enabled"])
        self.assertEqual(body["turnstile_site_key"], "0x4AAAAAABqlY7DKXMzoS81U")
        self.assertEqual(body["upstream"], "https://www.waifu2x.net/api")

    def test_turnstile_error_is_not_auth_failure(self):
        """测试缺 Turnstile 返回 403，避免和密钥无效的 401 混淆。"""
        def boom(**kwargs):
            raise Waifu2xError(status_code=403, detail={"error": "Turnstile Error"})

        with mock.patch.object(waifu2x_module, "upscale", boom):
            response = self.client.post(
                "/v1/waifu2x",
                headers=AUTH_HEADERS,
                json={"url": "https://example.com/cat.png"},
            )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("Turnstile", response.text)


if __name__ == "__main__":
    unittest.main()
