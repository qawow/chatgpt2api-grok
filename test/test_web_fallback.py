from __future__ import annotations

import unittest

from api.support import should_skip_spa_fallback


class SpaFallbackTests(unittest.TestCase):
    def test_unknown_api_and_auth_paths_do_not_get_dashboard_html(self) -> None:
        for path in (
            "api/cpa/pools",
            "/api/g2a/pool",
            "api/sub2api/servers",
            "v1/missing",
            "auth/login",
            "_next/static/missing.js",
        ):
            self.assertTrue(should_skip_spa_fallback(path), path)

    def test_app_routes_still_fall_back_to_spa(self) -> None:
        for path in ("settings", "settings/", "accounts", "image", ""):
            self.assertFalse(should_skip_spa_fallback(path), path)
