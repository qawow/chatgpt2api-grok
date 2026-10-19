"""grok.com 网页路径客户端 —— 解析层与请求构造回归。

协议依据：`docs/grok-web-protocol.md`（静态逆向 1367 个前端 chunk）。

覆盖范围说明：本文件只测**纯函数**（NDJSON 解析、信封拆解、字段提取、
请求体构造）。网络与认证部分需要真实的 grok.com 会话 cookie，不在此覆盖。
"""
from __future__ import annotations

import json
import unittest

from services import grok_web_api as g


class NdjsonParserTest(unittest.TestCase):
    """流是 NDJSON，不是 SSE —— 这是实现时最容易踩的坑。"""

    def test_each_line_is_a_json_object(self):
        lines = [
            b'{"result":{"response":{"message":"hello"}}}',
            b'{"result":{"response":{"message":" world"}}}',
        ]
        chunks = list(g.iter_ndjson(lines))
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["result"]["response"]["message"], "hello")

    def test_no_data_prefix_is_stripped(self):
        """SSE 的 `data: ` 前缀在这里不存在；带前缀的行应当解析失败被跳过。"""
        lines = [b'data: {"result":{}}', b'{"result":{"response":{"message":"ok"}}}']
        chunks = list(g.iter_ndjson(lines))
        self.assertEqual(len(chunks), 1, "带 data: 前缀的行应被跳过而非剥离后解析")
        self.assertEqual(chunks[0]["result"]["response"]["message"], "ok")

    def test_malformed_lines_are_skipped_not_fatal(self):
        """与前端一致：解析失败只上报，不中断流。"""
        lines = [b'{"a":1}', b'not json at all', b'', b'   ', b'{"b":2}']
        chunks = list(g.iter_ndjson(lines))
        self.assertEqual([list(c)[0] for c in chunks], ["a", "b"])

    def test_non_object_json_is_skipped(self):
        lines = [b'[1,2,3]', b'"a string"', b'42', b'{"ok":true}']
        chunks = list(g.iter_ndjson(lines))
        self.assertEqual(len(chunks), 1)

    def test_str_lines_are_accepted(self):
        chunks = list(g.iter_ndjson(['{"x":1}', '{"y":2}']))
        self.assertEqual(len(chunks), 2)

    def test_invalid_utf8_is_skipped(self):
        chunks = list(g.iter_ndjson([b'\xff\xfe invalid', b'{"ok":1}']))
        self.assertEqual(len(chunks), 1)


class UnwrapTest(unittest.TestCase):
    """两个端点的信封不同，unwrap 都要处理。"""

    def test_new_conversation_envelope(self):
        chunk = {"result": {"response": {"message": "hi", "responseId": "r1"},
                            "conversation": {"conversationId": "c1"},
                            "title": {"newTitle": "t"}}}
        response, error = g.unwrap(chunk)
        self.assertEqual(response["message"], "hi")
        self.assertIsNone(error)

    def test_add_response_envelope_puts_model_response_at_result(self):
        """`/conversations/{id}/responses` 的 result 直接就是 ModelResponse。"""
        chunk = {"result": {"message": "hi", "responseId": "r1"}}
        response, error = g.unwrap(chunk)
        self.assertEqual(response["message"], "hi")
        self.assertIsNone(error)

    def test_error_is_surfaced(self):
        chunk = {"result": {}, "error": {"code": "x", "message": "boom", "details": []}}
        response, error = g.unwrap(chunk)
        self.assertEqual(response, {})
        self.assertEqual(error["message"], "boom")

    def test_missing_result_is_empty(self):
        self.assertEqual(g.unwrap({}), ({}, None))
        self.assertEqual(g.unwrap({"result": None}), ({}, None))


class CitationExtractionTest(unittest.TestCase):
    def test_dict_wrapped_results(self):
        response = {"citedWebSearchResults": {"results": [
            {"url": "https://a.example", "title": "A", "preview": "..."},
        ]}}
        out = g.extract_citations(response)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "https://a.example")

    def test_bare_list_is_also_accepted(self):
        """同一类型在 bundle 里有两层形状（{results:[...]} 与裸数组）。"""
        response = {"webSearchResults": [{"url": "https://b.example"}]}
        self.assertEqual(g.extract_citations(response)[0]["url"], "https://b.example")

    def test_cited_and_plain_are_both_collected(self):
        response = {
            "citedWebSearchResults": {"results": [{"url": "u1"}]},
            "webSearchResults": {"results": [{"url": "u2"}]},
        }
        self.assertEqual([c["url"] for c in g.extract_citations(response)], ["u1", "u2"])

    def test_missing_field_is_empty(self):
        self.assertEqual(g.extract_citations({}), [])


