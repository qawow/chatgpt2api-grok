from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from utils import egress_locale
from utils.egress_locale import EgressLocale, default_locale, resolve_egress_locale
from utils.pow import build_pow_config


def _fake_detect(tz="America/Los_Angeles", country="US"):
    return lambda proxy_url: egress_locale._build(tz, country, "detected")


class EgressLocaleTests(unittest.TestCase):
    def setUp(self) -> None:
        with egress_locale._cache_lock:
            egress_locale._cache.clear()

    def test_browser_date_format(self) -> None:
        loc = egress_locale._build("Asia/Tokyo", "JP", "test")
        text = loc.format_browser_date(datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc))
        self.assertTrue(text.endswith(" GMT+0900 (Japan Standard Time)"), text)
        self.assertIn("2026 21:00:00", text)  # UTC+9

    def test_offset_sign_and_minutes(self) -> None:
        tokyo = egress_locale._build("Asia/Tokyo", "JP", "test")
        self.assertEqual(tokyo.offset_min, -540)
        self.assertEqual(tokyo.gmt_offset, "+0900")
        la = egress_locale._build("America/Los_Angeles", "US", "test")
        self.assertEqual(la.gmt_offset[:3], "-07")  # 9 月是 PDT -0700
        self.assertGreater(la.offset_min, 0)

    def test_language_follows_country(self) -> None:
        self.assertEqual(egress_locale._build("Asia/Tokyo", "JP", "t").language, "ja-JP")
        self.assertEqual(egress_locale._build("America/Los_Angeles", "US", "t").language, "en-US")

    def test_detect_result_cached_per_proxy(self) -> None:
        with patch.object(egress_locale, "_detect", side_effect=_fake_detect()) as det:
            a = resolve_egress_locale("socks5h://proxy-a:1")
            b = resolve_egress_locale("socks5h://proxy-a:1")
        self.assertIs(a, b)
        self.assertEqual(det.call_count, 1)
        self.assertEqual(a.timezone, "America/Los_Angeles")

    def test_env_override_wins(self) -> None:
        with patch.dict("os.environ", {"OAI_CLIENT_TIMEZONE": "Europe/London", "OAI_CLIENT_COUNTRY": "GB"}):
            loc = resolve_egress_locale("socks5h://proxy-a:1")
        self.assertEqual(loc.timezone, "Europe/London")
        self.assertEqual(loc.source, "env")
        self.assertEqual(loc.language, "en-GB")

    def test_detect_failure_falls_back_to_default(self) -> None:
        with patch.object(egress_locale, "_detect", return_value=None):
            loc = resolve_egress_locale("socks5h://dead-proxy:1")
        self.assertEqual(loc.source, "default")
        self.assertEqual(loc, default_locale())

    def test_pow_config_uses_locale_and_profile(self) -> None:
        loc = egress_locale._build("Asia/Tokyo", "JP", "test")
        config = build_pow_config("ua", locale=loc, screen=(1920, 1080), cores=16, sid="fixed-sid")
        self.assertEqual(config[0], 1920 + 1080)
        self.assertEqual(config[7], "ja-JP")
        self.assertEqual(config[8], "ja-JP,ja,en-US,en")
        self.assertEqual(config[14], "fixed-sid")
        self.assertEqual(config[16], 16)
        self.assertTrue(str(config[1]).endswith(loc.date_suffix), config[1])


if __name__ == "__main__":
    unittest.main()
