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

        return mock.patch.object(g, "_stream_post", return_value=resp)

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
        body = post.call_args.kwargs["body"]
        self.assertEqual(body["kind"], "CONVERSATION_KIND_IMAGINE")
        self.assertEqual(body["mediaGenInput"]["textToImage"]["numOfImages"], 3)


class ModelUnavailableTest(unittest.TestCase):
    """`"Invalid request. Please update your client."` 是兜底文案，不是版本问题。

    实测 2026-10-19：`grok-3` 在网页路径返回 200，会话被创建、用户消息被回显，
    然后流里给这两条：

        {"token":"Invalid request. Please update your client.","messageTag":"final"}
        {"error":{"message":"This model is temporarily unavailable. ...",
                  "modelNotAvailable":{},"severity":"STREAM_ERROR_SEVERITY_NORMAL"}}

    只看 `token` 字段会误判成「客户端版本太旧」而去改请求头 —— 实测所有头变体
    （octane / oai-device-id / Sec-Fetch / Accept-Language）都停在同一个错误。
    """

    def test_model_not_available_error_is_surfaced(self):
        response = {"error": {"message": "This model is temporarily unavailable. "
                                         "Please try a different model.",
                              "modelNotAvailable": {},
                              "severity": "STREAM_ERROR_SEVERITY_NORMAL"}}
        reason = g.model_unavailable(response)
        self.assertIsNotNone(reason)
        self.assertIn("temporarily unavailable", reason)

    def test_the_fallback_token_is_recognised(self):
        response = {"token": "Invalid request. Please update your client.",
                    "messageTag": "final"}
        self.assertEqual(g.model_unavailable(response), g.CLIENT_UPDATE_NOTICE)

    def test_a_normal_response_is_not_flagged(self):
        for response in ({}, {"message": "hello"}, {"token": ""},
                         {"error": None}, {"error": "not a dict"}):
            self.assertIsNone(g.model_unavailable(response))

    def test_a_generic_error_message_is_not_claimed_as_model_unavailable(self):
        """`model_unavailable` 只认「模型不可用」，不吞其它错误。

        否则 `image_rate_limited()` 的 `renderToolRateLimited` 会被误判成模型问题。
        """
        self.assertIsNone(g.model_unavailable({"error": {"message": "boom"}}))
        self.assertEqual(
            g.model_unavailable({"error": {"message": "The model is unavailable."}}),
            "The model is unavailable.",
        )

    def test_the_two_chunks_of_a_real_failure_both_report(self):
        """真实失败会先后给这两条，两条都要能识别。"""
        first = {"token": "Invalid request. Please update your client.",
                 "messageTag": "final", "responseId": "r1"}
        second = {"error": {"message": "This model is temporarily unavailable.",
                            "modelNotAvailable": {}}, "responseId": "r1"}
        self.assertIsNotNone(g.model_unavailable(first))
        self.assertIsNotNone(g.model_unavailable(second))


class BearerAuthTest(unittest.TestCase):
    """实况修正：网页路径接受 `Authorization: Bearer`，不是 Cookie。

    实测：`GET /rest/app-chat/conversations` + Bearer → 200；
    所有 cookie 名变体（sso / sso-rw / token / access_token /
    __Secure-next-auth.session-token）全部 401。
    """

    def test_bearer_is_emitted_when_given(self):
        headers = g.build_headers(bearer="tok-fixture")
        self.assertEqual(headers["Authorization"], "Bearer tok-fixture")

    def test_no_authorization_header_without_a_token(self):
        self.assertNotIn("Authorization", g.build_headers())
        self.assertNotIn("Authorization", g.build_headers(bearer=""))
        self.assertNotIn("Authorization", g.build_headers(bearer="   "))

    def test_stream_accepts_bearer_alone(self):
        from unittest import mock

        lines = ['{"result":{"response":{"message":"ok"}}}']
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.text = "\n".join(lines)
        resp.iter_lines.return_value = [line.encode() for line in lines]
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with mock.patch.object(g, "_stream_post", return_value=resp) as post:
            out = list(g.stream_conversation("hi", bearer="tok-fixture"))
        self.assertEqual(len(out), 1)
        sent = post.call_args.kwargs["headers"]
        self.assertEqual(sent["Authorization"], "Bearer tok-fixture")

    def test_no_auth_at_all_is_rejected_before_any_request(self):
        with self.assertRaises(g.GrokWebError):
            list(g.stream_conversation("hi"))


class CardAttachmentImageTest(unittest.TestCase):
    """网页生图的图片走 `cardAttachment`，不是 `streamingImageGenerationResponse`。

    实测 2026-10-19（`model=build` + `CONVERSATION_KIND_IMAGINE`）：

        {"cardAttachment": {"jsonData": "{\\"id\\":\\"gi6r5\\",
          \\"type\\":\\"render_generated_image\\",
          \\"cardType\\":\\"generated_image_card\\",
          \\"resolution\\":{\\"width\\":784,\\"height\\":1176,
                          \\"aspect_ratio\\":\\"2:3\\"},
          \\"image_chunk\\":{\\"imageUuid\\":\\"...\\",
            \\"imageUrl\\":\\"users/<uid>/generated/<uuid>/image.jpg\\",
            \\"mimeType\\":\\"image/jpeg\\",\\"seq\\":1,\\"progress\\":100,
            \\"moderated\\":false,\\"imageModel\\":\\"imagine_x_1\\"}}}"}}

    `jsonData` 是**字符串化的 JSON**，要二次解析 —— 这是最容易漏的一步。
    """

    def _card(self, payload: dict) -> dict:
        return {"cardAttachment": {"jsonData": json.dumps(payload)}}

    def test_final_frame_yields_a_downloadable_url(self):
        response = self._card({
            "id": "gi6r5", "type": "render_generated_image",
            "cardType": "generated_image_card",
            "resolution": {"width": 784, "height": 1176, "aspect_ratio": "2:3"},
            "image_chunk": {
                "imageUuid": "15ccde3c-6622-4667-a308-0093f4928548",
                "imageUrl": "users/79e28854/generated/15ccde3c/image.jpg",
                "mimeType": "image/jpeg", "seq": 1, "progress": 100,
                "moderated": False, "imageModel": "imagine_x_1",
            },
        })
        out = g.extract_images(response)
        self.assertEqual(len(out), 1)
        img = out[0]
        self.assertEqual(img["kind"], "card")
        self.assertEqual(img["card_id"], "gi6r5")
        self.assertEqual(img["asset_id"], "15ccde3c-6622-4667-a308-0093f4928548")
        self.assertEqual(img["image_url"],
                         "https://assets.grok.com/users/79e28854/generated/15ccde3c/image.jpg")
        self.assertEqual(img["progress"], 100)
        self.assertEqual(img["image_model"], "imagine_x_1")
        self.assertEqual(img["resolution"]["aspect_ratio"], "2:3")
        self.assertFalse(img["moderated"])

    def test_the_progress_50_frame_has_no_url(self):
        """第一帧 `seq=0 progress=50` 带 `-part-0` 路径，第二帧才是最终图。"""
        first = self._card({
            "id": "gi6r5", "image_chunk": {
                "imageUuid": "u1", "progress": 50, "seq": 0,
                "imageUrl": "users/x/generated/u1-part-0/image.jpg",
            },
        })
        out = g.extract_images(first)
        self.assertEqual(out[0]["progress"], 50)
        self.assertIn("-part-0", out[0]["image_url"])

    def test_a_failed_frame_carries_system_err_code(self):
        """限流帧 `progress=100` 但**没有 imageUrl**，只有 `systemErrCode`。"""
        response = self._card({
            "id": "0AmCB", "image_chunk": {
                "imageUuid": "a461724a-e33b-4b9d-a2e2-8f98ca9fe9ce",
                "seq": 0, "progress": 100, "systemErrCode": 1006,
            },
        })
        out = g.extract_images(response)
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["image_url"])
        self.assertEqual(out[0]["system_err_code"], 1006)

    def test_json_data_may_already_be_a_dict(self):
        response = {"cardAttachment": {"jsonData": {
            "id": "c1", "image_chunk": {"imageUrl": "a/b.jpg", "progress": 100},
        }}}
        out = g.extract_images(response)
        self.assertEqual(out[0]["image_url"], "https://assets.grok.com/a/b.jpg")

    def test_malformed_json_data_does_not_raise(self):
        for raw in ("{not json", "", None, 42):
            response = {"cardAttachment": {"jsonData": raw}}
            self.assertEqual(g.extract_images(response), [])

    def test_a_card_without_image_chunk_is_ignored(self):
        """生图请求刚发出时 `image_chunk` 是 null。"""
        response = self._card({"id": "gi6r5", "type": "render_generated_image",
                               "prompt": "a red apple", "image_chunk": None})
        self.assertEqual(g.extract_images(response), [])


class ImageRateLimitTest(unittest.TestCase):
    """连续生图会触发限流，且**失败帧的 progress 仍是 100**。"""

    def test_render_tool_rate_limited_is_detected(self):
        response = {"error": {
            "message": "You've hit your image rate limit. Please try again later.",
            "renderToolRateLimited": {"rateLimitedCount": 1},
            "severity": "STREAM_ERROR_SEVERITY_NORMAL",
        }}
        reason = g.image_rate_limited(response)
        self.assertIsNotNone(reason)
        self.assertIn("rate limit", reason.lower())

    def test_a_message_only_rate_limit_is_detected(self):
        response = {"error": {"message": "You've hit your image rate limit."}}
        self.assertIsNotNone(g.image_rate_limited(response))

    def test_a_normal_response_is_not_flagged(self):
        for response in ({}, {"message": "hi"}, {"error": None},
                         {"error": {"message": "model not available"}}):
            self.assertIsNone(g.image_rate_limited(response))

    def test_model_unavailable_and_rate_limit_are_distinct(self):
        """两个错误不能互相误判。"""
        unavailable = {"error": {"message": "This model is temporarily unavailable.",
                                 "modelNotAvailable": {}}}
        limited = {"error": {"message": "You've hit your image rate limit.",
                             "renderToolRateLimited": {"rateLimitedCount": 1}}}
        self.assertIsNotNone(g.model_unavailable(unavailable))
        self.assertIsNone(g.image_rate_limited(unavailable))
        self.assertIsNotNone(g.image_rate_limited(limited))
        self.assertIsNone(g.model_unavailable(limited))


class ImageModelConstantsTest(unittest.TestCase):
    """模型名是实测出来的，不能随意改。"""

    def test_the_working_model_is_build(self):
        self.assertEqual(g.IMAGE_MODEL, "build")

    def test_asset_server_is_the_cdn(self):
        self.assertEqual(g.ASSET_SERVER_URL, "https://assets.grok.com")

    def test_imagine_kind_literal(self):
        self.assertEqual(g.KIND_IMAGINE, "CONVERSATION_KIND_IMAGINE")

    def test_image_body_uses_the_working_model(self):
        body = g.build_body("x", image=True, model=g.IMAGE_MODEL)
        self.assertEqual(body["modelName"], "build")
        self.assertEqual(body["kind"], "CONVERSATION_KIND_IMAGINE")


class DownloadImageTest(unittest.TestCase):
    """下载需要 Bearer —— 实测无认证 403。"""

    def test_missing_bearer_raises_before_any_request(self):
        with self.assertRaises(g.GrokWebError):
            g.download_image("users/x/generated/u/image.jpg")

    def test_empty_key_raises(self):
        with self.assertRaises(g.GrokWebError):
            g.download_image("", bearer="tok")

    def test_the_request_carries_the_bearer_header(self):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = 200
        resp.content = b"\xff\xd8\xff" + b"x" * 2000
        with mock.patch.object(g, "_stream_get", return_value=resp) as sg:
            data = g.download_image("users/x/generated/u/image.jpg", bearer="tok-fixture")
        self.assertEqual(len(data), 2003)
        sent = sg.call_args.kwargs["headers"]
        self.assertEqual(sent["Authorization"], "Bearer tok-fixture")

    def test_a_non_200_raises_with_status(self):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = 403
        with mock.patch.object(g, "_stream_get", return_value=resp):
            with self.assertRaises(g.GrokWebError) as ctx:
                g.download_image("users/x/image.jpg", bearer="tok")
        self.assertEqual(ctx.exception.status, 403)