class SearchPageExtractionTest(unittest.TestCase):
    """富字段版在 outputChunks[].toolResult.webSearch.webpages —— 不在顶层。"""

    def _response(self, pages):
        return {"outputChunks": [{"toolResult": {"webSearch": {"webpages": pages}}}]}

    def test_pages_are_read_from_the_nested_path(self):
        response = self._response([
            {"url": "https://a.example", "title": "A", "snippet": "snip",
             "parsedText": "full", "datePublished": "2026-01-01", "siteName": "A"},
        ])
        out = g.extract_search_pages(response)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["snippet"], "snip")
        self.assertEqual(out[0]["parsedText"], "full")

    def test_top_level_tool_result_is_not_the_path(self):
        """把 toolResult 放顶层取不到东西 —— 防止实现方写错层级。"""
        response = {"toolResult": {"webSearch": {"webpages": [{"url": "u"}]}}}
        self.assertEqual(g.extract_search_pages(response), [])

    def test_multiple_chunks_are_all_scanned(self):
        response = {"outputChunks": [
            {"toolResult": {"webSearch": {"webpages": [{"url": "u1"}]}}},
            {"toolResult": {"webSearch": {"webpages": [{"url": "u2"}]}}},
        ]}
        self.assertEqual([p["url"] for p in g.extract_search_pages(response)], ["u1", "u2"])

    def test_malformed_shapes_do_not_raise(self):
        for response in ({}, {"outputChunks": None}, {"outputChunks": [None]},
                         {"outputChunks": [{"toolResult": None}]},
                         {"outputChunks": [{"toolResult": {"webSearch": None}}]},
                         {"outputChunks": [{"toolResult": {"webSearch": {"webpages": None}}}]}):
            self.assertEqual(g.extract_search_pages(response), [])


class ImageExtractionTest(unittest.TestCase):
    def test_streaming_frame_is_picked_up(self):
        response = {"streamingImageGenerationResponse": {
            "imageId": "i1", "imageUrl": "generated/x.png", "seq": 1,
            "progress": 100, "assetId": "a1", "hasWatermark": False,
        }}
        out = g.extract_images(response)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "stream")
        self.assertEqual(out[0]["image_url"], "https://assets.grok.com/generated/x.png")
        self.assertEqual(out[0]["asset_id"], "a1")

    def test_asset_metadata_filters_non_images(self):
        response = {"fileAttachmentAssetMetadata": [
            {"key": "img.png", "mimeType": "image/png", "assetId": "a1"},
            {"key": "doc.pdf", "mimeType": "application/pdf", "assetId": "a2"},
        ]}
        out = g.extract_images(response)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["asset_id"], "a1")

    def test_legacy_generated_image_urls(self):
        response = {"generatedImageUrls": ["https://cdn.example/x.png"]}
        out = g.extract_images(response)
        self.assertEqual(out[0]["image_url"], "https://cdn.example/x.png")

    def test_all_three_channels_coexist(self):
        response = {
            "streamingImageGenerationResponse": {"imageUrl": "s.png"},
            "fileAttachmentAssetMetadata": [{"key": "a.png", "mimeType": "image/png"}],
            "generatedImageUrls": ["g.png"],
        }
        kinds = [img["kind"] for img in g.extract_images(response)]
        self.assertEqual(kinds, ["stream", "asset", "url"])


class AssetUrlTest(unittest.TestCase):
    def test_relative_key_gets_the_cdn_prefix(self):
        self.assertEqual(g.asset_url("generated/x.png"),
                         "https://assets.grok.com/generated/x.png")

    def test_leading_slash_is_normalised(self):
        self.assertEqual(g.asset_url("/generated/x.png"),
                         "https://assets.grok.com/generated/x.png")

    def test_absolute_and_inline_urls_pass_through(self):
        for url in ("https://cdn.example/x.png", "http://x/y.png",
                    "data:image/png;base64,AAA", "blob:https://grok.com/uuid"):
            self.assertEqual(g.asset_url(url), url)

    def test_empty_is_none(self):
        for value in (None, "", 0, [], {}):
            self.assertIsNone(g.asset_url(value))


