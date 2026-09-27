"""「一个请求回传两张一样的图片」的回归测试。

已定位的四个来源：
1. 一个 ``sediment://file_00000000…`` 指针会同时进入 file_ids 与 sediment_ids，
   同一 id 走 file 与 attachment 两个下载接口，签出不同 URL → 同图下载两次；
2. 兜底：不同 id 指向同一资源时，按字节去重；
3. 丢失 conversation_id 后按 prompt 恢复会话，并发的两个生成会认领同一会话；
4. 豆包分流把每张图拆成 url 一条 + b64_json 一条。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from api.cn_images import openai_image_items
from services.openai_backend_api import OpenAIBackendAPI, _dedupe_image_bytes
from services.protocol import conversation
from services.protocol.conversation import extract_conversation_ids

IMAGE_ID = "file_00000000aaaabbbbccccddddeeeeffff"


def _backend() -> OpenAIBackendAPI:
    backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
    # CDN signs each endpoint's URL differently, which is why string dedupe failed.
    backend._get_file_download_url = lambda file_id: f"https://cdn/files/{file_id}?sig=file"
    backend._get_attachment_download_url = lambda cid, aid: f"https://cdn/attach/{aid}?sig=attach"
    return backend


class PointerDedupTests(unittest.TestCase):
    def test_single_sediment_pointer_populates_both_lists(self) -> None:
        """前提：这正是重复的起点。"""
        payload = '{"conversation_id":"c1","asset_pointer":"sediment://%s"}' % IMAGE_ID
        _, file_ids, sediment_ids = extract_conversation_ids(payload)
        self.assertEqual(file_ids, [IMAGE_ID])
        self.assertEqual(sediment_ids, [IMAGE_ID])

    def test_same_id_via_both_endpoints_resolves_once(self) -> None:
        urls = _backend()._resolve_image_urls("c1", [IMAGE_ID], [IMAGE_ID])
        self.assertEqual(len(urls), 1)

    def test_distinct_ids_both_resolve(self) -> None:
        urls = _backend()._resolve_image_urls("c1", ["file_a"], ["file_b"])
        self.assertEqual(len(urls), 2)

    def test_sediment_used_when_file_endpoint_fails(self) -> None:
        backend = _backend()

        def _fail(file_id):
            raise RuntimeError("404")

        backend._get_file_download_url = _fail
        urls = backend._resolve_image_urls("c1", [IMAGE_ID], [IMAGE_ID])
        self.assertEqual(urls, [f"https://cdn/attach/{IMAGE_ID}?sig=attach"])


class ByteDedupTests(unittest.TestCase):
    def test_identical_bytes_are_dropped_in_order(self) -> None:
        self.assertEqual(_dedupe_image_bytes([b"a", b"b", b"a", b"", b"c"]), [b"a", b"b", b"c"])


class ConversationClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        with conversation._claimed_conversations_lock:
            conversation._claimed_conversations.clear()

    def test_claim_is_exclusive(self) -> None:
        self.assertTrue(conversation.claim_conversation("conv-1"))
        self.assertFalse(conversation.claim_conversation("conv-1"))
        self.assertIn("conv-1", conversation.claimed_conversation_ids())

    def test_expired_claims_are_released(self) -> None:
        conversation.claim_conversation("conv-old")
        with conversation._claimed_conversations_lock:
            conversation._claimed_conversations["conv-old"] -= conversation._CLAIMED_CONVERSATION_TTL_SECS + 1
        self.assertTrue(conversation.claim_conversation("conv-old"))

    def test_recovery_skips_claimed_conversations(self) -> None:
        backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        backend._list_recent_conversations = lambda limit, timeout_secs: [
            {"id": "owned-by-other", "title": "Image of a cat", "update_time": 1000.0},
            {"id": "mine", "title": "Image of a cat", "update_time": 1000.0},
        ]
        found = backend.find_conversation_by_prompt("a cat", 1000.0, exclude_ids={"owned-by-other"})
        self.assertEqual(found, "mine")


class CnImageItemsTests(unittest.TestCase):
    def test_image_with_url_and_b64_yields_one_entry(self) -> None:
        image = SimpleNamespace(url="https://cdn/x.png", b64_json="aGk=")
        items = openai_image_items([image], "b64_json")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["b64_json"], "aGk=")

    def test_url_format_omits_base64(self) -> None:
        image = SimpleNamespace(url="https://cdn/x.png", b64_json="")
        self.assertEqual(openai_image_items([image], "url"), [{"url": "https://cdn/x.png"}])

    def test_empty_images_are_skipped(self) -> None:
        self.assertEqual(openai_image_items([SimpleNamespace(url="", b64_json="")]), [])


if __name__ == "__main__":
    unittest.main()
