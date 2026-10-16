from __future__ import annotations

import base64
import io
import unittest
from unittest import mock

from fastapi import HTTPException
from starlette.datastructures import UploadFile

from api import image_inputs
from services.protocol import cn_image_generations, grok_v1_image_generations, openai_v1_image_edit, openai_v1_image_generations
from utils.helper import parse_image_count


class ImageInputLimitTests(unittest.IsolatedAsyncioTestCase):
    def test_production_defaults(self):
        self.assertEqual(image_inputs.MAX_IMAGE_REFERENCE_COUNT, 16)
        self.assertEqual(image_inputs.MAX_IMAGE_REFERENCE_BYTES, 50 * 1024 * 1024)
        self.assertEqual(image_inputs.MAX_IMAGE_REFERENCE_TOTAL_BYTES, 200 * 1024 * 1024)

    async def test_rejects_count_before_decoding_inline_images(self):
        inline = {"b64_json": base64.b64encode(b"fixture").decode()}
        with mock.patch.object(image_inputs.base64, "b64decode") as decode:
            with self.assertRaises(HTTPException) as error:
                image_inputs._json_image_sources({"images": [inline] * 17})
        self.assertEqual(error.exception.status_code, 400)
        decode.assert_not_called()

    async def test_rejects_encoded_oversize_before_allocating_decoded_bytes(self):
        sources = image_inputs._json_image_sources({"image": {"b64_json": "A" * 100}})
        with (
            mock.patch.object(image_inputs, "MAX_IMAGE_REFERENCE_BYTES", 10),
            mock.patch.object(image_inputs.base64, "b64decode") as decode,
        ):
            with self.assertRaises(HTTPException):
                await image_inputs.read_image_sources(sources)
        decode.assert_not_called()

    async def test_upload_reads_at_most_limit_plus_one_and_closes_unread_files(self):
        uploads = [UploadFile(io.BytesIO(b"x" * 100), filename="a.png"), UploadFile(io.BytesIO(b"next"), filename="b.png")]
        uploads[0].read = mock.AsyncMock(wraps=uploads[0].read)
        uploads[1].read = mock.AsyncMock(wraps=uploads[1].read)
        with mock.patch.object(image_inputs, "MAX_IMAGE_REFERENCE_BYTES", 50):
            with self.assertRaises(HTTPException) as error:
                await image_inputs.read_image_sources(uploads)
        self.assertIn("50MB", str(error.exception.detail))
        uploads[0].read.assert_awaited_once_with(51)
        uploads[1].read.assert_not_awaited()
        self.assertTrue(all(upload.file.closed for upload in uploads))

    async def test_rejected_upload_count_closes_all_files_without_reading(self):
        uploads = [UploadFile(io.BytesIO(b"x"), filename="a.png") for _ in range(17)]
        with self.assertRaises(HTTPException):
            await image_inputs.read_image_sources(uploads)
        self.assertTrue(all(upload.file.closed for upload in uploads))

    async def test_upload_respects_bytes_already_consumed_by_images(self):
        upload = UploadFile(io.BytesIO(b"x" * 10), filename="mask.png")
        upload.read = mock.AsyncMock(wraps=upload.read)
        with mock.patch.object(image_inputs, "MAX_IMAGE_REFERENCE_TOTAL_BYTES", 20):
            with self.assertRaises(HTTPException) as error:
                await image_inputs.read_image_sources([upload], initial_total_bytes=15)
        self.assertIn("combined", str(error.exception.detail))
        upload.read.assert_awaited_once_with(6)


class ImageCountValidationTests(unittest.TestCase):
    def test_singleton_counts_normalize_but_ambiguous_counts_are_rejected(self):
        self.assertEqual(parse_image_count(["2"]), 2)
        self.assertEqual(parse_image_count(None), 1)
        for count in ([1, 2], [[1]], [], {"n": 1}, 1.5, float("inf"), "bad", 5):
            with self.subTest(count=count):
                with self.assertRaises(HTTPException) as error:
                    parse_image_count(count)
                self.assertEqual(error.exception.status_code, 400)

    def test_protocols_reject_invalid_counts_before_external_work(self):
        for handler in (
            openai_v1_image_generations.handle,
            openai_v1_image_edit.handle,
            grok_v1_image_generations._handle_via_local_pool,
            cn_image_generations.handle_zhitu,
        ):
            with self.subTest(handler=handler.__module__):
                with self.assertRaises(HTTPException) as error:
                    handler({"prompt": "local validation", "n": [1, 2]})
                self.assertEqual(error.exception.status_code, 400)