class GenerateImageDedupeTest(unittest.TestCase):
    """同一张图的 progress=50 / progress=100 两帧要合成一个最终态。"""

    def test_failed_frames_are_separated_from_successful_ones(self):
        from unittest import mock

        frames = [
            {"result": {"response": {"cardAttachment": {"jsonData": json.dumps({
                "id": "c1", "image_chunk": {"imageUuid": "u1", "progress": 50,
                                            "imageUrl": "a/u1-part-0/image.jpg"}})}}}},
            {"result": {"response": {"cardAttachment": {"jsonData": json.dumps({
                "id": "c1", "image_chunk": {"imageUuid": "u1", "progress": 100,
                                            "imageModel": "imagine_x_1",
                                            "imageUrl": "a/u1/image.jpg"}})}}}},
            {"result": {"response": {"cardAttachment": {"jsonData": json.dumps({
                "id": "c2", "image_chunk": {"imageUuid": "u2", "progress": 100,
                                            "systemErrCode": 1006}})}}}},
        ]
        lines = [json.dumps(f).encode() for f in frames]
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.text = "\n".join(json.dumps(f) for f in frames)
        resp.iter_lines.return_value = lines
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None

        with mock.patch.object(g, "_stream_post", return_value=resp):
            out = g.generate_image("x", bearer="tok")

        self.assertEqual(len(out["images"]), 1, "两帧同一张图应合成一个")
        self.assertEqual(out["images"][0]["progress"], 100)
        self.assertNotIn("-part-0", out["images"][0]["image_url"])
        self.assertEqual(len(out["failed"]), 1, "失败帧单独收集")
        self.assertEqual(out["failed"][0]["system_err_code"], 1006)


class QuotaEndpointTest(unittest.TestCase):
    """额度接口 —— 三个端点，只有 CLI 那个对 OAuth2 用户可用。

    实测 2026-10-19：

      GET  /rest/usage/free-usage-gates   ✅ 200，但三个凭证全是 0/0
                                             （**生图却可用** ⇒ 别用它判断能否生图）
      POST /rest/media/imagine/quota_info ❌ 403 oauth2-auth-forbidden
      POST cli-chat-proxy/v1/responses    ✅ 带真实限流头
                                             x-ratelimit-limit-requests: 21
                                             x-ratelimit-limit-tokens: 1000000
    """

    def test_free_usage_gates_parses_all_four_gates(self):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "chat": {"allowance": "10", "remaining": "3"},
            "imagine": {"allowance": "20", "remaining": "0"},
            "voice": {"allowance": "5", "remaining": "5"},
            "build": {"allowance": "0", "remaining": "0"},
        }
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with mock.patch.object(g, "_stream_get", return_value=resp):
            out = g.free_usage_gates(bearer="tok")
        self.assertEqual(out["chat"], {"allowance": 10, "remaining": 3})
        self.assertEqual(out["imagine"], {"allowance": 20, "remaining": 0})
        self.assertEqual(out["voice"]["remaining"], 5)
        self.assertEqual(out["build"]["allowance"], 0)

    def test_free_usage_gates_tolerates_missing_gates(self):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"chat": {"allowance": 1, "remaining": 1}}
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with mock.patch.object(g, "_stream_get", return_value=resp):
            out = g.free_usage_gates(bearer="tok")
        self.assertEqual(out["imagine"], {"allowance": 0, "remaining": 0})

    def test_quota_info_403_is_reported_as_auth_forbidden(self):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = 403
        resp.text = '{"code":7,"message":"Action cannot be performed by OAuth2 token users."}'
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with mock.patch.object(g, "_stream_post", return_value=resp):
            with self.assertRaises(g.GrokWebError) as ctx:
                g.imagine_quota(bearer="tok")
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("OAuth2", str(ctx.exception))

    def test_quota_info_returns_the_payload_when_allowed(self):
        from unittest import mock

        payload = {
            "image": {"estimate": {"remainingImageGenFast1k": 5,
                                   "remainingImageGenQuality2k": 2},
                      "shouldShow": True,
                      "imagineFreeCreditActive": True,
                      "imagineFreeCreditAllowance": 10,
                      "imagineFreeCreditRemaining": 7},
            "maxVideoDurationSeconds": 6,
        }
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = payload
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with mock.patch.object(g, "_stream_post", return_value=resp):
            out = g.imagine_quota(bearer="tok")
        self.assertEqual(out["image"]["estimate"]["remainingImageGenFast1k"], 5)
        self.assertEqual(out["maxVideoDurationSeconds"], 6)

    def test_quota_categories_and_fields_match_the_bundle(self):
        self.assertEqual(g.QUOTA_CATEGORIES,
                         ("image", "imagePro", "imageEdit", "video", "video720p"))
        self.assertEqual(len(g.QUOTA_ESTIMATE_FIELDS), 18)
        for name in ("remainingImageGenFast1k", "remainingImageGenFast2k",
                     "remainingImageGenQuality1k", "remainingImageGenQuality2k",
                     "remainingImageEditQuality1k", "remainingImageEditQuality2k",
                     "remainingT2v1080pSeconds", "remainingI2v1080pSeconds",
                     "remainingR2v1080pSeconds", "remainingExtension1080pSeconds"):
            self.assertIn(name, g.QUOTA_ESTIMATE_FIELDS)

    def test_cli_rate_limits_reads_the_four_headers(self):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {
            "x-ratelimit-limit-requests": "21",
            "x-ratelimit-limit-tokens": "1000000",
            "x-ratelimit-remaining-requests": "16",
            "x-ratelimit-remaining-tokens": "999000",
        }
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with mock.patch.object(g, "_stream_post", return_value=resp):
            out = g.cli_rate_limits("tok")
        self.assertEqual(out["limit-requests"], 21)
        self.assertEqual(out["remaining-requests"], 16)
        self.assertEqual(out["limit-tokens"], 1000000)
        self.assertEqual(out["remaining-tokens"], 999000)

    def test_cli_rate_limits_probes_responses_not_models(self):
        """`/models` 不带限流头 —— 必须打 `/responses`。"""
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with mock.patch.object(g, "_stream_post", return_value=resp) as sp:
            g.cli_rate_limits("tok")
        self.assertIn("/responses", sp.call_args[0][0])

    def test_cli_rate_limits_can_skip_the_probe(self):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with mock.patch.object(g, "_stream_get", return_value=resp) as sg:
            g.cli_rate_limits("tok", probe=False)
        self.assertIn("/models", sg.call_args[0][0])

    def test_cli_rate_limits_requires_a_bearer(self):
        with self.assertRaises(g.GrokWebError):
            g.cli_rate_limits("")

    def test_as_int_handles_strings_and_garbage(self):
        self.assertEqual(g._as_int("42"), 42)
        self.assertEqual(g._as_int(42), 42)
        self.assertEqual(g._as_int(None), 0)
        self.assertEqual(g._as_int(""), 0)
        self.assertEqual(g._as_int("abc"), 0)


class SilentDegradeTest(unittest.TestCase):
    """生图被拒的**第三种形态**：静默降级。

    实测 2026-10-19，三个凭证各命中一种：

      1. HTTP 429 `{"error":{"code":8,"message":"Too many requests"}}`（请求层）
      2. 流内 `renderToolRateLimited` + `systemErrCode: 1006`（生图层）
      3. **HTTP 200、无 error、无 cardAttachment、只有文字**（额度耗尽后
         服务端把生图请求当普通对话回）

    第 3 种最隐蔽 —— 只看 `error` 字段会把它误判成成功。
    """

    def _stream(self, frames):
        from unittest import mock

        lines = [json.dumps(f).encode() for f in frames]
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.text = "\n".join(json.dumps(f) for f in frames)
        resp.iter_lines.return_value = lines
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return mock.patch.object(g, "_stream_post", return_value=resp)

    def test_text_only_response_is_flagged_as_silent_degrade(self):
        frames = [
            {"result": {"response": {"messageTag": "final", "token": "Got it"}}},
            {"result": {"response": {"messageTag": "final", "token": " — test received."}}},
            {"result": {"response": {"modelResponse": {
                "message": "Got it — test received. How can I help?"}}}},
        ]
        with self._stream(frames):
            out = g.generate_image("a dot", bearer="tok")
        self.assertEqual(out["images"], [])
        self.assertTrue(out["silent_degrade"], "只有文字时必须标记为静默降级")
        self.assertIsNotNone(out["warning"])

    def test_a_real_image_is_not_flagged(self):
        frames = [
            {"result": {"response": {"cardAttachment": {"jsonData": json.dumps({
                "id": "c1", "image_chunk": {"imageUuid": "u1", "progress": 100,
                                            "imageUrl": "a/u1/image.jpg"}})}}}},
        ]
        with self._stream(frames):
            out = g.generate_image("a dot", bearer="tok")
        self.assertEqual(len(out["images"]), 1)
        self.assertFalse(out["silent_degrade"])

    def test_an_explicit_rate_limit_is_not_flagged_as_silent(self):
        frames = [
            {"result": {"response": {"error": {
                "message": "You've hit your image rate limit.",
                "renderToolRateLimited": {"rateLimitedCount": 1}}}}},
        ]
        with self._stream(frames):
            out = g.generate_image("a dot", bearer="tok")
        self.assertTrue(out["rate_limited"])
        self.assertFalse(out["silent_degrade"], "显式限流不应算静默降级")

    def test_a_failed_frame_is_not_flagged_as_silent(self):
        frames = [
            {"result": {"response": {"cardAttachment": {"jsonData": json.dumps({
                "id": "c1", "image_chunk": {"imageUuid": "u1", "progress": 100,
                                            "systemErrCode": 1006}})}}}},
        ]
        with self._stream(frames):
            out = g.generate_image("a dot", bearer="tok")
        self.assertEqual(len(out["failed"]), 1)
        self.assertFalse(out["silent_degrade"])

    def test_an_empty_response_is_not_flagged_as_silent(self):
        """完全没有正文时不算静默降级（那是另一种故障）。"""
        with self._stream([{"result": {"response": {"messageTag": "final",
                                                    "token": ""}}}]):
            out = g.generate_image("a dot", bearer="tok")
        self.assertFalse(out["silent_degrade"])

    def test_three_rejection_forms_are_distinguishable(self):
        """三种形态必须能被分别识别，不能互相误判。"""
        # 形态 2：流内限流
        limited = {"error": {"message": "You've hit your image rate limit.",
                             "renderToolRateLimited": {"rateLimitedCount": 1}}}
        self.assertIsNotNone(g.image_rate_limited(limited))
        self.assertIsNone(g.model_unavailable(limited))
        self.assertIsNone(g.is_rejected(limited))
        # 形态 3 的判定不在单帧上，而在聚合结果（见上面的用例）


