"""浏览器指纹自洽性回归测试。

背景（.verify 实测，2026-10-16）：

修复前本仓库发出的请求头是**三重矛盾**的：

    User-Agent                  : ...Chrome/142.0.7540.34...   （无 Edg/）
    Sec-Ch-Ua                   : "Microsoft Edge";v="143", "Chromium";v="143"
    Sec-Ch-Ua-Full-Version      : "143.0.3650.96"              （Edge 版本号）
    Sec-Ch-Ua-Full-Version-List : "Microsoft Edge";v="143.0.3650.96", ...
    TLS impersonate             : chrome142

真实浏览器不可能产生这个组合：UA 声称 Chrome 142，客户端提示声称 Edge 143，
而 TLS 层是 Chrome 142。TLS 指纹无法用请求头伪装，所以头必须跟着 TLS 走。

修复方式：新增 ``browser_identity(impersonate)``，从 TLS 目标派生**唯一**的
UA + 客户端提示集合，``_build_fp`` 不再硬编码任何浏览器版本。
"""
from __future__ import annotations

import re
import unittest

from services.openai_backend_api import (
    DEFAULT_IMPERSONATE,
    OpenAIBackendAPI,
    browser_identity,
)


class BrowserIdentityTests(unittest.TestCase):
    def test_chrome_identity_is_self_consistent(self) -> None:
        identity = browser_identity("chrome142")
        self.assertEqual(identity.family, "chrome")
        self.assertEqual(identity.major, "142")
        self.assertIn("Chrome/142.", identity.user_agent)
        self.assertNotIn("Edg/", identity.user_agent)
        self.assertIn('"Chromium";v="142"', identity.sec_ch_ua)
        self.assertIn('"Google Chrome";v="142"', identity.sec_ch_ua)
        self.assertNotIn("Edge", identity.sec_ch_ua)
        self.assertEqual(identity.sec_ch_ua_full_version, '"142.0.0.0"')
        self.assertIn('"Chromium";v="142.0.0.0"', identity.sec_ch_ua_full_version_list)

    def test_edge_identity_keeps_its_own_brand(self) -> None:
        identity = browser_identity("edge101")
        self.assertEqual(identity.family, "edge")
        self.assertIn("Edg/101.", identity.user_agent)
        self.assertIn('"Microsoft Edge";v="101"', identity.sec_ch_ua)
        self.assertIn("Edge", identity.sec_ch_ua_full_version_list)

    def test_firefox_identity_sends_no_client_hints(self) -> None:
        identity = browser_identity("firefox147")
        self.assertEqual(identity.family, "firefox")
        self.assertIn("Firefox/147.0", identity.user_agent)
        self.assertFalse(identity.sends_client_hints)
        self.assertEqual(identity.sec_ch_ua, "")

    def test_unknown_target_falls_back_to_the_default_major(self) -> None:
        """未知 impersonate 也必须产出自洽身份，不能漏出空版本号。"""
        identity = browser_identity("totally-unknown")
        self.assertEqual(identity.family, "chrome")
        self.assertTrue(identity.major.isdigit())
        self.assertIn(f"Chrome/{identity.major}.", identity.user_agent)

    def test_matches_ua_rejects_cross_brand_and_cross_version(self) -> None:
        chrome = browser_identity("chrome142")
        self.assertTrue(chrome.matches_ua(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/142.0.7540.34 Safari/537.36"))
        # 跨品牌
        self.assertFalse(chrome.matches_ua(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0"))
        # 跨版本
        self.assertFalse(chrome.matches_ua(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"))
        # 跨家族
        self.assertFalse(chrome.matches_ua(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:142.0) Gecko/20100101 Firefox/142.0"))


class SessionFingerprintTests(unittest.TestCase):
    """真实会话头必须与 TLS 目标一致。"""

    def _headers(self, account: dict | None = None) -> tuple[dict, str]:
        api = OpenAIBackendAPI("")
        self.addCleanup(api.close)
        if account is not None:
            api.account = account
            api.fp = api._build_fp()
            api.session.headers.update({
                "User-Agent": api.fp["user-agent"],
                "Sec-Ch-Ua": api.fp["sec-ch-ua"],
                "Sec-Ch-Ua-Full-Version": api.fp["sec-ch-ua-full-version"],
                "Sec-Ch-Ua-Full-Version-List": api.fp["sec-ch-ua-full-version-list"],
            })
        return ({str(k).lower(): str(v) for k, v in api.session.headers.items()},
                str(api.fp.get("impersonate") or ""))

    def test_user_agent_major_matches_the_client_hints(self) -> None:
        headers, impersonate = self._headers()
        ua = headers.get("user-agent", "")
        match = re.search(r"Chrome/(\d+)\.", ua)
        self.assertIsNotNone(match, msg=ua)
        major = match.group(1)
        self.assertIn(f'"Chromium";v="{major}"', headers.get("sec-ch-ua", ""))
        self.assertEqual(headers.get("sec-ch-ua-full-version"), f'"{major}.0.0.0"')
        self.assertIn(f'"Chromium";v="{major}.0.0.0"',
                      headers.get("sec-ch-ua-full-version-list", ""))
        self.assertEqual(impersonate, f"chrome{major}")

    def test_no_cross_brand_mix(self) -> None:
        """UA 说 Chrome 时，客户端提示里不能出现 Edge，反之亦然。"""
        headers, _ = self._headers()
        ua = headers.get("user-agent", "")
        hints = " ".join((
            headers.get("sec-ch-ua", ""),
            headers.get("sec-ch-ua-full-version-list", ""),
        ))
        if "Edg/" in ua:
            self.assertIn("Microsoft Edge", hints)
        else:
            self.assertNotIn("Edge", hints)
            self.assertNotIn("Edg/", ua)

    def test_a_stored_cross_brand_user_agent_is_replaced(self) -> None:
        """账号里存的 Edge UA 配 chrome142 TLS 时必须被换掉。"""
        headers, impersonate = self._headers({
            "fp": {
                "impersonate": "chrome142",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/136.0.7103.73 Safari/537.36 "
                    "Edg/136.0.0.0"
                ),
            }
        })
        self.assertEqual(impersonate, "chrome142")
        self.assertNotIn("Edg/", headers.get("user-agent", ""))
        self.assertIn("Chrome/142.", headers.get("user-agent", ""))

    def test_a_matching_stored_user_agent_is_preserved(self) -> None:
        """同家族同主版本的存量 UA 应保留，避免无谓改动账号画像。"""
        stored = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/142.0.7540.34 Safari/537.36"
        )
        headers, _ = self._headers({"fp": {"impersonate": "chrome142",
                                           "user-agent": stored}})
        self.assertEqual(headers.get("user-agent"), stored)

    def test_default_impersonate_is_chrome(self) -> None:
        self.assertTrue(str(DEFAULT_IMPERSONATE).startswith("chrome"))


if __name__ == "__main__":
    unittest.main()
