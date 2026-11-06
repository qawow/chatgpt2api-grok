"""``404 {"detail":"Model not found"}`` 的分类、熔断与账号处置回归测试。

背景（详见 .verify 探针结论，全部为实测）：

  * ``model=""`` / ``model="   "`` / ``model="zzz-not-a-model"`` 与账号自身
    ``/backend-api/models`` 目录里的每个 slug（35 个）返回完全相同的 404，
    说明这不是「slug 写错」。
  * ``/f/conversation/prepare`` 正常 200，``/f/conversation/resume`` 返回
    正常的 Pydantic 校验错误，说明路由、鉴权、sentinel 均无问题。
  * **匿名链路 ``/backend-anon/conversation`` 与 ``/backend-anon/f/conversation``
    在完全不带凭据时返回同一个 404**，换全新设备 ID 也不变 —— 因此这**不是**
    账号级终态，而是 ``-convo`` 后端（``x-build: 814069032b2f-convo``）的
    全局条件。旧实现把它当账号级终态、按 24 小时配额窗口逐个停放凭据，
    会在全局故障时把整个号池烧掉。
  * 体校验先于模型解析：缺 ``conversation_origin`` 会走到模型解析并失败；
    ``tpp`` / ``flora`` 能走到并成功（改回 ``conversation_model_origin_mismatch``）。
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import ANY, MagicMock, call, patch

from services.protocol.conversation import (
    MODEL_NOT_FOUND_BREAKER_SECS,
    ConversationRequest,
    ImageGenerationError,
    ImageOutput,
    _generate_single_image,
    is_model_not_found_error,
    model_not_found_breaker_remaining,
    note_model_not_found,
    reset_model_not_found_breaker,
)
from utils.helper import UpstreamHTTPError

MODEL_NOT_FOUND = UpstreamHTTPError(
    "/backend-api/f/conversation", 404, {"detail": "Model not found"}
)


class ModelNotFoundClassifierTests(unittest.TestCase):
    def test_matches_structured_upstream_error(self) -> None:
        self.assertTrue(is_model_not_found_error(MODEL_NOT_FOUND))

    def test_matches_formatted_message(self) -> None:
        self.assertTrue(is_model_not_found_error(str(MODEL_NOT_FOUND)))
        self.assertTrue(is_model_not_found_error(
            "/backend-api/f/conversation failed: status=404, body={\"detail\":\"Model not found\"}"
        ))

    def test_ignores_conversation_not_found_sibling(self) -> None:
        """同端点的另一个 404 是「会话不存在」，换 id 即可重试，不能误判。"""
        sibling = UpstreamHTTPError("/backend-api/f/conversation", 404, {
            "detail": {
                "message": "Conversation not found",
                "code": "conversation_not_found",
                "can_retry": False,
            }
        })
        self.assertFalse(is_model_not_found_error(sibling))
        self.assertFalse(is_model_not_found_error(str(sibling)))

    def test_ignores_other_failures(self) -> None:
        cases = [
            None,
            "",
            "token_revoked",
            "curl: (35) TLS connect error",
            UpstreamHTTPError("/backend-api/me", 401, {"detail": "unauthorized"}),
            UpstreamHTTPError("/backend-api/f/conversation", 403,
                              {"detail": "Unusual activity has been detected from your device"}),
            UpstreamHTTPError("/backend-api/f/conversation", 404, {"detail": "Not Found"}),
        ]
        for case in cases:
            self.assertFalse(is_model_not_found_error(case), msg=repr(case))


class ModelNotFoundBreakerTests(unittest.TestCase):
    """熔断器：区分「单个凭据命中」与「全局故障」。"""

    def setUp(self) -> None:
        reset_model_not_found_breaker()
        self.addCleanup(reset_model_not_found_breaker)

    def test_single_credential_is_not_global(self) -> None:
        verdict = note_model_not_found("a")
        self.assertFalse(verdict.is_global)
        self.assertEqual(verdict.park_secs, 86400.0)
        self.assertEqual(verdict.window_tokens, ("a",))
        self.assertEqual(model_not_found_breaker_remaining(), 0.0)

    def test_repeated_hits_from_one_credential_stay_local(self) -> None:
        for _ in range(5):
            self.assertFalse(note_model_not_found("a").is_global)

    def test_two_distinct_credentials_trip_the_breaker(self) -> None:
        self.assertFalse(note_model_not_found("a").is_global)
        verdict = note_model_not_found("b")
        self.assertTrue(verdict.is_global)
        self.assertEqual(verdict.park_secs, MODEL_NOT_FOUND_BREAKER_SECS)
        self.assertEqual(set(verdict.window_tokens), {"a", "b"})
        self.assertGreater(model_not_found_breaker_remaining(), 0.0)

    def test_window_expiry_forgets_old_credentials(self) -> None:
        with patch("services.protocol.conversation.time.time", return_value=1_000.0):
            self.assertFalse(note_model_not_found("a").is_global)
        # 超出 300 秒窗口后，旧凭据不再计入，单次命中重新变回局部。
        with patch("services.protocol.conversation.time.time", return_value=2_000.0):
            self.assertFalse(note_model_not_found("b").is_global)

    def test_reset_clears_breaker(self) -> None:
        note_model_not_found("a")
        note_model_not_found("b")
        self.assertGreater(model_not_found_breaker_remaining(), 0.0)
        reset_model_not_found_breaker()
        self.assertEqual(model_not_found_breaker_remaining(), 0.0)


class ModelNotFoundFailoverTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_model_not_found_breaker()
        self.addCleanup(reset_model_not_found_breaker)

    def run_generation(self, stream, *, failover_retries: int, tokens=("a", "b")):
        from contextlib import ExitStack

        stack = ExitStack()
        self.addCleanup(stack.close)
        accounts = stack.enter_context(patch("services.protocol.conversation.account_service"))
        backend = stack.enter_context(patch("services.protocol.conversation.OpenAIBackendAPI"))
        backend.side_effect = lambda access_token, **_kw: type("Backend", (), {
            "access_token": access_token, "close": lambda self: None,
        })()
        stack.enter_context(patch("services.protocol.conversation.stream_image_outputs", stream))
        stack.enter_context(patch("services.protocol.conversation.time.sleep"))
        stack.enter_context(patch(
            "services.protocol.conversation.proxy_settings.list_egress_candidates",
            return_value=[("direct", "")],
        ))
        cfg = stack.enter_context(patch("services.protocol.conversation.config"))
        cfg.image_account_failover_retries = failover_retries
        cfg.image_poll_failover_retries = 0
        cfg.image_text_failover_retries = 0
        cfg.image_tool_quota_cooldown_secs = 86400
        cfg.image_tool_short_cooldown_secs = 3600

        def pick(**kwargs):
            for token in tokens:
                if token not in kwargs["excluded_tokens"]:
                    return token
            raise RuntimeError("no available image quota")

        accounts.get_available_access_token.side_effect = pick
        accounts.get_account.side_effect = lambda token: {"email": token + "@test.invalid"}
        accounts.refresh_access_token.return_value = None
        accounts.confirm_token_revoked.return_value = False
        return accounts, lambda: _generate_single_image(
            ConversationRequest(model="gpt-image-2", prompt="cat"), 1, 1
        )

    def test_account_is_parked_and_pool_moves_on(self) -> None:
        visited = []

        def stream(backend, request, index, total):
            visited.append(backend.access_token)
            if backend.access_token == "a":
                raise MODEL_NOT_FOUND
            yield ImageOutput(kind="result", model=request.model, index=index,
                              total=total, data=[{"url": "ok"}])

        accounts, run = self.run_generation(stream, failover_retries=4)
        self.assertEqual(run()[-1].data, [{"url": "ok"}])
        self.assertEqual(visited, ["a", "b"])

        # 单次命中仍按局部故障处理：必须停放，否则下一次请求会立刻再挑中它。
        park = accounts.park_image_account.call_args
        self.assertIsNotNone(park)
        self.assertEqual(park.args[0], "a")
        self.assertEqual(park.kwargs["kind"], "model_unavailable")
        self.assertIn("Model not found", park.kwargs["reason"])
        self.assertGreater(park.kwargs["until_ts"], 0)

        # 不得把账号当成 token 失效来摘除：凭据本身是活的。
        accounts.remove_invalid_token.assert_not_called()
        accounts._record_invalid_token_seen.assert_not_called()

    def test_terminal_when_every_account_is_parked(self) -> None:
        def stream(backend, request, index, total):
            raise MODEL_NOT_FOUND
            yield  # pragma: no cover

        accounts, run = self.run_generation(stream, failover_retries=1, tokens=("a",))
        with self.assertRaises(ImageGenerationError) as ctx:
            run()
        self.assertEqual(ctx.exception.code, "upstream_model_not_found")
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(accounts.park_image_account.call_count, 1)

    def test_park_uses_the_long_quota_window_for_an_isolated_credential(self) -> None:
        """单凭据命中走长窗口：局部条件不会在 60 秒后自愈。"""
        def stream(backend, request, index, total):
            raise MODEL_NOT_FOUND
            yield  # pragma: no cover

        accounts, run = self.run_generation(stream, failover_retries=1, tokens=("a",))
        with patch("services.protocol.conversation.time.time", return_value=1_000_000.0):
            with self.assertRaises(ImageGenerationError):
                run()
        self.assertEqual(accounts.park_image_account.call_args.kwargs["until_ts"],
                         1_000_000.0 + 86400)

    def test_global_fault_short_parks_and_fails_fast(self) -> None:
        """多个不同凭据命中同一 404 => 全局故障：短停 + 立即失败，不烧号池。"""
        visited = []

        def stream(backend, request, index, total):
            visited.append(backend.access_token)
            raise MODEL_NOT_FOUND
            yield  # pragma: no cover

        accounts, run = self.run_generation(stream, failover_retries=4, tokens=("a", "b"))
        before = time.time()
        with self.assertRaises(ImageGenerationError) as ctx:
            run()
        after = time.time()
        self.assertEqual(ctx.exception.code, "upstream_model_not_found_global")
        self.assertEqual(ctx.exception.status_code, 503)
        # 第二个凭据命中即判定全局，不再继续走完整个池子。
        self.assertEqual(visited, ["a", "b"])

        # 每个凭据的**最终**停放都必须落在短窗口内：第一个凭据虽然是在
        # 「看起来只是局部故障」时被停的，判定为全局后必须被一并改短，
        # 否则一次后端抖动仍会烧掉它 24 小时。
        final_park: dict[str, float] = {}
        for call_args in accounts.park_image_account.call_args_list:
            final_park[call_args.args[0]] = call_args.kwargs["until_ts"]
        self.assertEqual(set(final_park), {"a", "b"})
        for token, until in final_park.items():
            self.assertGreaterEqual(until, before + MODEL_NOT_FOUND_BREAKER_SECS - 1,
                                    msg=token)
            self.assertLessEqual(until, after + MODEL_NOT_FOUND_BREAKER_SECS + 1,
                                 msg=token)

    def test_tripped_breaker_fails_new_requests_without_walking_the_pool(self) -> None:
        """熔断期内新请求直接失败，不再逐个凭据白等一轮上游往返。"""
        visited = []

        def stream(backend, request, index, total):
            visited.append(backend.access_token)
            raise MODEL_NOT_FOUND
            yield  # pragma: no cover

        accounts, run = self.run_generation(stream, failover_retries=4, tokens=("a", "b"))
        with self.assertRaises(ImageGenerationError):
            run()
        self.assertEqual(visited, ["a", "b"])
        self.assertGreater(model_not_found_breaker_remaining(), 0.0)

        # 熔断期内：一个新凭据都不该被碰。
        visited.clear()
        accounts.park_image_account.reset_mock()
        with self.assertRaises(ImageGenerationError) as ctx:
            run()
        self.assertEqual(ctx.exception.code, "upstream_model_not_found_global")
        self.assertEqual(visited, [])
        accounts.park_image_account.assert_not_called()


class ImageModelSlugTests(unittest.TestCase):
    """``_image_model_slug`` 必须返回目录里真实存在的 slug。"""

    # 实测 /backend-api/models 目录（2026-10-16）。
    LIVE_CATALOG = frozenset({
        "gpt-5-6", "gpt-5-3-mini", "gpt-5-6-mini", "gpt-5-6-t-mini",
        "gpt-5-6-t-mini-mini", "research", "gpt-6", "gpt-6-t-mini",
        "gpt-6-mini", "gpt-6-t-mini-mini", "auto",
    })

    def _slug(self, model: str) -> str:
        from services.openai_backend_api import OpenAIBackendAPI
        return OpenAIBackendAPI._image_model_slug(MagicMock(), model)

    def test_web_image_models_resolve_to_a_catalog_slug(self) -> None:
        for model in ("gpt-image-2.5", "gpt-image-2.6", "gpt-image-2"):
            slug = self._slug(model)
            self.assertIn(slug, self.LIVE_CATALOG, msg=model)

    def test_web_image_models_no_longer_target_the_retired_slug(self) -> None:
        """``gpt-5-3`` 已从目录下架，不能再作为映射目标。"""
        self.assertNotIn("gpt-5-3", self.LIVE_CATALOG)
        for model in ("gpt-image-2.5", "gpt-image-2.6", "gpt-image-2"):
            self.assertNotEqual(self._slug(model), "gpt-5-3")

    def test_codex_model_passes_through(self) -> None:
        self.assertEqual(self._slug("codex-gpt-image-2"), "codex-gpt-image-2")

    def test_unknown_model_falls_back_to_auto(self) -> None:
        self.assertEqual(self._slug(""), "auto")
        self.assertEqual(self._slug("nonsense"), "auto")


class PreparePayloadTests(unittest.TestCase):
    """prepare 体必须带上真实客户端的 dispatch/source 字段。"""

    def test_prepare_body_carries_dispatch_and_source(self) -> None:
        from services.openai_backend_api import OpenAIBackendAPI

        # Plain MagicMock: ``session`` is an instance attribute set in __init__,
        # so a spec'd mock would not expose it.
        api = MagicMock()
        api._image_model_slug = lambda m: "auto"
        api.locale = MagicMock(offset_min=-540, timezone="Asia/Tokyo")
        api.base_url = "https://chatgpt.com"
        api._image_headers.return_value = {}
        api.session.post.return_value = MagicMock(
            status_code=200, json=lambda: {"conduit_token": "tok"}
        )

        token = OpenAIBackendAPI._prepare_image_conversation(api, "cat", MagicMock(), "gpt-image-2.5")

        self.assertEqual(token, "tok")
        body = api.session.post.call_args.kwargs["json"]
        self.assertEqual(body["client_prepare_dispatch"], "immediate")
        self.assertEqual(body["client_prepare_source"], "composer_editor_state")
        self.assertEqual(body["model"], "auto")
        self.assertEqual(body["system_hints"], ["picture_v2"])

    def test_prepare_carries_dalle_operation_and_drops_picture_v2(self) -> None:
        """捕获的网页客户端在带 dalle 操作时会把 picture_v2 从 system_hints 移除。

        静态证据（bundle 956483，模块 r3oB）：
            null!=eY&&(a_={...a_,dalle:{from_client:{operation:eY.operation}}})
                      .system_hints&&(a_.system_hints=a_.system_hints.filter(
                          e=>"picture_v2"!==e))
        即 ``dalle`` 与 ``picture_v2`` 互斥。这条断言只覆盖请求体形状，
        不证明上游接受该组合。
        """
        from services.openai_backend_api import OpenAIBackendAPI

        api = MagicMock()
        api._image_model_slug = lambda m: "auto"
        api.locale = MagicMock(offset_min=-540, timezone="Asia/Tokyo")
        api.base_url = "https://chatgpt.com"
        api._image_headers.return_value = {}
        api.session.post.return_value = MagicMock(
            status_code=200, json=lambda: {"conduit_token": "tok"}
        )
        operation = {
            "type": "transformation",
            "original_file_id": "file-abc",
            "original_gen_id": "gen-123",
        }

        OpenAIBackendAPI._prepare_image_conversation(
            api, "make it blue", MagicMock(), "gpt-image-2.5", dalle_operation=operation,
        )

        body = api.session.post.call_args.kwargs["json"]
        self.assertEqual(body["dalle"], {"from_client": {"operation": operation}})
        self.assertEqual(body["system_hints"], [])

    def test_prepare_without_dalle_keeps_picture_v2(self) -> None:
        """没有 dalle 操作时保持原有行为，不发 dalle 键。"""
        from services.openai_backend_api import OpenAIBackendAPI

        api = MagicMock()
        api._image_model_slug = lambda m: "auto"
        api.locale = MagicMock(offset_min=-540, timezone="Asia/Tokyo")
        api.base_url = "https://chatgpt.com"
        api._image_headers.return_value = {}
        api.session.post.return_value = MagicMock(
            status_code=200, json=lambda: {"conduit_token": "tok"}
        )

        OpenAIBackendAPI._prepare_image_conversation(api, "cat", MagicMock(), "gpt-image-2.5")

        body = api.session.post.call_args.kwargs["json"]
        self.assertNotIn("dalle", body)
        self.assertEqual(body["system_hints"], ["picture_v2"])


if __name__ == "__main__":
    unittest.main()