class ControlPlaneTest(unittest.TestCase):
    """CLI 控制面：套餐 / 额度 / 策略。

    来源：codex2api 的 `grok-build-main/docs/GROK_BUILD_INFERENCE_API_REVERSE.md` §9，
    本仓库 2026-10-19 实测全部可用。

      GET /billing?format=credits       → 账期与用量百分比
      GET /user?include=subscription    → 实时套餐（**不发 x-userid**）
      GET /settings                     → min_client_version / allow_access
      GET /auto-topup-rule              → 自动充值规则（实测三个凭证都是 {}）
    """

    def _resp(self, payload, status=200):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = status
        resp.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        resp.json.return_value = payload
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return resp

    def test_billing_unwraps_the_config_envelope(self):
        from unittest import mock

        payload = {"config": {
            "creditUsagePercent": 1.0,
            "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                              "start": "2026-10-19T05:23:41.035+00:00",
                              "end": "2026-10-26T05:23:41.035+00:00"},
            "prepaidBalance": {"val": 0},
            "isUnifiedBillingUser": True,
            "productUsage": [{"product": "GrokBuild", "usagePercent": 1.0}],
        }}
        with mock.patch.object(g, "_stream_get", return_value=self._resp(payload)):
            out = g.billing("tok")
        self.assertEqual(out["creditUsagePercent"], 1.0)
        self.assertEqual(out["currentPeriod"]["type"], "USAGE_PERIOD_TYPE_WEEKLY")
        self.assertEqual(out["productUsage"][0]["product"], "GrokBuild")

    def test_billing_missing_percent_stays_missing(self):
        """**缺失 ≠ 0%** —— 不能把字段缺失当成明确 0%。"""
        from unittest import mock

        payload = {"config": {"currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY"},
                              "prepaidBalance": {"val": 0}}}
        with mock.patch.object(g, "_stream_get", return_value=self._resp(payload)):
            out = g.billing("tok")
        self.assertNotIn("creditUsagePercent", out)
        self.assertIsNone(out.get("creditUsagePercent"))

    def test_billing_requires_a_bearer(self):
        with self.assertRaises(g.GrokWebError):
            g.billing("")

    def test_subscription_does_not_send_x_userid(self):
        """`/user` 与 billing 的头差异：**不发 `x-userid`**。"""
        from unittest import mock

        with mock.patch.object(g, "_stream_get",
                               return_value=self._resp({"userId": "u1"})) as sg:
            g.subscription("tok")
        sent = sg.call_args.kwargs["headers"]
        self.assertNotIn("x-userid", sent)
        self.assertEqual(sent["X-XAI-Token-Auth"], "xai-grok-cli")

    def test_settings_reports_min_client_version(self):
        from unittest import mock

        with mock.patch.object(g, "_stream_get",
                               return_value=self._resp({
                                   "min_client_version": "1.0.13",
                                   "force_update": True,
                                   "allow_access": True})) as sg:
            out = g.client_settings("tok")
        self.assertEqual(out["min_client_version"], "1.0.13")
        self.assertTrue(out["force_update"])
        self.assertIn("/settings", sg.call_args[0][0])

    def test_auto_topup_rule_returns_empty_when_absent(self):
        from unittest import mock

        with mock.patch.object(g, "_stream_get", return_value=self._resp({})):
            self.assertEqual(g.auto_topup_rule("tok"), {})

    def test_control_headers_carry_the_required_set(self):
        h = g._control_headers("tok", user_id="u1")
        self.assertEqual(h["Authorization"], "Bearer tok")
        self.assertEqual(h["X-XAI-Token-Auth"], "xai-grok-cli")
        self.assertEqual(h["x-grok-client-version"], g.CONTROL_CLIENT_VERSION)
        self.assertEqual(h["x-grok-client-mode"], "headless")
        self.assertEqual(h["x-userid"], "u1")

    def test_control_headers_omit_optional_fields_by_default(self):
        h = g._control_headers("tok")
        for key in ("x-userid", "x-email", "x-grok-client-identifier"):
            self.assertNotIn(key, h)


class VersionGateTest(unittest.TestCase):
    """版本门控 —— 实测 2026-10-19。

    早先硬编码 `x-grok-client-version: 1.0.1`，而上游要求 **1.0.13**：

      version=1.0.1  → 426 {"error":"Your Grok CLI version (1.0.1) is outdated.
                             Please update to version 1.0.13 or later ..."}
      version=1.0.13 → 200
      version=1.0.62 → 200

    **控制面端点不做版本门控**（1.0.1 也能拿 billing），只有生成端点做。
    """

    OUTDATED = ('{"error":"Your Grok CLI version (1.0.1) is outdated. '
                'Please update to version 1.0.13 or later via `grok update` '
                'or the installation documentation."}')

    def test_the_outdated_body_is_detected(self):
        self.assertIsNotNone(g.version_outdated(self.OUTDATED))

    def test_the_required_version_is_extracted(self):
        self.assertEqual(g.required_client_version(self.OUTDATED), "1.0.13")

    def test_a_normal_response_is_not_flagged(self):
        for body in ('{"id":"x","object":"response"}', "", "{}", None):
            self.assertIsNone(g.version_outdated(body))

    def test_a_dict_error_is_handled(self):
        self.assertIsNotNone(g.version_outdated(
            {"error": "Your Grok CLI version (1.0.1) is outdated."}))

    def test_the_constant_is_at_least_the_required_version(self):
        """常量必须 ≥ 上游要求，否则生成请求会被 426。"""
        parts = [int(x) for x in g.CONTROL_CLIENT_VERSION.split(".")]
        self.assertGreaterEqual(parts, [1, 0, 13])

    def test_426_maps_to_version_required(self):
        r = g.classify_upstream_error(426, self.OUTDATED)
        self.assertEqual(r["kind"], "version_required")
        self.assertEqual(r["cooldown_seconds"], 60.0)
        self.assertFalse(r["retryable"])


class UpstreamErrorClassificationTest(unittest.TestCase):
    """上游错误 → 冷却建议（对齐 codex2api `applyGrokCooldown`）。"""

    def test_free_quota_exhausted_cools_24h(self):
        body = ('{"code":"subscription:free-usage-exhausted","error":"You\'ve used '
                'all the included free usage for model grok-4.5-build-free for now. '
                'Usage resets over a rolling 24-hour window — tokens (actual/limit): '
                '1003617/1000000."}')
        r = g.classify_upstream_error(429, body)
        self.assertEqual(r["kind"], "free_quota_exhausted")
        self.assertEqual(r["cooldown_seconds"], 86400.0)
        self.assertFalse(r["retryable"])

    def test_spending_limit_cools_24h(self):
        r = g.classify_upstream_error(
            402, '{"error":{"code":"balance_exhausted","message":"usage balance exhausted"}}')
        self.assertEqual(r["kind"], "spending_limit")
        self.assertEqual(r["cooldown_seconds"], 86400.0)

    def test_a_bare_402_is_not_hard_isolated(self):
        """裸 402 证据不足 —— 不能硬隔离账号。"""
        r = g.classify_upstream_error(402, '{"error":{"message":"payment required"}}')
        self.assertEqual(r["kind"], "payment_required_unknown")
        self.assertEqual(r["cooldown_seconds"], 0.0)

    def test_body_markers_win_over_the_status_code(self):
        """免费额度耗尽可能以 429 或 402 出现 —— 先看错误体。"""
        body = '{"code":"subscription:free-usage-exhausted"}'
        for status in (402, 429):
            self.assertEqual(g.classify_upstream_error(status, body)["kind"],
                             "free_quota_exhausted")

    def test_429_is_retryable_with_a_short_cooldown(self):
        r = g.classify_upstream_error(429, '{"error":{"code":8}}')
        self.assertEqual(r["kind"], "rate_limited")
        self.assertTrue(r["retryable"])

    def test_403_never_triggers_a_token_refresh(self):
        r = g.classify_upstream_error(403, '{"code":7,"message":"oauth2-auth-forbidden"}')
        self.assertEqual(r["kind"], "forbidden")
        self.assertEqual(r["cooldown_seconds"], 300.0)
        self.assertFalse(r["retryable"])

    def test_5xx_is_retryable(self):
        for status in (500, 502, 503):
            self.assertTrue(g.classify_upstream_error(status, "")["retryable"])

    def test_429_cooldown_is_clamped_to_15_minutes(self):
        self.assertEqual(g.clamp_rate_limit_cooldown(30), 30.0)
        self.assertEqual(g.clamp_rate_limit_cooldown(3600), 900.0)
        self.assertEqual(g.clamp_rate_limit_cooldown(0), 60.0)
        self.assertEqual(g.clamp_rate_limit_cooldown(-5), 60.0)

    def test_the_cooldown_table_matches_the_documented_values(self):
        self.assertEqual(g.RATE_LIMIT_MAX_COOLDOWN, 900.0)
        self.assertEqual(g.FREE_QUOTA_COOLDOWN, 86400.0)
        self.assertEqual(g.SPENDING_LIMIT_COOLDOWN, 86400.0)
        self.assertEqual(g.COOLDOWN_BY_STATUS[403], 300.0)
        self.assertEqual(g.COOLDOWN_BY_STATUS[402], 0.0)


class WebCreditsEndpointTest(unittest.TestCase):
    """`GET /rest/grok/credits` —— 网页 UI 真正使用的额度端点。

    来自 bundle 模块 `9096207` 的 protobuf `fileDesc` 解码：

        rpc GetGrokCreditsConfig -> GET /rest/grok/credits

    返回 `GrokCreditsConfig`，其中 `product_usage[]` 按 `billing_product.Product`
    分项，**含 `PRODUCT_GROK_IMAGINE = 5`（生图独立产品位）**。

    **实测 CLI 签发的 OAuth token 被拒**（403 `bad-credentials`）：
    JWT 的 `aud`/`client_id` 是 `b1a00492-...`（Grok CLI 客户端），
    而该端点只认 grok.com 网页应用的 OAuth 客户端。
    """

    def _resp(self, payload, status=200):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = status
        resp.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        resp.json.return_value = payload
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return resp

    def test_the_path_matches_the_protobuf_annotation(self):
        self.assertEqual(g.CREDITS_PATH, "/rest/grok/credits")
        self.assertEqual(g.BUILD_BILLING_PATH, "/rest/grok-build/billing")

    def test_config_is_unwrapped(self):
        from unittest import mock

        payload = {"config": {
            "credit_usage_percent": 42.5,
            "current_period": {"type": "USAGE_PERIOD_TYPE_WEEKLY"},
            "product_usage": [{"product": "PRODUCT_GROK_IMAGINE",
                               "usage_percent": 60.0}],
            "is_unified_billing_user": True,
        }}
        with mock.patch.object(g, "_stream_get", return_value=self._resp(payload)):
            out = g.web_credits(bearer="tok")
        self.assertEqual(out["credit_usage_percent"], 42.5)
        self.assertEqual(out["product_usage"][0]["product"], "PRODUCT_GROK_IMAGINE")

    def test_a_403_is_surfaced_with_the_upstream_message(self):
        from unittest import mock

        body = ('{"code":7,"message":"The OAuth2 access token could not be '
                'validated. [WKE=unauthenticated:bad-credentials]","details":[]}')
        with mock.patch.object(g, "_stream_get", return_value=self._resp(body, 403)):
            with self.assertRaises(g.GrokWebError) as ctx:
                g.web_credits(bearer="tok")
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("bad-credentials", str(ctx.exception))

    def test_it_requires_authentication(self):
        with self.assertRaises(g.GrokWebError):
            g.web_credits()

    def test_the_product_enum_has_imagine_at_five(self):
        self.assertEqual(g.PRODUCT_ENUM[5], "PRODUCT_GROK_IMAGINE")
        self.assertEqual(g.PRODUCT_GROK_IMAGINE, 5)
        self.assertEqual(g.PRODUCT_ENUM[2], "PRODUCT_GROK_BUILD")
        self.assertEqual(g.PRODUCT_ENUM[4], "PRODUCT_GROK_CHAT")
        self.assertEqual(len(g.PRODUCT_ENUM), 12)

    def test_product_name_accepts_both_string_and_int(self):
        self.assertEqual(g.product_name("PRODUCT_GROK_IMAGINE"), "PRODUCT_GROK_IMAGINE")
        self.assertEqual(g.product_name(5), "PRODUCT_GROK_IMAGINE")
        self.assertIsNone(g.product_name("GROK_IMAGINE"))
        self.assertIsNone(g.product_name(None))
        self.assertIsNone(g.product_name(99))

    def test_usage_period_types(self):
        self.assertEqual(g.USAGE_PERIOD_TYPES[1], "USAGE_PERIOD_TYPE_MONTHLY")
        self.assertEqual(g.USAGE_PERIOD_TYPES[2], "USAGE_PERIOD_TYPE_WEEKLY")

    def test_the_config_field_list_matches_the_proto(self):
        self.assertEqual(len(g.CREDITS_CONFIG_FIELDS), 11)
        for name in ("credit_usage_percent", "product_usage", "current_period",
                     "is_unified_billing_user", "prepaid_balance"):
            self.assertIn(name, g.CREDITS_CONFIG_FIELDS)


