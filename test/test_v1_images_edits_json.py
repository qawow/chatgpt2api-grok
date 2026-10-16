from __future__ import annotations

import base64
import os
import unittest
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "chatgpt2api")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.ai as ai_module
import api.image_inputs as image_inputs

AUTH_HEADERS = {"Authorization": "Bearer chatgpt2api"}
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(b"fake-png").decode("ascii")
JPEG_DATA_URL = "data:image/jpeg;base64," + base64.b64encode(b"fake-jpeg").decode("ascii")


class ImageEditsJsonApiTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_handle(payload):
            self.calls.append(payload)
            return {"created": 1, "data": [{"b64_json": "ZmFrZQ=="}]}

        self.handle_patcher = mock.patch.object(ai_module.openai_v1_image_edit, "handle", fake_handle)
        self.filter_patcher = mock.patch.object(ai_module, "filter_or_log", mock.AsyncMock())
        self.handle_patcher.start()
        self.filter_patcher.start()
        self.addCleanup(self.handle_patcher.stop)
        self.addCleanup(self.filter_patcher.stop)

        app = FastAPI()
        app.include_router(ai_module.create_router())
        self.client = TestClient(app)

    def test_json_model_omitted_uses_existing_default_logic(self):
        response = self.client.post("/v1/images/edits", headers=AUTH_HEADERS, json={"prompt": "未传 model", "image": PNG_DATA_URL})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.calls[0]["model"], "gpt-image-2.5")

    def test_json_model_is_not_overwritten_when_provided(self):
        response = self.client.post(
            "/v1/images/edits",
            headers=AUTH_HEADERS,
            json={"model": "codex-gpt-image-2", "prompt": "保留 model", "image": PNG_DATA_URL},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.calls[0]["model"], "codex-gpt-image-2")

    def test_image_edit_accepts_json_image_url(self):
        response = self.client.post(
            "/v1/images/edits",
            headers=AUTH_HEADERS,
            json={
                "model": "gpt-image-2",
                "prompt": "把图片改成夜景风格",
                "n": 1,
                "size": "1024x1536",
                "response_format": "b64_json",
                "images": [{"image_url": PNG_DATA_URL}],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = self.calls[0]
        self.assertEqual(payload["images"], [(b"fake-png", "image_url.png", "image/png")])
        self.assertEqual(payload["size"], "1024x1536")

    def test_image_edit_accepts_json_multiple_images_and_b64_json(self):
        response = self.client.post(
            "/v1/images/edits",
            headers=AUTH_HEADERS,
            json={
                "prompt": "把两张图合成海报",
                "images": [
                    PNG_DATA_URL,
                    {"b64_json": base64.b64encode(b"raw-jpeg").decode("ascii"), "mime_type": "image/jpeg", "filename": "two.jpg"},
                    {"image_url": {"url": JPEG_DATA_URL}},
                ],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.calls[0]["images"], [
            (b"fake-png", "image_url.png", "image/png"),
            (b"raw-jpeg", "two.jpg", "image/jpeg"),
            (b"fake-jpeg", "image_url.jpg", "image/jpeg"),
        ])

    def test_image_edit_keeps_original_multipart_multiple_image_logic(self):
        response = self.client.post(
            "/v1/images/edits",
            headers=AUTH_HEADERS,
            data={"prompt": "multipart 多图仍然可用", "model": "gpt-image-2", "n": "1"},
            files=[
                ("image", ("one.png", b"one", "image/png")),
                ("image", ("two.jpg", b"two", "image/jpeg")),
                ("image[]", ("three.webp", b"three", "image/webp")),
            ],
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.calls[0]["images"], [
            (b"one", "one.png", "image/png"),
            (b"two", "two.jpg", "image/jpeg"),
            (b"three", "three.webp", "image/webp"),
        ])

    def test_image_edit_rejects_json_without_image(self):
        response = self.client.post("/v1/images/edits", headers=AUTH_HEADERS, json={"prompt": "缺少图片"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("image file or image_url is required", response.text)

    def test_image_edit_rejects_private_network_json_url(self):
        """远程图片已支持，但 SSRF 防护仍须拦住内网 / 回环地址。"""
        for url in ("http://127.0.0.1/a.png", "http://192.168.1.5/a.png", "http://[::1]/a.png"):
            with self.subTest(url=url):
                self.calls.clear()
                response = self.client.post(
                    "/v1/images/edits",
                    headers=AUTH_HEADERS,
                    json={"prompt": "不允许拉内网图", "images": [{"image_url": url}]},
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("private or local network address", response.text)
                self.assertFalse(self.calls)

    def test_image_edit_rejects_too_many_image_references(self):
        response = self.client.post(
            "/v1/images/edits",
            headers=AUTH_HEADERS,
            json={"prompt": "引用过多", "images": [PNG_DATA_URL] * 17},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("too many image references", response.text)
        self.assertFalse(self.calls)

    def test_image_edit_rejects_single_image_exceeds_50mb(self):
        """Scale the byte limit down to exercise the real decode path cheaply."""
        huge_data_url = "data:image/png;base64," + base64.b64encode(b"x" * 51).decode("ascii")
        with mock.patch.object(image_inputs, "MAX_IMAGE_REFERENCE_BYTES", 50):
            response = self.client.post(
                "/v1/images/edits", headers=AUTH_HEADERS,
                json={"prompt": "单图过大", "images": [huge_data_url]},
            )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("image input exceeds 50MB limit", response.text)
        self.assertFalse(self.calls)

    def test_image_edit_rejects_combined_images_exceed_200mb(self):
        """多张图片累计超过 200MB 拒绝。"""
        chunk_url = "data:image/png;base64," + base64.b64encode(b"x" * 49).decode("ascii")
        with (
            mock.patch.object(image_inputs, "MAX_IMAGE_REFERENCE_BYTES", 50),
            mock.patch.object(image_inputs, "MAX_IMAGE_REFERENCE_TOTAL_BYTES", 200),
        ):
            response = self.client.post(
                "/v1/images/edits", headers=AUTH_HEADERS,
                json={"prompt": "累计过大", "images": [chunk_url] * 5},
            )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("combined image inputs exceed 200MB limit", response.text)
        self.assertFalse(self.calls)

    def test_image_edit_rejects_image_plus_mask_exceed_200mb(self):
        """image + mask 组合超过 200MB 总限制。"""
        chunk_url = "data:image/png;base64," + base64.b64encode(b"x" * 41).decode("ascii")
        with (
            mock.patch.object(image_inputs, "MAX_IMAGE_REFERENCE_BYTES", 50),
            mock.patch.object(image_inputs, "MAX_IMAGE_REFERENCE_TOTAL_BYTES", 200),
        ):
            response = self.client.post(
                "/v1/images/edits", headers=AUTH_HEADERS,
                json={"prompt": "image + mask 累计过大", "images": [chunk_url] * 4, "mask": chunk_url},
            )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("combined image inputs exceed 200MB limit", response.text)
        self.assertFalse(self.calls)

    def test_image_edit_accepts_exact_byte_boundaries(self):
        chunk_url = "data:image/png;base64," + base64.b64encode(b"x" * 50).decode("ascii")
        with (
            mock.patch.object(image_inputs, "MAX_IMAGE_REFERENCE_BYTES", 50),
            mock.patch.object(image_inputs, "MAX_IMAGE_REFERENCE_TOTAL_BYTES", 200),
        ):
            response = self.client.post(
                "/v1/images/edits", headers=AUTH_HEADERS,
                json={"prompt": "恰好达到大小限制", "images": [chunk_url] * 3, "mask": chunk_url},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(sum(len(i[0]) for i in self.calls[0]["images"] + self.calls[0]["mask"]), 200)

    def test_image_edit_rejects_oversized_mask(self):
        mask_url = "data:image/png;base64," + base64.b64encode(b"x" * 51).decode("ascii")
        with mock.patch.object(image_inputs, "MAX_IMAGE_REFERENCE_BYTES", 50):
            response = self.client.post(
                "/v1/images/edits", headers=AUTH_HEADERS,
                json={"prompt": "mask 过大", "image": PNG_DATA_URL, "mask": mask_url},
            )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse(self.calls)

    def test_empty_preferred_field_falls_back_to_image(self):
        for empty in (None, [], ""):
            with self.subTest(empty=empty):
                self.calls.clear()
                response = self.client.post(
                    "/v1/images/edits", headers=AUTH_HEADERS,
                    json={"prompt": "兼容空字段", "images": empty, "image": PNG_DATA_URL},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.calls[0]["images"][0][0], b"fake-png")

    def test_image_edit_accepts_exactly_16_images(self):
        """边界测试：恰好 16 张图片应该被接受。"""
        response = self.client.post(
            "/v1/images/edits",
            headers=AUTH_HEADERS,
            json={"prompt": "恰好 16 张", "images": [PNG_DATA_URL] * 16},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.calls[0]["images"]), 16)

    def test_image_edit_rejects_json_n_out_of_range(self):
        response = self.client.post("/v1/images/edits", headers=AUTH_HEADERS, json={"prompt": "n 越界", "n": 5, "image": PNG_DATA_URL})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse(self.calls)

    def test_image_edit_respects_field_priority_when_multiple_keys_present(self):
        """测试网关场景：当 JSON body 同时包含 images、image、image_url 字段时，按优先级只取第一个。

        New API / one-api 等网关在转发时可能同时传递多个字段，确保不会重复处理或混淆。
        优先级：images > image > image_url
        """
        # 场景 1：同时存在 images 和 image，应该只取 images
        response = self.client.post(
            "/v1/images/edits",
            headers=AUTH_HEADERS,
            json={
                "prompt": "测试优先级",
                "images": [PNG_DATA_URL, JPEG_DATA_URL],  # 2 张图
                "image": "data:image/png;base64,c2hvdWxkLWlnbm9yZQ==",  # 应被忽略
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        # 应该只有 images 数组的 2 张图，image 字段被忽略
        self.assertEqual(len(self.calls[0]["images"]), 2)
        self.assertEqual(self.calls[0]["images"][0][0], b"fake-png")
        self.assertEqual(self.calls[0]["images"][1][0], b"fake-jpeg")

        # 场景 2：同时存在 image 和 image_url，应该只取 image
        self.calls.clear()
        response = self.client.post(
            "/v1/images/edits",
            headers=AUTH_HEADERS,
            json={
                "prompt": "测试优先级",
                "image": PNG_DATA_URL,  # 应该被使用
                "image_url": JPEG_DATA_URL,  # 应被忽略
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        # 应该只有 image 字段的 1 张图
        self.assertEqual(len(self.calls[0]["images"]), 1)
        self.assertEqual(self.calls[0]["images"][0][0], b"fake-png")

        # 场景 3：只有 image_url（最低优先级）
        self.calls.clear()
        response = self.client.post(
            "/v1/images/edits",
            headers=AUTH_HEADERS,
            json={
                "prompt": "测试优先级",
                "image_url": JPEG_DATA_URL,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.calls[0]["images"]), 1)
        self.assertEqual(self.calls[0]["images"][0][0], b"fake-jpeg")


if __name__ == "__main__":
    unittest.main()