class RejectionTest(unittest.TestCase):
    """只看 imageUrl 会把「被拒」误判成「还在生成」。"""

    def test_rejection_reason_is_surfaced(self):
        response = {"streamingImageGenerationResponse": {
            "rejectionReason": "content policy", "progress": 0}}
        self.assertEqual(g.is_rejected(response), "content policy")

    def test_moderated_without_url_counts_as_rejected(self):
        response = {"streamingImageGenerationResponse": {"moderated": True}}
        self.assertEqual(g.is_rejected(response), "moderated")

    def test_moderated_with_url_is_not_rejected(self):
        response = {"streamingImageGenerationResponse": {
            "moderated": True, "imageUrl": "x.png"}}
        self.assertIsNone(g.is_rejected(response))

    def test_normal_frame_is_not_rejected(self):
        response = {"streamingImageGenerationResponse": {"imageUrl": "x.png"}}
        self.assertIsNone(g.is_rejected(response))

    def test_missing_frame_is_not_rejected(self):
        self.assertIsNone(g.is_rejected({}))


class BuildBodyTest(unittest.TestCase):
    def test_minimal_body(self):
        body = g.build_body("hi")
        self.assertEqual(body["message"], "hi")
        self.assertEqual(body["modelName"], g.DEFAULT_MODEL)
        self.assertFalse(body["disableSearch"])
        self.assertNotIn("toolOverrides", body, "全开时不下发 toolOverrides")
        self.assertEqual(len(body["deviceEnvInfo"]), 6)

    def test_search_false_uses_tool_overrides_not_disable_search(self):
        """bundle 里 disableSearch 恒为 false，关搜索走 toolOverrides。"""
        body = g.build_body("hi", search=False)
        self.assertFalse(body["disableSearch"], "disableSearch 不应被置 true")
        self.assertEqual(body["toolOverrides"], {"webSearch": False, "xSearch": False})

    def test_extra_tools_can_be_disabled(self):
        body = g.build_body("hi", disable_tools=["imageGen", "videoGen"])
        self.assertEqual(body["toolOverrides"],
                         {"imageGen": False, "videoGen": False})

    def test_unknown_tool_key_is_rejected(self):
        with self.assertRaises(ValueError):
            g.build_body("hi", disable_tools=["notARealTool"])

    def test_image_body_shape(self):
        body = g.build_body("a cat", image=True, image_count=4,
                            aspect_ratio="16:9", resolution="2k", image_mode="quality")
        self.assertEqual(body["kind"], "CONVERSATION_KIND_IMAGINE")
        self.assertTrue(body["enableImageGeneration"])
        self.assertTrue(body["enableImageStreaming"])
        gen = body["mediaGenInput"]["textToImage"]
        self.assertEqual(gen["prompt"], "a cat")
        self.assertEqual(gen["numOfImages"], 4)
        self.assertEqual(gen["aspectRatio"], "16:9")
        self.assertEqual(gen["resolutionName"], "2k")
        self.assertEqual(gen["mode"], "quality")
        self.assertEqual(
            body["responseMetadata"]["modelConfigOverride"]["modelMap"]["imageEditModel"],
            "imagine",
        )

    def test_image_body_omits_unset_optionals(self):
        gen = g.build_body("x", image=True)["mediaGenInput"]["textToImage"]
        for key in ("aspectRatio", "resolutionName", "mode"):
            self.assertNotIn(key, gen)

    def test_search_and_image_combine(self):
        body = g.build_body("x", image=True, search=False)
        self.assertEqual(body["toolOverrides"], {"webSearch": False, "xSearch": False})
        self.assertEqual(body["kind"], "CONVERSATION_KIND_IMAGINE")

    def test_tool_keys_are_the_documented_fourteen(self):
        self.assertEqual(len(g.TOOL_KEYS), 14)
        self.assertIn("webSearch", g.TOOL_KEYS)
        self.assertIn("xSearch", g.TOOL_KEYS)
        self.assertIn("imageGen", g.TOOL_KEYS)


class BuildHeadersTest(unittest.TestCase):
    def test_required_headers_present(self):
        headers = g.build_headers()
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Origin"], "https://grok.com")
        self.assertIn("x-xai-request-id", headers)

    def test_request_id_is_a_fresh_uuid_each_call(self):
        import uuid as _uuid

        first = g.build_headers()["x-xai-request-id"]
        second = g.build_headers()["x-xai-request-id"]
        self.assertNotEqual(first, second)
        _uuid.UUID(first)  # 必须是合法 UUID，否则抛错

    def test_absent_headers_are_not_invented(self):
        """这些头在 bundle 里不存在或未确认发送，不应出现。"""
        headers = g.build_headers()
        for name in ("x-statsig-id", "x-app-version", "x-device-model",
                     "x-device-vendor", "x-app-language", "x-app-timezone",
                     "Authorization"):
            self.assertNotIn(name, headers)