class UsagePollingPolicyTest(unittest.TestCase):
    """前端轮询策略（bundle 提取）。

    ```js
    let f = [90, 75, 50, 25];
    function crossedThreshold(e,t){ return f.find(n => e<n && t>=n) ?? null }
    refetchInterval = pct>=90 ? 30000 : pct>=50 ? 60000 : 180000
    ```
    """

    def test_thresholds_match_the_bundle(self):
        self.assertEqual(g.USAGE_TOAST_THRESHOLDS, (90, 75, 50, 25))

    def test_poll_intervals_match_the_bundle(self):
        self.assertEqual(g.usage_poll_interval_ms(95), 30_000)
        self.assertEqual(g.usage_poll_interval_ms(90), 30_000)
        self.assertEqual(g.usage_poll_interval_ms(89.9), 60_000)
        self.assertEqual(g.usage_poll_interval_ms(50), 60_000)
        self.assertEqual(g.usage_poll_interval_ms(49.9), 180_000)
        self.assertEqual(g.usage_poll_interval_ms(0), 180_000)

    def test_a_missing_percent_does_not_poll(self):
        """缺失 ≠ 0% —— 不能当成「用量 0%」去走最慢档。"""
        self.assertIsNone(g.usage_poll_interval_ms(None))
        self.assertIsNone(g.usage_poll_interval_ms("abc"))

    def test_crossed_threshold_finds_the_first_crossing(self):
        self.assertEqual(g.crossed_usage_threshold(80, 92), 90)
        self.assertEqual(g.crossed_usage_threshold(20, 30), 25)
        self.assertEqual(g.crossed_usage_threshold(10, 80), 75)
        self.assertIsNone(g.crossed_usage_threshold(95, 99))
        self.assertIsNone(g.crossed_usage_threshold(10, 20))

    def test_crossed_threshold_ignores_missing_values(self):
        self.assertIsNone(g.crossed_usage_threshold(None, 95))
        self.assertIsNone(g.crossed_usage_threshold(50, None))


class WebCreditsTransientRetryTest(unittest.TestCase):
    """额度端点的上游瞬时故障。

    实测 2026-10-20：`503 {"code":14,"message":"cursor billing: snapshot failed"}` ——
    同一请求重试即恢复，所以必须重试而不是当成永久拒绝。
    """

    def _resp(self, payload, status=200):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = status
        resp.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        resp.json.return_value = payload
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return resp

    def test_a_snapshot_failure_is_retried(self):
        from unittest import mock

        bad = self._resp({"code": 14, "message": "cursor billing: snapshot failed"}, 503)
        good = self._resp({"config": {"creditUsagePercent": 12.5}})
        with mock.patch.object(g, "_stream_get", side_effect=[bad, good]) as sg:
            out = g.web_credits(bearer="tok", retries=3)
        self.assertEqual(out["creditUsagePercent"], 12.5)
        self.assertEqual(sg.call_count, 2)

    def test_a_persistent_503_raises_after_the_budget(self):
        from unittest import mock

        bad = self._resp({"code": 14, "message": "cursor billing: snapshot failed"}, 503)
        with mock.patch.object(g, "_stream_get", return_value=bad) as sg:
            with self.assertRaises(g.GrokWebError) as ctx:
                g.web_credits(bearer="tok", retries=2)
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(sg.call_count, 2)

    def test_a_403_is_not_retried(self):
        """403 是账号级策略拒绝 —— 重试没有意义。"""
        from unittest import mock

        forbidden = self._resp({"code": 7, "message": "bad-credentials"}, 403)
        with mock.patch.object(g, "_stream_get", return_value=forbidden) as sg:
            with self.assertRaises(g.GrokWebError):
                g.web_credits(bearer="tok", retries=3)
        self.assertEqual(sg.call_count, 1)

    def test_the_transient_markers_are_documented(self):
        self.assertIn("cursor billing", g.CREDITS_TRANSIENT_MARKERS)
        self.assertIn("snapshot failed", g.CREDITS_TRANSIENT_MARKERS)

    def test_sso_cookie_authentication_is_used(self):
        """`Cookie: sso=` 是网页会话路径（实测 200）。"""
        from unittest import mock

        good = self._resp({"config": {"creditUsagePercent": 0,
                                      "productUsage": []}})
        with mock.patch.object(g, "_stream_get", return_value=good) as sg:
            out = g.web_credits(cookies={"sso": "fixture-sso"})
        self.assertEqual(out["productUsage"], [])
        self.assertEqual(sg.call_args.kwargs["cookies"], {"sso": "fixture-sso"})


class RunCodeTest(unittest.TestCase):
    """`POST /rest/app-chat/run-code` —— 网页沙箱**真的执行代码**。

    实测 2026-10-20（SSO 会话）：

        python 3.12.3  /  Node v24.15.0  /  bash
        platform Linux-4.19.0-gvisor-x86_64-with-glibc2.39   ← gVisor
        cwd /home/workdir   uid 0   cpu 2
        DNS 通，**TCP 拒绝**（无网络出口）

    `language` 只认 `python` / `python3` / `bash` / `javascript`；
    `sh` / `node` / `sql` / `ruby` 一律内部错误。
    """

    def _resp(self, payload, status=200):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = status
        resp.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        resp.json.return_value = payload
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return resp

    def test_stdout_is_returned(self):
        from unittest import mock

        payload = {"success": True, "stdout": "hello from sandbox\n",
                   "stderr": "", "outputFiles": {}}
        with mock.patch.object(g, "_stream_post", return_value=self._resp(payload)) as sp:
            out = g.run_code("print('hello from sandbox')", cookies={"sso": "x"})
        self.assertTrue(out["success"])
        self.assertEqual(out["stdout"], "hello from sandbox\n")
        self.assertEqual(sp.call_args.kwargs["body"]["language"], "python")

    def test_the_language_enum_matches_the_probe(self):
        self.assertEqual(g.RUN_CODE_LANGUAGES,
                         ("python", "python3", "bash", "javascript"))
        self.assertEqual(g.RUN_CODE_DEFAULT_LANGUAGE, "python")

    def test_an_unsupported_language_fails_before_any_request(self):
        """`ruby` / `sql` / `sh` / `node` 都不支持 —— 本地就挡掉。"""
        from unittest import mock

        for lang in ("ruby", "sql", "sh", "node", "go"):
            with mock.patch.object(g, "_stream_post") as sp:
                with self.assertRaises(g.GrokWebError):
                    g.run_code("x", language=lang, cookies={"sso": "x"})
                sp.assert_not_called()

    def test_an_empty_language_falls_back_to_the_default(self):
        """空串回落 `python` —— 不是错误。"""
        from unittest import mock

        payload = {"success": True, "stdout": "", "stderr": "", "outputFiles": {}}
        with mock.patch.object(g, "_stream_post", return_value=self._resp(payload)) as sp:
            g.run_code("print(1)", language="", cookies={"sso": "x"})
        self.assertEqual(sp.call_args.kwargs["body"]["language"], "python")

    def test_javascript_is_accepted(self):
        from unittest import mock

        payload = {"success": True, "stdout": "js ok\n", "stderr": "",
                   "outputFiles": {}}
        with mock.patch.object(g, "_stream_post", return_value=self._resp(payload)) as sp:
            out = g.run_code("console.log('js ok')", language="javascript",
                             cookies={"sso": "x"})
        self.assertTrue(out["success"])
        self.assertEqual(sp.call_args.kwargs["body"]["language"], "javascript")

    def test_the_language_is_lowercased(self):
        from unittest import mock

        payload = {"success": True, "stdout": "", "stderr": "", "outputFiles": {}}
        with mock.patch.object(g, "_stream_post", return_value=self._resp(payload)) as sp:
            g.run_code("echo x", language="BASH", cookies={"sso": "x"})
        self.assertEqual(sp.call_args.kwargs["body"]["language"], "bash")

    def test_a_failure_keeps_the_stderr(self):
        from unittest import mock

        payload = {"success": False, "stdout": "",
                   "stderr": "SyntaxError: Unexpected identifier", "outputFiles": {}}
        with mock.patch.object(g, "_stream_post", return_value=self._resp(payload)):
            out = g.run_code("echo ok", language="javascript", cookies={"sso": "x"})
        self.assertFalse(out["success"])
        self.assertIn("SyntaxError", out["stderr"])

    def test_the_sandbox_facts_are_recorded(self):
        self.assertEqual(g.RUN_CODE_SANDBOX["python"], "3.12.3")
        self.assertIn("gvisor", g.RUN_CODE_SANDBOX["platform"])
        self.assertEqual(g.RUN_CODE_SANDBOX["uid"], 0)
        self.assertTrue(g.RUN_CODE_SANDBOX["dns"])
        self.assertFalse(g.RUN_CODE_SANDBOX["tcp_egress"])
        self.assertIn("numpy", g.RUN_CODE_SANDBOX["libraries"])
        self.assertIn("torch", g.RUN_CODE_SANDBOX["libraries"])

    def test_it_requires_authentication(self):
        with self.assertRaises(g.GrokWebError):
            g.run_code("print(1)")


class ImageGenerationsTest(unittest.TestCase):
    """`GET /rest/app-chat/image-generations` —— 生图历史。

    **实测：生成图片后仍为 `[]`** —— 不能用来计数生图用量。
    """

    def _resp(self, payload, status=200):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = status
        resp.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        resp.json.return_value = payload
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return resp

    def test_an_empty_history_is_a_list(self):
        from unittest import mock

        with mock.patch.object(g, "_stream_get",
                               return_value=self._resp({"generations": []})):
            self.assertEqual(g.image_generations(cookies={"sso": "x"}), [])

    def test_a_populated_history_is_passed_through(self):
        from unittest import mock

        gens = [{"id": "g1", "imageUrl": "a/b.jpg"}]
        with mock.patch.object(g, "_stream_get",
                               return_value=self._resp({"generations": gens})):
            self.assertEqual(g.image_generations(cookies={"sso": "x"}), gens)

    def test_a_missing_field_yields_an_empty_list(self):
        from unittest import mock

        with mock.patch.object(g, "_stream_get", return_value=self._resp({})):
            self.assertEqual(g.image_generations(cookies={"sso": "x"}), [])

    def test_a_403_is_surfaced(self):
        from unittest import mock

        with mock.patch.object(g, "_stream_get", return_value=self._resp(
                {"code": 7, "message": "oauth2-auth-forbidden"}, 403)):
            with self.assertRaises(g.GrokWebError) as ctx:
                g.image_generations(cookies={"sso": "x"})
        self.assertEqual(ctx.exception.status, 403)


