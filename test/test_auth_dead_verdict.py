"""403 不是吊销证明 —— 挑战/限流与真吊销必须区分。

背景（`.verify/REVERSE_ENGINEERING.md` §4.4o，2026-10-16 实测）：

号池里 50 个账号全部 `status=异常`，其中 **19 个从未发出过任何请求**
（`last_used_at` 为空、`success=0`、`fail=0`）就已被标记失效。
`last_refresh_error` 分布：

    26  session_refresh_stale_token_revoked
    21  token invalidated (/backend-api/me)
     3  refreshed_token_still_invalid_on_me

根因在 `_validate_access_token_alive`：它把 **401 与 403 一并**当作
「确认已死」：

    if resp.status_code in {401, 403}:
        saw_auth_dead = True
        return False

但 chatgpt.com 对**被挑战或被限流**的客户端同样回 403 —— 本会话反复观测到
`{"detail":"Unusual activity has been detected from your device. ..."}`。
整个号池共用一个出口 IP，批量探测 `/backend-api/me` 必然触发限流，
于是健康账号被永久判定为吊销。

`utils/network_diagnostics.py` 里本来就写着这条原则
（`error_details` 的 docstring：*never infer revocation from a 403*），
且 `response_diagnostics` 已能区分 `challenge` / `rate_limit` /
`http_auth` / `http_forbidden`。本模块把该定义接入验证路径。
"""
from __future__ import annotations

import unittest
from typing import Any

from services.account_service import _auth_dead_verdict
from utils.network_diagnostics import response_diagnostics


class _Response:
    """Minimal stand-in for a curl_cffi response."""

    def __init__(self, status_code: int, text: str = "", headers: dict | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class AuthDeadVerdictTests(unittest.TestCase):
    def test_401_is_a_confirmed_revoke(self):
        self.assertIs(_auth_dead_verdict(_Response(401, '{"detail":"Unauthorized"}')), True)

    def test_plain_403_json_is_a_confirmed_revoke(self):
        self.assertIs(
            _auth_dead_verdict(_Response(
                403, '{"detail":"Forbidden"}', {"content-type": "application/json"})),
            True,
        )

    def test_unusual_activity_403_is_not_a_revoke(self):
        """这正是把 19 个从未使用的账号判死的那个响应。"""
        body = ('{"detail":"Unusual activity has been detected from your device. '
                'Try again later. (9a2e03b0)"}')
        self.assertIs(
            _auth_dead_verdict(_Response(403, body, {"content-type": "application/json"})),
            False,
        )

    def test_cf_mitigated_challenge_is_not_a_revoke(self):
        self.assertIs(
            _auth_dead_verdict(_Response(
                403, "<html>Just a moment</html>",
                {"cf-mitigated": "challenge", "content-type": "text/html"})),
            False,
        )

    def test_html_challenge_body_is_not_a_revoke(self):
        self.assertIs(
            _auth_dead_verdict(_Response(
                403, "<html><body>cf-chl-abc</body></html>", {"content-type": "text/html"})),
            False,
        )

    def test_rate_limit_carries_no_verdict(self):
        self.assertIsNone(_auth_dead_verdict(_Response(429, '{"detail":"Too many requests"}')))

    def test_success_and_server_errors_carry_no_verdict(self):
        self.assertIsNone(_auth_dead_verdict(_Response(200, "{}")))
        self.assertIsNone(_auth_dead_verdict(_Response(500, "boom")))

    def test_a_broken_diagnostics_call_never_kills_an_account(self):
        """诊断自身出错时必须返回 None，不能把账号判死。"""
        class Hostile:
            status_code = 403

            @property
            def text(self) -> str:
                raise RuntimeError("boom")

            @property
            def headers(self) -> dict:
                raise RuntimeError("boom")

        self.assertIsNone(_auth_dead_verdict(Hostile()))


class ChallengeMarkerTests(unittest.TestCase):
    """共享定义必须认得 chatgpt.com 的挑战体。"""

    def test_unusual_activity_is_classified_as_a_challenge(self):
        result = response_diagnostics(
            403,
            '{"detail":"Unusual activity has been detected from your device."}',
            {"content-type": "application/json"},
        )
        self.assertEqual(result["failure_kind"], "challenge")

    def test_a_plain_json_403_stays_http_forbidden(self):
        result = response_diagnostics(
            403, '{"detail":"Forbidden"}', {"content-type": "application/json"})
        self.assertEqual(result["failure_kind"], "http_forbidden")

    def test_cf_mitigated_header_still_wins(self):
        result = response_diagnostics(
            403, "blocked", {"cf-mitigated": "challenge", "content-type": "application/json"})
        self.assertEqual(result["failure_kind"], "challenge")

    def test_429_is_still_a_rate_limit(self):
        result = response_diagnostics(429, "slow down", {})
        self.assertEqual(result["failure_kind"], "rate_limit")


if __name__ == "__main__":
    unittest.main()