class CookieParsingTest(unittest.TestCase):
    def test_dict_form(self):
        self.assertEqual(g._cookies({"a": "1"}), {"a": "1"})

    def test_header_form(self):
        self.assertEqual(g._cookies("a=1; b=2"), {"a": "1", "b": "2"})

    def test_empty_is_none(self):
        for value in (None, "", "   ", {}):
            self.assertIsNone(g._cookies(value))

    def test_values_with_equals_are_kept_whole(self):
        self.assertEqual(g._cookies("t=a=b=c"), {"t": "a=b=c"})


class StreamWithoutCookiesTest(unittest.TestCase):
    def test_missing_cookie_raises_before_any_request(self):
        with self.assertRaises(g.GrokWebError):
            list(g.stream_conversation("hi", cookies=""))


class AskAggregationTest(unittest.TestCase):
    """用合成流验证聚合逻辑（不触网）。"""

    def _lines(self, *responses):
        return [json.dumps({"result": {"response": r}}).encode() for r in responses]

    def test_text_is_concatenated_in_order(self):
        lines = self._lines({"message": "Hello"}, {"message": ", "}, {"message": "world"})
        parts = [g.extract_text(g.unwrap(c)[0]) for c in g.iter_ndjson(lines)]
        self.assertEqual("".join(parts), "Hello, world")

    def test_unwrap_is_required_before_extract(self):
        """extract_text 作用在 ModelResponse 上，不是整个信封。"""
        lines = self._lines({"message": "hi"})
        chunk = next(g.iter_ndjson(lines))
        self.assertEqual(g.extract_text(chunk), "", "信封上没有 message")
        self.assertEqual(g.extract_text(g.unwrap(chunk)[0]), "hi")

    def test_dedupe_keeps_first_occurrence(self):
        items = [{"url": "u1", "t": 1}, {"url": "u1", "t": 2}, {"url": "u2"}]
        out = g._dedupe(items, "url")
        self.assertEqual([i["url"] for i in out], ["u1", "u2"])
        self.assertEqual(out[0]["t"], 1)

    def test_dedupe_tolerates_missing_key(self):
        out = g._dedupe([{"a": 1}, {"a": 1}], "url")
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()