class MediaGenVariantsTest(unittest.TestCase):
    """`mediaGenInput` 是六选一的 oneof（bundle `d()` 提取）。

        textToImage / imageToImage / textToVideo
        imageToVideo / referenceToVideo / videoExtension

    直连模型名（`0oottrppfxt08.js` 的构造分支）：
        imagine-image-gen   + imageGenModelConfig  {aspectRatio, mode, resolutionName?}
        imagine-image-edit  + imageEditModelConfig {imageReferences[], aspectRatio?, selectionRegions?}
        imagine-video-gen   + textToVideo
    """

    def test_the_six_variants_match_the_bundle(self):
        self.assertEqual(g.MEDIA_GEN_VARIANTS,
                         ("textToImage", "imageToImage", "textToVideo",
                          "imageToVideo", "referenceToVideo", "videoExtension"))

    def test_kind_detection_follows_the_bundle_order(self):
        self.assertEqual(g.media_gen_kind({"textToImage": {}}), "textToImage")
        self.assertEqual(g.media_gen_kind({"imageToImage": {}}), "imageToImage")
        self.assertEqual(g.media_gen_kind({"textToVideo": {}}), "textToVideo")
        self.assertEqual(g.media_gen_kind({"imageToVideo": {}}), "imageToVideo")
        self.assertEqual(g.media_gen_kind({"referenceToVideo": {}}), "referenceToVideo")
        self.assertEqual(g.media_gen_kind({"videoExtension": {}}), "videoExtension")

    def test_kind_detection_is_first_match(self):
        """同时出现多个键时按固定顺序取第一个。"""
        both = {"videoExtension": {}, "textToImage": {}}
        self.assertEqual(g.media_gen_kind(both), "textToImage")

    def test_kind_detection_rejects_empty_and_non_dict(self):
        for value in ({}, None, [], "x", {"unknown": {}}):
            self.assertIsNone(g.media_gen_kind(value))

    def test_the_proven_video_model_name(self):
        """`imagine-video-gen` 实测 200；`imagine-video` 是错名（403 Model is not found）。"""
        self.assertEqual(g.VIDEO_GEN_MODEL, "imagine-video-gen")

    def test_the_image_model_names_are_recorded(self):
        self.assertEqual(g.IMAGE_GEN_MODEL, "imagine-image-gen")
        self.assertEqual(g.IMAGE_EDIT_MODEL, "imagine-image-edit")

    def test_video_resolutions(self):
        self.assertEqual(g.VIDEO_RESOLUTIONS, ("480p", "720p", "1080p"))

    def test_build_text_to_video_shape(self):
        out = g.build_text_to_video("a cat", aspect_ratio="16:9", duration=6,
                                    resolution="480p")
        self.assertEqual(out["textToVideo"], {
            "prompt": "a cat", "aspectRatio": "16:9",
            "duration": 6, "resolutionName": "480p"})

    def test_build_text_to_video_omits_optional_fields(self):
        out = g.build_text_to_video("a cat", aspect_ratio="", duration=0,
                                    resolution="")
        self.assertEqual(out["textToVideo"], {"prompt": "a cat"})

    def test_build_text_to_video_skip_audio(self):
        out = g.build_text_to_video("a cat", skip_audio=True)
        self.assertTrue(out["textToVideo"]["skipAudio"])

    def test_image_gen_config_quality_2k(self):
        cfg = g.build_image_gen_config(aspect_ratio="2:3", mode="quality",
                                       resolution="2k")
        self.assertEqual(cfg, {"aspectRatio": "2:3", "mode": "quality",
                               "resolutionName": "2k"})

    def test_image_gen_config_fast_drops_resolution(self):
        """`resolutionName` 只在 quality 档下发。"""
        cfg = g.build_image_gen_config(mode="fast", resolution="2k")
        self.assertNotIn("resolutionName", cfg)

    def test_image_edit_config_requires_references(self):
        with self.assertRaises(g.GrokWebError):
            g.build_image_edit_config([])

    def test_image_edit_config_single_image_keeps_aspect_ratio(self):
        cfg = g.build_image_edit_config(["a/1.jpg"], aspect_ratio="1:1")
        self.assertEqual(cfg["imageReferences"], ["a/1.jpg"])
        self.assertEqual(cfg["aspectRatio"], "1:1")

    def test_image_edit_config_multi_image_drops_aspect_ratio(self):
        """多图时 bundle 不发 `aspectRatio`。"""
        cfg = g.build_image_edit_config(["a/1.jpg", "a/2.jpg"], aspect_ratio="1:1")
        self.assertNotIn("aspectRatio", cfg)
        self.assertEqual(len(cfg["imageReferences"]), 2)

    def test_image_edit_config_selection_regions(self):
        regions = [{"x": 0, "y": 0, "w": 10, "h": 10}]
        cfg = g.build_image_edit_config(["a/1.jpg"], selection_regions=regions)
        self.assertEqual(cfg["selectionRegions"], regions)


class VideoBodyTest(unittest.TestCase):
    """`build_body(video=True)` —— 走 `imagine-video-gen` + `textToVideo`。"""

    def test_video_body_uses_the_proven_model(self):
        body = g.build_body("a cat walking", video=True, aspect_ratio="16:9",
                            video_duration=6, video_resolution="480p")
        self.assertEqual(body["modelName"], "imagine-video-gen")
        self.assertEqual(body["kind"], g.KIND_IMAGINE)
        self.assertTrue(body["enableImageGeneration"])
        self.assertEqual(body["mediaGenInput"]["textToVideo"]["duration"], 6)

    def test_an_explicit_model_overrides_the_video_default(self):
        body = g.build_body("a cat", video=True, model="some-model")
        self.assertEqual(body["modelName"], "some-model")

    def test_media_gen_overrides_both_image_and_video(self):
        """`media_gen` 优先级最高。"""
        body = g.build_body("x", image=True, video=True, model=g.IMAGE_EDIT_MODEL,
                            media_gen={"imageToImage": {"inputAssets": ["a1"]}})
        self.assertEqual(body["mediaGenInput"], {"imageToImage": {"inputAssets": ["a1"]}})
        self.assertEqual(body["modelName"], g.IMAGE_EDIT_MODEL)

    def test_an_invalid_media_gen_raises(self):
        with self.assertRaises(ValueError):
            g.build_body("x", media_gen={"bogus": {}})

    def test_the_image_path_is_unchanged(self):
        """回归：`image=True` 仍走 `textToImage` + `imageEditModel: imagine`。"""
        body = g.build_body("a cat", image=True, model=g.IMAGE_MODEL,
                            image_count=1)
        self.assertEqual(body["modelName"], "build")
        self.assertEqual(body["mediaGenInput"]["textToImage"]["numOfImages"], 1)
        self.assertEqual(
            body["responseMetadata"]["modelConfigOverride"]["modelMap"]["imageEditModel"],
            "imagine")

    def test_media_gen_adds_the_imagine_override_for_image_kinds(self):
        body = g.build_body("x", media_gen={"textToImage": {"prompt": "x"}})
        self.assertEqual(
            body["responseMetadata"]["modelConfigOverride"]["modelMap"]["imageEditModel"],
            "imagine")

    def test_media_gen_does_not_add_the_override_for_video_kinds(self):
        body = g.build_body("x", media_gen={"textToVideo": {"prompt": "x"}})
        self.assertNotIn("responseMetadata", body)


class GenerateVideoTest(unittest.TestCase):
    """`generate_video()` —— 实测 `imagine-video-gen` 返回 200 + video id。

    视频是**异步任务**：本次只拿到 id，未确认轮询端点
    （`/rest/media/video/*` 只有 `music`/`caption`/`upscale`/`video-sequence`）。
    **图片与视频共享同一个速率限制桶。**
    """

    def _stream(self, frames):
        from unittest import mock

        lines = [json.dumps(f).encode() for f in frames]
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.text = "\n".join(json.dumps(f) for f in frames)
        resp.iter_lines.return_value = lines
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return mock.patch.object(g, "_stream_post", return_value=resp)

    def test_a_video_id_is_collected(self):
        frames = [
            {"result": {"response": {
                "streamingVideoGenerationResponse": {"videoId": "d1b9572a"}}}},
        ]
        with self._stream(frames):
            out = g.generate_video("a cat walking", bearer="tok")
        self.assertEqual(out["video_ids"], ["d1b9572a"])
        self.assertEqual(out["errors"], [])

    def test_ids_are_deduped(self):
        frames = [
            {"result": {"response": {
                "streamingVideoGenerationResponse": {"videoId": "v1"}}}},
            {"result": {"response": {
                "streamingVideoGenerationResponse": {"videoId": "v1"}}}},
        ]
        with self._stream(frames):
            out = g.generate_video("x", bearer="tok")
        self.assertEqual(out["video_ids"], ["v1"])

    def test_the_request_uses_the_proven_model(self):
        from unittest import mock

        with self._stream([]) as sp:
            g.generate_video("a cat", bearer="tok")
        body = sp.call_args.kwargs["body"]
        self.assertEqual(body["modelName"], "imagine-video-gen")
        self.assertIn("textToVideo", body["mediaGenInput"])

    def test_skip_audio_is_passed_through(self):
        from unittest import mock

        with self._stream([]) as sp:
            g.generate_video("a cat", bearer="tok", skip_audio=True)
        body = sp.call_args.kwargs["body"]
        self.assertTrue(body["mediaGenInput"]["textToVideo"]["skipAudio"])

    def test_a_rate_limit_is_reported(self):
        frames = [
            {"result": {"response": {"error": {
                "message": "You've hit your image rate limit.",
                "renderToolRateLimited": {"rateLimitedCount": 1}}}}},
        ]
        with self._stream(frames):
            out = g.generate_video("x", bearer="tok")
        self.assertTrue(out["errors"])
        self.assertEqual(out["video_ids"], [])

    def test_video_id_extraction_from_card_attachment(self):
        payload = {"cardAttachment": {"jsonData": json.dumps(
            {"video_chunk": {"videoUuid": "u1", "videoUrl": "a/v.mp4"}})}}
        self.assertEqual(g._extract_video_ids(payload), ["u1", "a/v.mp4"])

    def test_video_id_extraction_tolerates_garbage(self):
        for payload in ({}, {"cardAttachment": {"jsonData": "{bad"}},
                        {"cardAttachment": {"jsonData": None}},
                        {"streamingVideoGenerationResponse": None}):
            self.assertEqual(g._extract_video_ids(payload), [])

    def test_video_variant_fields_are_documented(self):
        self.assertEqual(
            g.VIDEO_VARIANT_FIELDS["textToVideo"],
            ("prompt", "aspectRatio", "duration", "resolutionName",
             "modelName", "skipAudio"))
        self.assertIn("useFirstFrame", g.VIDEO_VARIANT_FIELDS["imageToVideo"])
        self.assertIn("firstFrameAsset",
                      g.VIDEO_VARIANT_FIELDS["referenceToVideo"])


