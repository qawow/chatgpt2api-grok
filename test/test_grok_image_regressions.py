"""Grok 生图链路回归测试。

1. 免费路径 200 但模型没调图片工具时，付费路径的 403（无额度）不得当成鉴权失败，
   否则账号被标异常，一个被拒的 prompt 能把整个池逐个打死；
2. 抽图以 image_generation_call 为准，url / data-URI 副本不再算作第二张；
3. size 在免费路径通过 prompt 传达，而不是被静默丢弃；
4. 失败分类按 HTTP 状态，不再对错误文本做 "403" 子串匹配；
5. 限流号冷却后自动回池；
6. 号池循环：422 直接返回不记失败、刷新未换 token 不重试、刷新抛错不再 UnboundLocalError。
"""
from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from services import grok_backend_api as backend
from services.grok_account_service import GrokAccountService, _classify_failure
from services.grok_backend_api import GrokBackendError, _extract_images_from_responses
from services.protocol import grok_v1_image_generations as pool

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 96).decode()
ACCOUNT = {"access_token": "at-1", "refresh_token": "", "type": "xai"}


def _paid_response(status: int):
    return mock.Mock(status_code=status, text='{"error":"spending limit"}')


class GenerateImageTests(unittest.TestCase):
    def test_declined_prompt_is_not_reported_as_auth_failure(self) -> None:
        declined = {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": "I can't create that image."}]}]}
        with mock.patch.object(backend, "create_response", return_value=declined), \
                mock.patch.object(backend, "_request", return_value=_paid_response(403)):
            with self.assertRaises(GrokBackendError) as ctx:
                backend.generate_image(ACCOUNT, prompt="something", model="grok-imagine-image")
        self.assertEqual(ctx.exception.status, 422)
        self.assertIn("I can't create that image", str(ctx.exception))

    def test_real_auth_failure_still_surfaces_as_auth(self) -> None:
        with mock.patch.object(backend, "create_response",
                               side_effect=GrokBackendError("HTTP 401", status=401)):
            with self.assertRaises(GrokBackendError) as ctx:
                backend.generate_image(ACCOUNT, prompt="cat")
        self.assertEqual(ctx.exception.status, 401)

    def test_size_reaches_the_free_agent_prompt(self) -> None:
        ok = {"output": [{"type": "image_generation_call", "result": PNG_B64}]}
        with mock.patch.object(backend, "create_response", return_value=ok) as create:
            backend.generate_image(ACCOUNT, prompt="cat", size="1024x1536")
        self.assertIn("1024x1536", create.call_args.kwargs["input_text"])


class ExtractImagesTests(unittest.TestCase):
    def test_tool_result_wins_over_url_copy_of_the_same_image(self) -> None:
        data = {"output": [
            {"type": "image_generation_call", "result": PNG_B64, "url": "https://cdn/x.png"},
            {"type": "message", "content": [{"type": "output_text",
                                             "text": f"![img](data:image/png;base64,{PNG_B64})"}]},
        ]}
        self.assertEqual(_extract_images_from_responses(data), [{"b64_json": PNG_B64}])

    def test_generic_scan_is_used_when_no_tool_result(self) -> None:
        data = {"data": [{"url": "https://cdn/a.png"}]}
        self.assertEqual(_extract_images_from_responses(data), [{"url": "https://cdn/a.png"}])


class ClassifyFailureTests(unittest.TestCase):
    def test_status_wins(self) -> None:
        self.assertEqual(_classify_failure(401, ""), "auth")
        self.assertEqual(_classify_failure(429, ""), "rate_limited")
        self.assertEqual(_classify_failure(502, "HTTP 403 somewhere in attempts"), "other")

    def test_stray_digits_in_text_do_not_mark_auth(self) -> None:
        self.assertEqual(_classify_failure(None, "attempts=[{'body_prefix': 'req_4031a'}]"), "other")


class RateLimitRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = GrokAccountService(path=Path(self.tmp.name) / "grok_accounts.json")
        self.addCleanup(self.tmp.cleanup)

    def _limited_account(self, minutes_ago: int) -> dict:
        at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
        return {"access_token": "at", "type": "xai", "status": "限流", "last_error_at": at}

    def test_recent_rate_limit_is_unavailable(self) -> None:
        self.assertFalse(self.svc._is_available(self._limited_account(1)))

    def test_rate_limit_expires(self) -> None:
        self.assertTrue(self.svc._is_available(self._limited_account(16)))

    def test_success_clears_rate_limit(self) -> None:
        self.svc.add_account_items([{"access_token": "at", "type": "xai", "status": "限流"}])
        self.svc.mark_result("at", True)
        self.assertEqual(self.svc.list_accounts()[0]["status"], "正常")


class PoolLoopTests(unittest.TestCase):
    def _run(self, **patches):
        body = {"prompt": "cat", "model": "grok-imagine-image"}
        with mock.patch.object(pool.grok_account_service, "get_next_account",
                               side_effect=[dict(ACCOUNT), None]), \
                mock.patch.object(pool.grok_account_service, "mark_result") as mark, \
                mock.patch.multiple(pool, **patches):
            try:
                return pool.handle(body), mark
            except RuntimeError as exc:
                return exc, mark

    def test_declined_prompt_does_not_mark_account(self) -> None:
        result, mark = self._run(generate_image=mock.Mock(side_effect=GrokBackendError("declined", status=422)))
        self.assertIsInstance(result, RuntimeError)
        mark.assert_not_called()

    def test_no_retry_when_refresh_keeps_the_same_token(self) -> None:
        generate = mock.Mock(side_effect=GrokBackendError("HTTP 401", status=401))
        with mock.patch.object(pool.grok_account_service, "ensure_fresh_account", return_value=dict(ACCOUNT)):
            _, mark = self._run(generate_image=generate)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(mark.call_args.kwargs.get("status"), 401)

    def test_refresh_raising_is_recorded_not_unbound(self) -> None:
        generate = mock.Mock(side_effect=GrokBackendError("HTTP 401", status=401))
        with mock.patch.object(pool.grok_account_service, "ensure_fresh_account",
                               side_effect=OSError("disk full")):
            result, mark = self._run(generate_image=generate)
        self.assertIsInstance(result, RuntimeError)
        self.assertNotIsInstance(result, UnboundLocalError)
        mark.assert_called_once()


if __name__ == "__main__":
    unittest.main()