class EndToEndAggregationTest(unittest.TestCase):
    """mock 掉 requests，验证 stream_conversation → ask 的完整聚合。

    这样能覆盖：NDJSON 分帧 → 信封拆解 → 文本/来源/图片聚合 → 去重。
    唯一没覆盖的是真实网络与认证。
    """

    def _fake_response(self, lines, *, status=200, headers=None):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = status
        resp.headers = headers or {}
        resp.text = "\n".join(lines)
        resp.iter_lines.return_value = [line.encode() for line in lines]
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return resp

    def _patch_post(self, resp):
        from unittest import mock

        return mock.patch.object(g.requests, "post", return_value=resp)

    def test_search_flow_aggregates_text_and_sources(self):
        lines = [
            '{"result":{"response":{"responseId":"r1","message":"The answer is "}}}',
            '{"result":{"response":{"message":"42."}}}',
            '{"result":{"response":{"conversationId":"c1","outputChunks":['
            '{"toolResult":{"webSearch":{"webpages":['
            '{"url":"https://a.example","title":"A","snippet":"snip"}]}}}]}}}',
            '{"result":{"response":{"citedWebSearchResults":{"results":['
            '{"url":"https://b.example","title":"B"}]}}}}',
        ]
        with self._patch_post(self._fake_response(lines)):
            out = g.search("what is the answer", cookies={"s": "1"})

        self.assertEqual(out["answer"], "The answer is 42.")
        self.assertEqual(out["response_id"], "r1")
        self.assertEqual(out["conversation_id"], "c1")
        # sources 是检索到的网页（富字段版优先）
        self.assertEqual([s["url"] for s in out["sources"]], ["https://a.example"])
        # citations 是被正文实际引用的子集，两者都暴露
        self.assertEqual([c["url"] for c in out["citations"]], ["https://b.example"])

    def test_sources_prefer_the_rich_pages_over_citations(self):
        lines = [
            '{"result":{"response":{"outputChunks":[{"toolResult":{"webSearch":'
            '{"webpages":[{"url":"https://rich.example","snippet":"full text"}]}}}]}}}',
            '{"result":{"response":{"citedWebSearchResults":{"results":['
            '{"url":"https://thin.example"}]}}}}',
        ]
        with self._patch_post(self._fake_response(lines)):
            out = g.search("q", cookies={"s": "1"})
        self.assertEqual([s["url"] for s in out["sources"]], ["https://rich.example"])
        self.assertEqual([c["url"] for c in out["citations"]], ["https://thin.example"])

    def test_sources_fall_back_to_citations_when_no_rich_pages(self):
        lines = ['{"result":{"response":{"citedWebSearchResults":{"results":['
                 '{"url":"https://only.example"}]}}}}']
        with self._patch_post(self._fake_response(lines)):
            out = g.search("q", cookies={"s": "1"})
        self.assertEqual([s["url"] for s in out["sources"]], ["https://only.example"])

    def test_image_flow_collects_urls_and_rejections(self):
        lines = [
            '{"result":{"response":{"streamingImageGenerationResponse":'
            '{"imageUrl":"gen/1.png","assetId":"a1","progress":100}}}}',
            '{"result":{"response":{"streamingImageGenerationResponse":'
            '{"rejectionReason":"content policy"}}}}',
        ]
        with self._patch_post(self._fake_response(lines)):
            out = g.generate_image("a cat", cookies={"s": "1"})

        self.assertEqual([i["image_url"] for i in out["images"]],
                         ["https://assets.grok.com/gen/1.png"])
        self.assertEqual(out["rejected"], ["content policy"])

    def test_duplicate_urls_are_deduped_across_chunks(self):
        lines = [
            '{"result":{"response":{"streamingImageGenerationResponse":'
            '{"imageUrl":"gen/1.png","progress":50}}}}',
            '{"result":{"response":{"streamingImageGenerationResponse":'
            '{"imageUrl":"gen/1.png","progress":100}}}}',
        ]
        with self._patch_post(self._fake_response(lines)):
            out = g.generate_image("x", cookies={"s": "1"})
        self.assertEqual(len(out["images"]), 1)

    def test_429_raises_with_status(self):
        with self._patch_post(self._fake_response([], status=429)):
            with self.assertRaises(g.GrokWebError) as ctx:
                list(g.stream_conversation("x", cookies={"s": "1"}))
        self.assertEqual(ctx.exception.status, 429)

    def test_403_reports_cf_mitigated_header(self):
        with self._patch_post(self._fake_response([], status=403,
                                                  headers={"cf-mitigated": "challenge"})):
            with self.assertRaises(g.GrokWebError) as ctx:
                list(g.stream_conversation("x", cookies={"s": "1"}))
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("challenge", str(ctx.exception))

    def test_in_stream_error_is_raised(self):
        lines = ['{"result":{},"error":{"code":"x","message":"upstream boom"}}']
        with self._patch_post(self._fake_response(lines)):
            with self.assertRaises(g.GrokWebError) as ctx:
                list(g.stream_conversation("x", cookies={"s": "1"}))
        self.assertIn("upstream boom", str(ctx.exception))

    def test_conversation_id_switches_to_the_add_response_endpoint(self):
        lines = ['{"result":{"message":"ok","responseId":"r1"}}']
        with self._patch_post(self._fake_response(lines)) as post:
            list(g.stream_conversation("x", cookies={"s": "1"}, conversation_id="c9"))
        url = post.call_args[0][0]
        self.assertIn("/conversations/c9/responses", url)

    def test_new_conversation_uses_the_new_endpoint(self):
        lines = ['{"result":{"response":{"message":"ok"}}}']
        with self._patch_post(self._fake_response(lines)) as post:
            list(g.stream_conversation("x", cookies={"s": "1"}))
        url = post.call_args[0][0]
        self.assertTrue(url.endswith("/rest/app-chat/conversations/new"), url)

    def test_image_request_body_reaches_the_wire(self):
        lines = ['{"result":{"response":{"message":"ok"}}}']
        with self._patch_post(self._fake_response(lines)) as post:
            list(g.stream_conversation("a cat", cookies={"s": "1"},
                                       image=True, image_count=3))
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["kind"], "CONVERSATION_KIND_IMAGINE")
        self.assertEqual(body["mediaGenInput"]["textToImage"]["numOfImages"], 3)