class UploadFileTest(unittest.TestCase):
    """`POST /rest/app-chat/upload-file` —— 图生图的前置步骤。

    **请求体是 JSON，`content` 是 base64**（bundle `chatUploadFileRaw`）：

        {fileName, fileMimeType, content, makePublic, fileSource, ...}

    实测用 multipart（`files=`）会 **400** —— 服务端读不到 body。
    """

    def _resp(self, payload, status=200):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = status
        resp.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        resp.json.return_value = payload
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return resp

    def test_bytes_are_base64_encoded_into_a_json_body(self):
        import base64
        from unittest import mock

        payload = {"fileMetadataId": "m1", "fileUri": "users/u/src.jpg"}
        with mock.patch.object(g, "_stream_post", return_value=self._resp(payload)) as sp:
            out = g.upload_file(b"\xff\xd8\xffJPEGDATA", file_name="a.jpg",
                                mime_type="image/jpeg", cookies={"sso": "x"})
        body = sp.call_args.kwargs["body"]
        self.assertEqual(body["fileName"], "a.jpg")
        self.assertEqual(body["fileMimeType"], "image/jpeg")
        self.assertEqual(base64.b64decode(body["content"]), b"\xff\xd8\xffJPEGDATA")
        self.assertFalse(body["makePublic"])
        self.assertEqual(out["fileUri"], "users/u/src.jpg")

    def test_str_content_is_encoded_as_utf8_then_base64(self):
        import base64
        from unittest import mock

        with mock.patch.object(g, "_stream_post",
                               return_value=self._resp({})) as sp:
            g.upload_file("hello", cookies={"sso": "x"})
        body = sp.call_args.kwargs["body"]
        self.assertEqual(base64.b64decode(body["content"]), b"hello")

    def test_make_public_is_passed_through(self):
        from unittest import mock

        with mock.patch.object(g, "_stream_post",
                               return_value=self._resp({})) as sp:
            g.upload_file(b"x", make_public=True, cookies={"sso": "x"})
        self.assertTrue(sp.call_args.kwargs["body"]["makePublic"])

    def test_empty_content_raises_before_any_request(self):
        from unittest import mock

        with mock.patch.object(g, "_stream_post") as sp:
            with self.assertRaises(g.GrokWebError):
                g.upload_file(b"", cookies={"sso": "x"})
            sp.assert_not_called()

    def test_it_requires_authentication(self):
        with self.assertRaises(g.GrokWebError):
            g.upload_file(b"x")

    def test_a_400_is_surfaced(self):
        from unittest import mock

        with mock.patch.object(g, "_stream_post",
                               return_value=self._resp({"code": 3}, 400)):
            with self.assertRaises(g.GrokWebError) as ctx:
                g.upload_file(b"x", cookies={"sso": "x"})
        self.assertEqual(ctx.exception.status, 400)

    def test_the_field_list_matches_the_bundle(self):
        self.assertEqual(g.UPLOAD_FILE_FIELDS,
                         ("fileName", "fileMimeType", "content", "makePublic",
                          "fileSource", "thirdPartyFileId", "workspaceId"))
        self.assertEqual(g.UPLOAD_FILE_PATH, "/rest/app-chat/upload-file")


class MediaLimitDiagnosisTest(unittest.TestCase):
    """媒体预算诊断（结论已修正 2026-10-21 晚）。

    曾一度认为「媒体配额按出口 IP 计量」，该结论**已推翻** —— 证据被请求体
    缺陷污染：精简 body（缺 `disableSearch`/`isReasoning`/`modelMode` 与
    `x-xai-request-id` 头）会得到 200 + 无图 + 正文声称已生成。

    对照实验（同一账号同一 prompt）：

        build_body 完整 body   → 出图 ✅
        精简 body              → 200 + 无图 ❌

    所以 `silent_degrade` 有两个成因：**请求体不完整**（调用方 bug）与
    **真配额耗尽**（完整 body 连发约 4–8 次后）。
    """

    def test_all_success_means_budget_remains(self):
        out = g.diagnose_media_limit([
            {"images": ["a"]}, {"images": ["b"]}])
        self.assertEqual(out["verdict"], "ok")
        self.assertEqual(out["ok_count"], 2)
        self.assertEqual(out["limited_count"], 0)

    def test_all_failure_means_quota_exhausted(self):
        """注意：若调用方用了精简 body，这里会误判 —— 见类文档。"""
        out = g.diagnose_media_limit([
            {"images": [], "rate_limited": ["hit limit"]},
            {"images": [], "silent_degrade": True},
            {"images": [], "failed": [{"system_err_code": 1006}]},
        ])
        self.assertEqual(out["verdict"], "quota_exhausted")
        self.assertEqual(out["ok_count"], 0)
        self.assertEqual(out["limited_count"], 3)
        self.assertIn("build_body", out["reason"])

    def test_mixed_means_account_level(self):
        """成功的那批证明预算存在，失败是账号差异。"""
        out = g.diagnose_media_limit([
            {"images": ["a"]},
            {"images": [], "silent_degrade": True},
        ])
        self.assertEqual(out["verdict"], "account_limited")
        self.assertEqual(out["ok_count"], 1)
        self.assertEqual(out["limited_count"], 1)

    def test_no_results_is_unknown(self):
        for value in ([], [None, "x", {}]):
            self.assertEqual(g.diagnose_media_limit(value)["verdict"], "unknown")

    def test_the_verdict_table_is_complete(self):
        self.assertEqual(set(g.MEDIA_LIMIT_VERDICTS),
                         {"ok", "quota_exhausted", "account_limited", "unknown"})

    def test_a_result_with_images_but_also_errors_counts_as_success(self):
        """只要出图就算成功 —— 部分失败帧不改变判定。"""
        out = g.diagnose_media_limit([
            {"images": ["a"], "rate_limited": ["x"],
             "failed": [{"system_err_code": 1006}]},
        ])
        self.assertEqual(out["ok_count"], 1)
        self.assertEqual(out["verdict"], "ok")


class SilentDegradeCausesTest(unittest.TestCase):
    """`silent_degrade` 有**两个完全不同**的成因（2026-10-21 对照实验）。

    同一账号、同一 prompt，只改请求体：

        build_body 完整 body                        → 出图 ✅
        精简 body（缺 disableSearch / isReasoning /
                   modelMode，缺 x-xai-request-id）  → 200 + 无图，
                                                        正文却说
                                                        "I generated an image ..."

    所以「静默降级」不能直接当成配额耗尽 —— **先确认请求体是完整的**。
    """

    def test_the_full_body_contains_the_fields_the_probe_omitted(self):
        body = g.build_body("x", image=True, model=g.IMAGE_GEN_MODEL)
        for key in ("disableSearch", "isReasoning", "modelMode", "deviceEnvInfo"):
            self.assertIn(key, body, f"完整 body 必须含 {key}")
        self.assertIs(body["disableSearch"], False)
        self.assertIs(body["isReasoning"], False)
        self.assertEqual(body["modelMode"], "none")

    def test_the_headers_carry_a_request_id(self):
        """`x-xai-request-id` 是完整请求的一部分，精简探针漏了它。"""
        headers = g.build_headers(bearer="tok")
        self.assertIn("x-xai-request-id", headers)
        self.assertTrue(headers["x-xai-request-id"])

    def test_generate_image_passes_the_gen_config_through(self):
        from unittest import mock

        with mock.patch.object(g, "_stream_post") as sp:
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.text = ""
            resp.iter_lines.return_value = []
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda *a: None
            sp.return_value = resp
            g.generate_image("x", bearer="tok", model=g.IMAGE_GEN_MODEL,
                             image_gen_config=g.build_image_gen_config(mode="fast"))
        body = sp.call_args.kwargs["body"]
        self.assertEqual(body["modelName"], g.IMAGE_GEN_MODEL)
        self.assertEqual(body["imageGenModelConfig"]["mode"], "fast")

    def test_generate_image_passes_the_edit_config_through(self):
        from unittest import mock

        with mock.patch.object(g, "_stream_post") as sp:
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.text = ""
            resp.iter_lines.return_value = []
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda *a: None
            sp.return_value = resp
            g.generate_image("x", bearer="tok", model=g.IMAGE_EDIT_MODEL,
                             image_edit_config=g.build_image_edit_config(["a/1.jpg"]))
        body = sp.call_args.kwargs["body"]
        self.assertEqual(body["modelName"], g.IMAGE_EDIT_MODEL)
        self.assertEqual(body["imageEditModelConfig"]["imageReferences"], ["a/1.jpg"])


class ModelCatalogTest(unittest.TestCase):
    """`POST /rest/models` —— 模型目录（**必须 POST**，GET 返回 501）。

    单条 schema：`{modelId, name, description, modeDescription, modeName,
    badgeText, tags[], modelMode, promptingBackend}`。
    顶层另有 `defaultAnonModel` / `defaultFreeModel`（`grok-3`）/
    `defaultHeavyModel` / `defaultProModel`（`grok-4`）/ `unavailableModels`。

    **`imagine-image-gen` / `imagine-image-edit` / `imagine-video-gen` 不在
    目录里** —— 它们是前端 image studio 直连的隐藏模型名。
    """

    def _resp(self, payload, status=200):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = status
        resp.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        resp.json.return_value = payload
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return resp

    def test_it_posts_not_gets(self):
        from unittest import mock

        with mock.patch.object(g, "_stream_post",
                               return_value=self._resp({"models": []})) as sp:
            g.list_models(bearer="tok")
        self.assertIn("/rest/models", sp.call_args[0][0])
        self.assertEqual(sp.call_args.kwargs["body"], {})

    def test_model_ids_extracts_every_entry(self):
        catalog = {"models": [
            {"modelId": "build"}, {"modelId": "grok-3"}, {"modelId": "grok-4"},
            {"name": "no-id"}, "garbage",
        ]}
        self.assertEqual(g.model_ids(catalog), ["build", "grok-3", "grok-4"])

    def test_model_ids_tolerates_bad_shapes(self):
        for value in ({}, {"models": None}, {"models": "x"}, None, []):
            self.assertEqual(g.model_ids(value), [])

    def test_the_catalog_media_models_match_the_probe(self):
        self.assertEqual(g.CATALOG_MEDIA_MODELS,
                         ("build", "imagine-agent-mode", "imagine-agent-mode-dev",
                          "imagine-agent-mode-grok-4-5",
                          "imagine-video-prompt-enhance"))

    def test_the_hidden_models_are_not_in_the_catalog_list(self):
        """目录里没有直连模型名 —— 它们是前端隐藏使用的。"""
        for name in (g.IMAGE_GEN_MODEL, g.IMAGE_EDIT_MODEL, g.VIDEO_GEN_MODEL):
            self.assertNotIn(name, g.CATALOG_MEDIA_MODELS)

    def test_the_model_field_list_matches_the_schema(self):
        self.assertEqual(g.MODEL_FIELDS,
                         ("modelId", "name", "description", "modeDescription",
                          "modeName", "badgeText", "tags", "modelMode",
                          "promptingBackend"))
        self.assertEqual(g.MODELS_PATH, "/rest/models")

    def test_a_non_200_is_surfaced(self):
        from unittest import mock

        with mock.patch.object(g, "_stream_post",
                               return_value=self._resp({"code": 12}, 501)):
            with self.assertRaises(g.GrokWebError) as ctx:
                g.list_models(bearer="tok")
        self.assertEqual(ctx.exception.status, 501)


class EnhancePromptTest(unittest.TestCase):
    """`imagine-video-prompt-enhance` —— **纯文本模型，不消耗媒体配额**。

    实测：所有生图模型都 429 时它仍返回 200，把
    `"a cat walking in rain"` 扩写成一段详细的视频描述。
    """

    def test_it_uses_the_enhancer_model(self):
        from unittest import mock

        frames = [{"result": {"response": {
            "messageTag": "final", "token": "A cat walks steadily..."}}}]
        lines = [json.dumps(f).encode() for f in frames]
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.text = "\n".join(json.dumps(f) for f in frames)
        resp.iter_lines.return_value = lines
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with mock.patch.object(g, "_stream_post", return_value=resp) as sp:
            out = g.enhance_prompt("a cat walking in rain", bearer="tok")
        body = sp.call_args.kwargs["body"]
        self.assertEqual(body["modelName"], g.PROMPT_ENHANCE_MODEL)
        self.assertIn("A cat walks", out)

    def test_it_is_not_a_media_model(self):
        """它不走 `kind: CONVERSATION_KIND_IMAGINE`。"""
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.text = ""
        resp.iter_lines.return_value = []
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with mock.patch.object(g, "_stream_post", return_value=resp) as sp:
            g.enhance_prompt("x", bearer="tok")
        body = sp.call_args.kwargs["body"]
        self.assertNotIn("mediaGenInput", body)
        self.assertNotEqual(body.get("kind"), g.KIND_IMAGINE)

    def test_the_model_name_is_the_probed_one(self):
        self.assertEqual(g.PROMPT_ENHANCE_MODEL, "imagine-video-prompt-enhance")


class TextToSpeechTest(unittest.TestCase):
    """`POST /rest/app-chat/text-to-speech` —— 语音合成（实测 2026-10-21）。

    响应是 **NDJSON**：每行 `{"result":{"data":"<base64>"}}`，逐块拼接后
    base64 解码得到完整音频。实测产物经 ffprobe 确认：

        codec=mp3  sample_rate=24000  channels=1(mono)
        bit_rate=128kbps  duration=2.616s  41856 字节  5 个分块

    请求体（`chatTextToSpeechRaw`）：
        {text, voiceId, language, codec, sampleRate, bitRate,
         optimizeStreamingLatency, textNormalization, speed}
    """

    def _resp(self, lines, status=200):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = status
        resp.text = "\n".join(lines)
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return resp

    def test_base64_chunks_are_concatenated_and_decoded(self):
        import base64
        from unittest import mock

        part1 = base64.b64encode(b"\xff\xf3\xc4\xc4" + b"A" * 100).decode()
        part2 = base64.b64encode(b"B" * 100).decode()
        lines = [json.dumps({"result": {"data": part1}}),
                 json.dumps({"result": {"data": part2}})]
        with mock.patch.object(g, "_stream_post",
                               return_value=self._resp(lines)) as sp:
            out = g.text_to_speech("hi", voice_id="altair", bearer="tok")
        self.assertEqual(out["chunks"], 2)
        self.assertEqual(out["bytes"], 204)
        self.assertEqual(out["format"], "mp3")
        self.assertTrue(out["audio"].startswith(b"\xff\xf3"))
        body = sp.call_args.kwargs["body"]
        self.assertEqual(body["text"], "hi")
        self.assertEqual(body["voiceId"], "altair")

    def test_optional_fields_are_only_sent_when_set(self):
        from unittest import mock

        lines = [json.dumps({"result": {"data": ""}})]
        with mock.patch.object(g, "_stream_post",
                               return_value=self._resp(lines)) as sp:
            g.text_to_speech("hi", bearer="tok")
        body = sp.call_args.kwargs["body"]
        self.assertNotIn("language", body)
        self.assertNotIn("codec", body)

    def test_optional_fields_are_passed_through(self):
        from unittest import mock

        lines = [json.dumps({"result": {"data": ""}})]
        with mock.patch.object(g, "_stream_post",
                               return_value=self._resp(lines)) as sp:
            g.text_to_speech("hi", language="en", codec="mp3", sample_rate=24000,
                             speed=1.1, bearer="tok")
        body = sp.call_args.kwargs["body"]
        self.assertEqual(body["language"], "en")
        self.assertEqual(body["codec"], "mp3")
        self.assertEqual(body["sampleRate"], 24000)
        self.assertEqual(body["speed"], 1.1)

    def test_empty_text_is_rejected_before_any_request(self):
        from unittest import mock

        with mock.patch.object(g, "_stream_post") as sp:
            for text in ("", "   ", None):
                with self.assertRaises(g.GrokWebError):
                    g.text_to_speech(text, bearer="tok")
            sp.assert_not_called()

    def test_a_non_ndjson_line_is_skipped(self):
        import base64
        from unittest import mock

        good = base64.b64encode(b"\xff\xf3data").decode()
        lines = ["not json", json.dumps({"result": {"data": good}}), ""]
        with mock.patch.object(g, "_stream_post",
                               return_value=self._resp(lines)):
            out = g.text_to_speech("hi", bearer="tok")
        self.assertEqual(out["chunks"], 1)
        self.assertEqual(out["bytes"], 6)

    def test_audio_format_sniffing(self):
        self.assertEqual(g._audio_format(b"RIFF...."), "wav")
        self.assertEqual(g._audio_format(b"OggS...."), "ogg")
        self.assertEqual(g._audio_format(b"ID3\x04"), "mp3")
        self.assertEqual(g._audio_format(b"\xff\xf3\xc4\xc4"), "mp3")
        self.assertEqual(g._audio_format(b"\x00\x00\x00\x20ftyp"), "m4a")
        self.assertEqual(g._audio_format(b""), "empty")
        self.assertEqual(g._audio_format(b"junk"), "unknown")

    def test_the_field_list_matches_the_bundle(self):
        self.assertEqual(g.TTS_FIELDS,
                         ("text", "voiceId", "language", "codec", "sampleRate",
                          "bitRate", "optimizeStreamingLatency",
                          "textNormalization", "speed"))
        self.assertEqual(g.TTS_PATH, "/rest/app-chat/text-to-speech")

    def test_tts_voices_returns_the_list(self):
        from unittest import mock

        voices = [{"voiceId": "altair", "name": "Altair",
                   "language": "multilingual", "gender": "male"}]
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"voices": voices}
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with mock.patch.object(g, "_stream_get", return_value=resp) as sg:
            out = g.tts_voices(cookies={"sso": "x"})
        self.assertEqual(out, voices)
        self.assertIn("/tts-voices", sg.call_args[0][0])

    def test_tts_voices_tolerates_a_missing_field(self):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = {}
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with mock.patch.object(g, "_stream_get", return_value=resp):
            self.assertEqual(g.tts_voices(cookies={"sso": "x"}), [])


class TemplateLibraryTest(unittest.TestCase):
    """Imagine 模板库（实测 2026-10-21）。

        POST /rest/media/pipeline/template/list         → {templates[], summaries[]}
        POST /rest/media/pipeline/template-folder/list  → {folders[]}
        POST /rest/media/pipeline/template/get          → {summary, isSaved}

    **20 个模板全部 `creditCost: 0`（免费）**，16 个 image + 4 个 video。

    `summary` 字段：`{id, name, description, thumbnailUrl, inputs[],
    publishedVersion, userId, runCount, creditCost, createdAt,
    primaryOutputMediaType, folders[]}`；`inputs[]` 是 typed：
    `{name, inputType: "image"|"text", label, required}`。

    **`template/get` 只返回 `summary`，不含提示词**（服务端持有）；
    **`POST /rest/media/pipeline/run` 实测 403** —— 能列能读，不能用 API 跑。
    """

    def _resp(self, payload, status=200):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = status
        resp.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        resp.json.return_value = payload
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return resp

    def test_templates_returns_the_summaries(self):
        from unittest import mock

        payload = {"templates": [], "summaries": [
            {"id": "t1", "name": "Chibi", "creditCost": 0,
             "primaryOutputMediaType": "image"},
            {"id": "t2", "name": "Funky Dance", "creditCost": 0,
             "primaryOutputMediaType": "video"},
        ]}
        with mock.patch.object(g, "_stream_post",
                               return_value=self._resp(payload)) as sp:
            out = g.templates(bearer="tok")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["name"], "Chibi")
        self.assertIn("/template/list", sp.call_args[0][0])

    def test_templates_tolerates_a_missing_field(self):
        from unittest import mock

        with mock.patch.object(g, "_stream_post",
                               return_value=self._resp({})):
            self.assertEqual(g.templates(bearer="tok"), [])

    def test_folders_are_returned(self):
        from unittest import mock

        payload = {"folders": [{"slug": "functional", "name": "Image Editing",
                                "sortOrder": 0}]}
        with mock.patch.object(g, "_stream_post",
                               return_value=self._resp(payload)) as sp:
            out = g.template_folders(bearer="tok")
        self.assertEqual(out[0]["slug"], "functional")
        self.assertIn("/template-folder/list", sp.call_args[0][0])

    def test_detail_sends_template_id_camel_case(self):
        from unittest import mock

        payload = {"summary": {"id": "t1", "name": "Chibi"}, "isSaved": False}
        with mock.patch.object(g, "_stream_post",
                               return_value=self._resp(payload)) as sp:
            out = g.template_detail("t1", bearer="tok")
        self.assertEqual(out["name"], "Chibi")
        self.assertEqual(sp.call_args.kwargs["body"], {"templateId": "t1"})

    def test_detail_rejects_an_empty_id_before_any_request(self):
        from unittest import mock

        with mock.patch.object(g, "_stream_post") as sp:
            for tid in ("", "   ", None):
                with self.assertRaises(g.GrokWebError):
                    g.template_detail(tid, bearer="tok")
            sp.assert_not_called()

    def test_the_catalog_has_twenty_templates_all_free(self):
        self.assertEqual(len(g.CATALOG_TEMPLATES), 20)
        names = [n for n, _ in g.CATALOG_TEMPLATES]
        self.assertIn("Quality Enhancer", names)
        self.assertIn("Chibi", names)
        videos = [n for n, k in g.CATALOG_TEMPLATES if k == "video"]
        self.assertEqual(len(videos), 4)
        self.assertIn("Funky Dance", videos)

    def test_the_field_list_matches_the_summary_schema(self):
        self.assertEqual(g.TEMPLATE_FIELDS,
                         ("id", "name", "description", "thumbnailUrl", "inputs",
                          "publishedVersion", "userId", "runCount", "creditCost",
                          "createdAt", "primaryOutputMediaType", "folders"))

    def test_pipeline_run_is_documented_as_forbidden(self):
        """`pipeline/run` 实测 403 —— 常量存在但只作记录。"""
        self.assertEqual(g.PIPELINE_RUN_PATH, "/rest/media/pipeline/run")


class RefreshAccessTokenTest(unittest.TestCase):
    """`POST https://auth.x.ai/oauth2/token` + `grant_type=refresh_token`。

    实测（2026-10-21）：可用，返回新 `access_token`（`expires_in=21600` = 6h）。

    **失效的 refresh token 返回 HTTP 400 + `invalid_grant`**
    （`Invalid or unknown refresh token`）—— 永久性错误，重试无用。

    **刷新不重置媒体配额** —— 实测刷新后仍受同一预算约束。
    """

    def _resp(self, payload, status=200):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = status
        resp.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        resp.json.return_value = payload
        return resp

    def _session_mock(self, resp):
        from unittest import mock

        session = mock.MagicMock()
        session.post.return_value = resp
        return mock.patch.object(g, "_session", return_value=session), session

    def test_a_successful_refresh_returns_the_token(self):
        payload = {"access_token": "new-at", "refresh_token": "new-rt",
                   "expires_in": 21600, "token_type": "Bearer"}
        patcher, session = self._session_mock(self._resp(payload))
        with patcher:
            out = g.refresh_access_token("old-rt")
        self.assertEqual(out["access_token"], "new-at")
        self.assertEqual(out["expires_in"], 21600)
        form = session.post.call_args.kwargs["data"]
        self.assertEqual(form["grant_type"], "refresh_token")
        self.assertEqual(form["refresh_token"], "old-rt")
        self.assertEqual(form["client_id"], g.OAUTH_CLIENT_ID)

    def test_invalid_grant_is_reported_as_permanent(self):
        payload = {"error": "invalid_grant",
                   "error_description": "Invalid or unknown refresh token"}
        patcher, _ = self._session_mock(self._resp(payload, 400))
        with patcher:
            with self.assertRaises(g.GrokWebError) as ctx:
                g.refresh_access_token("dead-rt")
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("invalid_grant", str(ctx.exception))

    def test_an_empty_refresh_token_is_rejected_before_any_request(self):
        from unittest import mock

        with mock.patch.object(g, "_session") as sess:
            for rt in ("", "   ", None):
                with self.assertRaises(g.GrokWebError):
                    g.refresh_access_token(rt)
            sess.assert_not_called()

    def test_a_response_without_access_token_raises(self):
        patcher, _ = self._session_mock(self._resp({"expires_in": 21600}))
        with patcher:
            with self.assertRaises(g.GrokWebError):
                g.refresh_access_token("rt")

    def test_the_oauth_constants_match_the_jwt(self):
        self.assertEqual(g.OAUTH_CLIENT_ID, "b1a00492-073a-47ea-816f-4c329264a828")
        self.assertEqual(g.OAUTH_TOKEN_URL, "https://auth.x.ai/oauth2/token")
        self.assertIn("offline_access", g.OAUTH_SCOPES)
        self.assertIn("grok-cli:access", g.OAUTH_SCOPES)
        self.assertIn("conversations:write", g.OAUTH_SCOPES)


class MediaQuotaInfoTest(unittest.TestCase):
    """`POST /rest/media/imagine/quota_info` —— **真正的配额端点**（2026-10-21 打通）。

    **必须用 SSO cookie**；CLI 签发的 Bearer 一律 403。

    实测响应::

        {"image": null,
         "imagePro": {"available": true, "remainingQueries": 4,
                      "windowSizeSeconds": 86400},
         "imageEdit": null, "video": null,
         "video720p": {"available": true, "remainingQueries": 1,
                       "windowSizeSeconds": 86400}}

    **窗口是 86400 秒 = 24 小时。** 这解释了为什么 3.5 小时后仍未恢复 ——
    bundle 里 `formatAvailableAt` 的「+30 分钟」是 credit-quota 的显示逻辑，
    不是 media quota。
    """

    def _resp(self, payload, status=200):
        from unittest import mock

        resp = mock.MagicMock()
        resp.status_code = status
        resp.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        resp.json.return_value = payload
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return resp

    def test_it_sends_the_sso_cookie(self):
        from unittest import mock

        payload = {"imagePro": {"available": True, "remainingQueries": 4,
                                "windowSizeSeconds": 86400}}
        with mock.patch.object(g, "_stream_post",
                               return_value=self._resp(payload)) as sp:
            g.quota_info(cookies={"sso": "abc"})
        self.assertIn("/media/imagine/quota_info", sp.call_args[0][0])
        self.assertEqual(sp.call_args.kwargs["cookies"], {"sso": "abc"})

    def test_missing_auth_is_rejected_before_any_request(self):
        from unittest import mock

        with mock.patch.object(g, "_stream_post") as sp:
            with self.assertRaises(g.GrokWebError):
                g.quota_info()
            sp.assert_not_called()

    def test_the_window_is_twenty_four_hours(self):
        quota = {"imagePro": {"available": True, "remainingQueries": 4,
                              "windowSizeSeconds": 86400}}
        out = g.remaining_media(quota)
        self.assertEqual(out["imagePro"]["windowHours"], 24.0)
        self.assertEqual(out["imagePro"]["remainingQueries"], 4)

    def test_null_buckets_are_dropped(self):
        quota = {"image": None, "imagePro": {"remainingQueries": 1,
                                             "windowSizeSeconds": 86400},
                 "imageEdit": None, "video": None, "video720p": None}
        out = g.remaining_media(quota)
        self.assertEqual(list(out), ["imagePro"])

    def test_all_five_buckets_are_recognised(self):
        self.assertEqual(g.MEDIA_QUOTA_BUCKETS,
                         ("image", "imagePro", "imageEdit", "video", "video720p"))
        quota = {b: {"remainingQueries": 1, "windowSizeSeconds": 3600}
                 for b in g.MEDIA_QUOTA_BUCKETS}
        self.assertEqual(len(g.remaining_media(quota)), 5)

    def test_remaining_media_tolerates_garbage(self):
        for value in ({}, None, [], "x"):
            self.assertEqual(g.remaining_media(value), {})

    def test_a_bad_window_value_does_not_raise(self):
        out = g.remaining_media({"imagePro": {"windowSizeSeconds": "bad"}})
        self.assertIsNone(out["imagePro"]["windowHours"])

    def test_the_endpoint_paths_match_the_bundle(self):
        self.assertEqual(g.QUOTA_INFO_PATH, "/rest/media/imagine/quota_info")
        self.assertEqual(g.GENERATION_ESTIMATE_PATH,
                         "/rest/media/imagine/generation_estimate")
        self.assertEqual(g.IMAGINE_OVERRIDES_PATH, "/rest/models/imagine/overrides")


class GenerationFailureModesTest(unittest.TestCase):
    """生图失败的**五种**形态（2026-10-22 实测）。

    判定成功**只能看** `cardAttachment.image_chunk.imageUrl`。
    HTTP 200、正文声称"已生成"、agent 说"这是结果" —— 全都不可信。

        ① 请求层 429        {"error":{"code":8,"message":"Too many requests"}}
        ② 流内限流          renderToolRateLimited + systemErrCode 1006
        ③ 请求体不完整      "I generated an image with the prompt: ..."
        ④ **agent 幻觉**    "Here's the apple recolored to ..."
        ⑤ 模型报错          "Oops, something broke. Talk to me later?"
    """

    HALLUCINATION = ("Here's the apple recolored to bright green, with the "
                     "original shape, size, lighting, texture, and white "
                     "background preserved:")

    def test_the_agent_hallucination_is_caught(self):
        """实测：170 帧流里 cardAttachment 出现 0 次，只有 modelResponse。"""
        self.assertTrue(g.claims_success_without_image(self.HALLUCINATION, []))
        self.assertTrue(g.claims_success_without_image(self.HALLUCINATION, None))

    def test_real_images_are_never_flagged(self):
        self.assertFalse(g.claims_success_without_image(
            self.HALLUCINATION, [{"image_url": "x"}]))

    def test_ordinary_text_is_not_flagged(self):
        for text in ("", "   ", "What would you like me to make green?",
                     "I cannot help with that.", None):
            self.assertFalse(g.claims_success_without_image(text, []))

    def test_the_broken_model_marker(self):
        self.assertTrue(g.model_broken("Oops, something broke. Talk to me later?"))
        self.assertFalse(g.model_broken("Here's the image"))

    def test_the_quota_exhausted_hint(self):
        self.assertTrue(g.quota_exhausted_hint(
            "It looks like you've hit the image generation limit for this session, "
            "so I can't create or edit images right now."))
        self.assertFalse(g.quota_exhausted_hint("a blue circle"))

    def test_the_marker_tables_are_populated(self):
        self.assertIn("here's the", g.AGENT_HALLUCINATION_MARKERS)
        self.assertIn("preserved:", g.AGENT_HALLUCINATION_MARKERS)
        self.assertEqual(g.MODEL_BROKEN_MARKER, "Oops, something broke")
        self.assertEqual(g.QUOTA_EXHAUSTED_MARKER, "image generation limit")


class StreamErrorTaxonomyTest(unittest.TestCase):
    """完整错误分类法（从 bundle 模块 962117 / 4150825 提取）。

    前端 `F(e)` 按**固定优先级**遍历流内错误对象，命中即返回。
    这是服务端错误语义的唯一权威来源 —— 比按状态码猜准确，因为配额耗尽时
    HTTP 仍是 200。
    """

    def test_the_priority_order_matches_the_bundle(self):
        names = [f for f, _ in g.STREAM_ERROR_KINDS]
        self.assertEqual(names[:6], ["beyondContextLength", "userPromptTooLong",
                                     "modelNotAvailable", "globalRateLimit",
                                     "concurrencyLimit", "systemKillSwitch"])
        self.assertEqual(len(g.STREAM_ERROR_KINDS), 20)

    def test_higher_priority_wins(self):
        """beyondContextLength 排在 usageLimitReached 前面。"""
        kind = g.stream_error_kind([
            {"usageLimitReached": {}}, {"beyondContextLength": {}}])
        self.assertEqual(kind, "beyondContextLength")

    def test_snake_case_variants_are_recognised(self):
        self.assertEqual(g.stream_error_kind([{"global_rate_limit": {}}]),
                         "globalRateLimit")
        self.assertEqual(g.stream_error_kind([{"usage_pool_exhausted": {}}]),
                         "usagePoolExhausted")
        self.assertEqual(g.stream_error_kind([{"usage_limit_reached": {}}]),
                         "usageLimitReached")

    def test_nested_error_objects_are_searched(self):
        self.assertEqual(
            g.stream_error_kind([{"error": {"renderToolRateLimited": {}}}]),
            "renderToolRateLimited")

    def test_the_context_limit_regex_fallback(self):
        for msg in ("conversation is too long",
                    "You have hit the context limit",
                    "context window exceeded"):
            self.assertEqual(g.stream_error_kind([{"message": msg}]),
                             "beyondContextLength", msg)

    def test_connector_auth_failure(self):
        self.assertEqual(g.stream_error_kind([{"message": "invalid_grant"}]),
                         "connectorAuthFailed")

    def test_clean_stream_returns_none(self):
        for frames in ([], [{"token": "hi"}], [{"message": "hello"}]):
            self.assertIsNone(g.stream_error_kind(frames))

    def test_only_beyond_context_length_is_fatal(self):
        self.assertEqual(g.FATAL_STREAM_KINDS, frozenset({"beyondContextLength"}))
        self.assertTrue(g.is_fatal_stream_error([{"beyondContextLength": {}}]))
        self.assertFalse(g.is_fatal_stream_error([{"usageLimitReached": {}}]))

    def test_the_fatal_severity_marker(self):
        self.assertTrue(g.is_fatal_stream_error(
            [{"severity": "STREAM_ERROR_SEVERITY_FATAL"}]))

    def test_the_two_quota_kinds_are_distinct(self):
        """pool_exhausted 是共享池耗尽，limit_reached 是个人上限。"""
        self.assertEqual(g.USAGE_EXHAUSTED_KINDS,
                         frozenset({"usagePoolExhausted", "usageLimitReached"}))
        self.assertNotEqual(g.stream_error_kind([{"usagePoolExhausted": {}}]),
                            g.stream_error_kind([{"usageLimitReached": {}}]))


class EmptyResponseTest(unittest.TestCase):
    """前端 `B(e)` —— **silent_degrade 的官方判据**。

        !text.trim() && cardAttachments.length === 0
          && cardAttachmentsJson.length === 0
          && !error && usageLimitKind == null
          && !concurrencyLimited && !rateLimited

    四个都空才算空响应。
    """

    def test_all_empty_is_an_empty_response(self):
        self.assertTrue(g.is_empty_response(text=""))
        self.assertTrue(g.is_empty_response(text="   "))

    def test_text_makes_it_non_empty(self):
        self.assertFalse(g.is_empty_response(text="I generated an image"))

    def test_cards_make_it_non_empty(self):
        self.assertFalse(g.is_empty_response(text="", card_attachments=[{}]))

    def test_an_error_makes_it_non_empty(self):
        self.assertFalse(g.is_empty_response(text="", error={"code": 8}))

    def test_a_rate_limit_marker_makes_it_non_empty(self):
        self.assertFalse(g.is_empty_response(text="", rate_limited=True))
        self.assertFalse(g.is_empty_response(text="", concurrency_limited=True))
        self.assertFalse(g.is_empty_response(text="", usage_limit_kind="pool_exhausted"))

    def test_the_bundle_constants(self):
        self.assertEqual(g.NEW_CONVERSATION_TITLE, "New conversation")
        self.assertEqual(g.IMAGINE_AGENT_MODEL_ID, "imagine_agent_model")
        self.assertEqual(g.STREAM_ERROR_SEVERITY_FATAL, "STREAM_ERROR_SEVERITY_FATAL")
