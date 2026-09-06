from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.account_service import AccountService
from services.auth_service import AuthService
from services.config import config
from services.openai_backend_api import InvalidAccessTokenError
from services.storage.json_storage import JSONStorageBackend
from utils.helper import anonymize_token, split_image_model


class AccountCapabilityTests(unittest.TestCase):
    def test_image_accounts_require_positive_quota(self) -> None:
        self.assertFalse(
            AccountService._is_image_account_available(
                {"status": "限流", "quota": 1, "refresh_token": "rt"}
            )
        )
        # Zero quota without recovery material is not selectable.
        self.assertFalse(
            AccountService._is_image_account_available(
                {"status": "正常", "quota": 0, "access_token": "at"}
            )
        )
        # Already-used free account with quota 0 stays out (no bootstrap).
        self.assertFalse(
            AccountService._is_image_account_available(
                {"status": "正常", "quota": 0, "refresh_token": "rt", "success": 1}
            )
        )
        self.assertTrue(
            AccountService._is_image_account_available(
                {"status": "正常", "quota": 1, "refresh_token": "rt"}
            )
        )

    def test_revoked_and_free_bootstrap_image_candidates(self) -> None:
        # Known-dead tokens must not be selected even with local quota leftover.
        self.assertFalse(
            AccountService._is_image_account_available(
                {
                    "status": "正常",
                    "quota": 25,
                    "refresh_token": "rt",
                    "last_refresh_error": "token invalidated (/backend-api/me)",
                }
            )
        )
        # Fresh free account with recovery material may bootstrap for remote check.
        self.assertTrue(
            AccountService._is_image_account_available(
                {
                    "status": "正常",
                    "quota": 0,
                    "type": "free",
                    "session_token": "sess",
                    "success": 0,
                }
            )
        )
        # Session-only marker still describes the account, but real quota can enter pool.
        self.assertTrue(
            AccountService._is_session_only_account(
                {"status": "正常", "quota": 5, "access_token": "at"}
            )
        )
        self.assertTrue(
            AccountService._is_image_account_available(
                {"status": "正常", "quota": 5, "access_token": "at", "session_only": True}
            )
        )
        self.assertFalse(
            AccountService._is_session_only_account(
                {"status": "正常", "quota": 5, "refresh_token": "rt"}
            )
        )

    def test_prolite_variants_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            self.assertEqual(service._normalize_account_type("prolite"), "ProLite")
            self.assertEqual(service._normalize_account_type("pro_lite"), "ProLite")

    def test_search_account_type_ignores_unrelated_scalar_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            self.assertIsNone(
                service._search_account_type(
                    {
                        "amr": ["pwd", "otp", "mfa"],
                        "chatgpt_compute_residency": "no_constraint",
                        "chatgpt_data_residency": "no_constraint",
                        "user_id": "user-I52GFfLGFM0dokFk2dBiKEBn",
                    }
                )
            )

    def test_mark_image_result_consumes_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-1"])
            service.update_account(
                "token-1",
                {
                    "status": "正常",
                    "quota": 1,
                },
            )

            updated = service.mark_image_result("token-1", success=True)

            self.assertIsNotNone(updated)
            self.assertEqual(updated["quota"], 0)
            self.assertEqual(updated["status"], "限流")

    def test_split_image_model_supports_plan_type_prefix(self) -> None:
        self.assertEqual(split_image_model("gpt-image-2"), (None, "gpt-image-2"))
        self.assertEqual(split_image_model("plus-codex-gpt-image-2"), ("plus", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("team-codex-gpt-image-2"), ("team", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("pro-codex-gpt-image-2"), ("pro", "codex-gpt-image-2"))
        self.assertEqual(split_image_model("plus-gpt-image-2"), (None, None))
        self.assertEqual(split_image_model("unknown-image-model"), (None, None))

    def test_get_available_access_token_filters_by_plan_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {"access_token": "token-plus", "type": "Plus", "status": "正常", "quota": 3, "refresh_token": "rt-plus"},
                    {"access_token": "token-pro", "type": "Pro", "status": "正常", "quota": 3, "refresh_token": "rt-pro"},
                ]
            )

            service.fetch_remote_info = lambda access_token, event="fetch_remote_info": service.get_account(access_token)

            plus_token = service.get_available_access_token(plan_type="plus")
            pro_token = service.get_available_access_token(plan_type="pro")
            service.release_image_slot(plus_token)
            service.release_image_slot(pro_token)

            self.assertEqual(plus_token, "token-plus")
            self.assertEqual(pro_token, "token-pro")

    def test_acquire_prefers_least_inflight_then_highest_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {"access_token": "busy", "type": "Plus", "status": "正常", "quota": 9, "refresh_token": "rt-a"},
                    {"access_token": "idle", "type": "Plus", "status": "正常", "quota": 4, "refresh_token": "rt-b"},
                ]
            )
            service._image_inflight["busy"] = 2
            picked = service._acquire_next_candidate_token()
            service.release_image_slot(picked)
            self.assertEqual(picked, "idle")

            service._image_inflight.clear()
            service._accounts["busy"]["quota"] = 2
            service._accounts["idle"]["quota"] = 8
            picked = service._acquire_next_candidate_token()
            service.release_image_slot(picked)
            self.assertEqual(picked, "idle")

    def test_acquire_does_not_oversubscribe_remaining_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {"access_token": "low", "type": "free", "status": "正常", "quota": 1, "refresh_token": "rt-a"},
                    {"access_token": "high", "type": "free", "status": "正常", "quota": 5, "refresh_token": "rt-b"},
                ]
            )
            service._image_inflight["low"] = 1
            picked = service._acquire_next_candidate_token()
            service.release_image_slot(picked)
            self.assertEqual(picked, "high")

    def test_get_available_access_token_skips_remote_probe_when_jwt_fresh(self) -> None:
        import base64
        import json
        import time as time_mod

        payload = {"exp": int(time_mod.time()) + 3600, "iat": int(time_mod.time())}
        jwt = "h." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".s"
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": jwt,
                        "type": "Plus",
                        "status": "正常",
                        "quota": 5,
                        "refresh_token": "rt-plus",
                    }
                ]
            )
            probed = {"n": 0}

            def boom(_access_token, event="fetch_remote_info"):
                probed["n"] += 1
                raise AssertionError("remote probe should be skipped")

            service.fetch_remote_info = boom  # type: ignore[method-assign]
            token = service.get_available_access_token()
            service.release_image_slot(token)
            self.assertEqual(token, jwt)
            self.assertEqual(probed["n"], 0)

    def test_get_available_access_token_probes_when_jwt_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "opaque-token",
                        "type": "Plus",
                        "status": "正常",
                        "quota": 5,
                        "refresh_token": "rt-plus",
                    }
                ]
            )
            probed = {"n": 0}

            def fake(access_token, event="fetch_remote_info"):
                probed["n"] += 1
                return service.get_account(access_token)

            service.fetch_remote_info = fake  # type: ignore[method-assign]
            token = service.get_available_access_token()
            service.release_image_slot(token)
            self.assertEqual(token, "opaque-token")
            self.assertEqual(probed["n"], 1)

    def test_refresh_accounts_can_remove_invalid_token_without_confirmation_delay(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = True
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{"access_token": "invalid-token", "status": "正常", "refresh_token": "rt-invalid"}])

                with patch(
                    "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                    side_effect=InvalidAccessTokenError("token invalidated (/backend-api/me)"),
                ):
                    result = service.refresh_accounts(["invalid-token"], defer_invalid_removal=False)

                self.assertEqual(result["refreshed"], 0)
                self.assertEqual(len(result["errors"]), 1)
                self.assertEqual(result["items"], [])
                self.assertIsNone(service.get_account("invalid-token"))
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value

    def test_refresh_accounts_defers_invalid_token_removal_by_default(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = True
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items([{"access_token": "invalid-token", "status": "正常", "refresh_token": "rt-invalid"}])

                with patch(
                    "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
                    side_effect=InvalidAccessTokenError("token invalidated (/backend-api/me)"),
                ):
                    result = service.refresh_accounts(["invalid-token"])

                account = service.get_account("invalid-token")
                self.assertEqual(result["refreshed"], 0)
                self.assertEqual(len(result["errors"]), 1)
                self.assertIsNotNone(account)
                self.assertEqual(account["invalid_count"], 1)
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value

    def test_session_only_invalid_token_is_kept_not_removed(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = True
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items(
                    [
                        {
                            "access_token": "session-token",
                            "status": "正常",
                            "quota": 0,
                            # no refresh_token → session_only
                        }
                    ]
                )
                account = service.get_account("session-token")
                self.assertIsNotNone(account)
                self.assertTrue(account["session_only"])
                self.assertTrue(account["fragile"])

                removed = service.remove_invalid_token("session-token", "test_event")
                self.assertFalse(removed)
                kept = service.get_account("session-token")
                self.assertIsNotNone(kept)
                self.assertEqual(kept["status"], "异常")
                self.assertTrue(kept["session_only"])
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value

    def test_normalize_marks_missing_refresh_as_session_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            bare = service._normalize_account({"access_token": "a1"})
            durable = service._normalize_account(
                {"access_token": "a2", "refresh_token": "rt-1"}
            )
            self.assertTrue(bare["session_only"])
            self.assertTrue(bare["fragile"])
            self.assertFalse(durable["session_only"])
            self.assertFalse(durable["fragile"])

    def test_free_session_only_skipped_from_periodic_watcher_lists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "free-session",
                        "status": "正常",
                        "type": "free",
                        "session_token": "sess",
                        # no refresh_token → session_only
                    },
                    {
                        "access_token": "plus-oauth",
                        "status": "正常",
                        "type": "Plus",
                        "refresh_token": "rt-plus",
                        "quota": 2,
                    },
                ]
            )
            normals = service.list_normal_tokens()
            self.assertNotIn("free-session", normals)
            self.assertIn("plus-oauth", normals)

    def test_revoked_cooldown_blocks_recover_and_refresh_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "dead-free",
                        "status": "异常",
                        "type": "free",
                        "session_token": "sess",
                        "password": "pw",
                        "last_refresh_error": "session_refresh_stale_token_revoked",
                        "last_refresh_error_at": datetime.now(timezone.utc).isoformat(),
                        "last_token_refresh_error": "session_refresh_stale_token_revoked",
                        "last_token_refresh_error_at": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            )
            acc = service.get_account("dead-free")
            self.assertTrue(AccountService._token_looks_revoked(acc))
            self.assertTrue(AccountService._revoked_cooldown_active(acc))
            self.assertTrue(AccountService._should_skip_periodic_refresh(acc))

            # refresh_accounts should skip without calling remote
            with patch(
                "services.openai_backend_api.OpenAIBackendAPI.get_user_info",
            ) as get_user_info:
                result = service.refresh_accounts(["dead-free"])
                get_user_info.assert_not_called()
            self.assertEqual(result.get("skipped"), 1)
            self.assertEqual(result.get("refreshed"), 0)

            # remove_invalid_token should not force another recover round-trip
            with patch.object(service, "refresh_access_token") as refresh_mock:
                removed = service.remove_invalid_token("dead-free", "test_cooldown")
                refresh_mock.assert_not_called()
            self.assertFalse(removed)
            kept = service.get_account("dead-free")
            self.assertIsNotNone(kept)
            self.assertEqual(kept["status"], "异常")

    def test_revoked_free_marked_abnormal_even_when_auto_remove_off(self) -> None:
        original_value = config.data.get("auto_remove_invalid_accounts")
        config.data["auto_remove_invalid_accounts"] = False
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
                service.add_account_items(
                    [
                        {
                            "access_token": "t1",
                            "status": "正常",
                            "type": "free",
                            "quota": 25,
                            "session_token": "sess",
                            "last_refresh_error": "token invalidated (/backend-api/me)",
                            "last_refresh_error_at": datetime.now(timezone.utc).isoformat(),
                            "last_token_refresh_error": "session_refresh_stale_token_revoked",
                            "last_token_refresh_error_at": datetime.now(timezone.utc).isoformat(),
                            "invalid_count": 1,
                        }
                    ]
                )
                with patch.object(service, "refresh_access_token", return_value=None):
                    removed = service.remove_invalid_token("t1", "test_mark")
                self.assertFalse(removed)
                kept = service.get_account("t1")
                self.assertEqual(kept["status"], "异常")
                self.assertFalse(AccountService._is_image_account_available(kept))
        finally:
            if original_value is None:
                config.data.pop("auto_remove_invalid_accounts", None)
            else:
                config.data["auto_remove_invalid_accounts"] = original_value

    def test_record_invalid_token_seen_marks_abnormal_on_revoked_error(self) -> None:
        # Bugfix #5: a revoked token (token invalidated) must flip status to 异常
        # immediately even while removal is deferred for free/session-only accounts.
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "revoked-free",
                        "status": "正常",
                        "type": "free",
                        "session_token": "sess",  # session-only, removal deferred
                    }
                ]
            )
            # Simulate the backend reporting the token is invalidated.
            service._record_invalid_token_seen(
                "revoked-free",
                "test_event",
                "token invalidated (/backend-api/me)",
                defer_invalid_removal=True,
            )
            account = service.get_account("revoked-free")
            self.assertIsNotNone(account)
            self.assertEqual(account["status"], "异常")
            self.assertEqual(account["invalid_count"], 1)
            # Still selectable for nothing — image pool must exclude revoked.
            self.assertFalse(AccountService._is_image_account_available(account))

    def test_list_abnormal_tokens_includes_recoverable_plus_accounts(self) -> None:
        # Bugfix #6: Plus/Pro 异常 accounts with refresh_token should be probed by
        # the watcher for recovery (previously 异常 accounts were never scanned).
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "plus-abnormal",
                        "status": "异常",
                        "type": "Plus",
                        "refresh_token": "rt-plus",
                    },
                    {
                        "access_token": "free-abnormal-session",
                        "status": "异常",
                        "type": "free",
                        "session_token": "sess",  # session-only → NOT recoverable
                    },
                    {
                        "access_token": "free-abnormal-password",
                        "status": "异常",
                        "type": "free",
                        "password": "pw",  # has password → recoverable
                    },
                    {
                        "access_token": "disabled",
                        "status": "禁用",
                        "type": "Plus",
                        "refresh_token": "rt",
                    },
                ]
            )
            abnormal = service.list_abnormal_tokens()
            self.assertIn("plus-abnormal", abnormal)
            self.assertIn("free-abnormal-password", abnormal)
            # session-only free without refresh_token is NOT recoverable
            self.assertNotIn("free-abnormal-session", abnormal)
            # 禁用 is never a candidate
            self.assertNotIn("disabled", abnormal)

    def test_list_abnormal_tokens_excludes_revoked_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "plus-cooldown",
                        "status": "异常",
                        "type": "Plus",
                        "refresh_token": "rt",
                        "last_refresh_error": "token invalidated (/backend-api/me)",
                        "last_refresh_error_at": datetime.now(timezone.utc).isoformat(),
                        "last_token_refresh_error": "session_refresh_stale_token_revoked",
                        "last_token_refresh_error_at": datetime.now(timezone.utc).isoformat(),
                    },
                ]
            )
            abnormal = service.list_abnormal_tokens()
            # Revoked cooldown active → skip
            self.assertNotIn("plus-cooldown", abnormal)

    def test_refresh_progress_survives_all_skipped_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "free-session",
                        "status": "正常",
                        "type": "free",
                        "session_token": "sess",
                    }
                ]
            )
            progress_id = "pid-skip-all"
            service.init_refresh_progress(progress_id, 1)
            result = service.refresh_accounts(["free-session"], progress_id=progress_id)
            self.assertEqual(result.get("skipped"), 1)
            progress = service.get_refresh_progress(progress_id)
            self.assertIsNotNone(progress)
            self.assertTrue(progress["done"])
            self.assertEqual(progress["result"]["skipped"], 1)

    def test_image_quota_message_explains_revoked_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items(
                [
                    {
                        "access_token": "dead",
                        "status": "异常",
                        "type": "free",
                        "quota": 25,
                        "session_token": "sess",
                        "last_refresh_error": "token invalidated (/backend-api/me)",
                        "last_refresh_error_at": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            )
            with self.assertRaises(RuntimeError) as ctx:
                service.get_available_access_token()
            msg = str(ctx.exception)
            self.assertIn("no available image quota", msg)
            self.assertIn("revoked", msg)

    def test_validate_access_token_alive_returns_none_on_network_error(self) -> None:
        # Bugfix A2: network errors must return None (inconclusive), not False
        # (which would mean "confirmed dead"). A transient proxy flake must not
        # discard a freshly-refreshed token.
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "at", "refresh_token": "rt"}])
            account = service.get_account("at")
            # Simulate a network error during /backend-api/me probe.
            mock_session = mock.MagicMock()
            mock_session.get.side_effect = ConnectionError("proxy timeout")
            with patch("curl_cffi.requests.Session", return_value=mock_session):
                result = service._validate_access_token_alive("at", account)
            self.assertIsNone(result)

    def test_validate_access_token_alive_returns_false_on_401(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "at", "refresh_token": "rt"}])
            account = service.get_account("at")
            mock_session = mock.MagicMock()
            mock_resp = mock.MagicMock()
            mock_resp.status_code = 401
            mock_session.get.return_value = mock_resp
            with patch("curl_cffi.requests.Session", return_value=mock_session):
                result = service._validate_access_token_alive("at", account)
            self.assertFalse(result)

    def test_validate_access_token_alive_returns_true_on_200(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "at", "refresh_token": "rt"}])
            account = service.get_account("at")
            mock_session = mock.MagicMock()
            mock_resp = mock.MagicMock()
            mock_resp.status_code = 200
            mock_session.get.return_value = mock_resp
            with patch("curl_cffi.requests.Session", return_value=mock_session):
                result = service._validate_access_token_alive("at", account)
            self.assertTrue(result)

    def test_token_looks_revoked_includes_app_session_terminated(self) -> None:
        # Bugfix S4: app_session_terminated must trigger revoked cooldown
        # to prevent OAuth refresh storms (refresh → terminated → background
        # relogin → fail → remove_invalid → refresh → ...).
        self.assertTrue(
            AccountService._token_looks_revoked(
                {"last_token_refresh_error": "oauth_refresh_http_400: app_session_terminated"}
            )
        )

    def test_remove_account_locked_cleans_aliases(self) -> None:
        # Bugfix S2: _remove_account_locked must clean up _token_aliases so
        # _resolve_access_token_locked doesn't return a dead token.
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([{"access_token": "old", "refresh_token": "rt"}])
            # Simulate a token rotation: old → new
            service._apply_refreshed_tokens(
                "old",
                {"access_token": "new", "refresh_token": "rt2"},
                "test",
            )
            # old should be gone from _accounts, alias old→new should exist
            with service._lock:
                self.assertNotIn("old", service._accounts)
                self.assertIn("new", service._accounts)
            self.assertIn("old", service._token_aliases)
            self.assertIsNotNone(service.get_account("new"))

            # Now remove "new" — alias must be cleaned up
            with service._lock:
                service._remove_account_locked("new")
            self.assertNotIn("old", service._token_aliases)
            self.assertNotIn("new", service._token_aliases)
            with service._lock:
                self.assertNotIn("new", service._accounts)

    def test_progress_dicts_gc_completed_entries(self) -> None:
        # Bugfix S3: _refresh_progress must not grow unbounded.
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            # Fill with completed entries up to the cap
            for i in range(AccountService._MAX_PROGRESS_ENTRIES):
                pid = f"old-{i}"
                service.init_refresh_progress(pid, 0)
                service.finish_refresh_progress(pid)
            self.assertGreaterEqual(len(service._refresh_progress), AccountService._MAX_PROGRESS_ENTRIES)
            # Adding one more should trigger GC of completed entries
            service.init_refresh_progress("new", 1)
            # Completed entries should have been cleaned up
            remaining_completed = sum(1 for p in service._refresh_progress.values() if p.get("done"))
            self.assertLess(remaining_completed, AccountService._MAX_PROGRESS_ENTRIES)

    def test_config_get_masks_backup_secrets(self) -> None:
        # Bugfix R1: GET /api/settings must not leak secret_access_key / passphrase.
        import os
        from services.config import ConfigStore

        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            store = ConfigStore(config_path)
            store.data["backup"] = {
                "provider": "cloudflare_r2",
                "account_id": "test-id",
                "access_key_id": "test-key",
                "secret_access_key": "super-secret-key",
                "passphrase": "super-passphrase",
                "bucket": "test-bucket",
            }
            store.data["image_storage"] = {
                "provider": "webdav",
                "url": "https://dav.example.com/",
                "username": "user",
                "webdav_password": "super-webdav-pw",
            }
            store._save()
            public = store.get()
            self.assertEqual(public["backup"]["secret_access_key"], "********")
            self.assertEqual(public["backup"]["passphrase"], "********")
            self.assertEqual(public["image_storage"]["webdav_password"], "********")
            # Raw values still available via dedicated getters
            self.assertEqual(store.get_backup_settings()["secret_access_key"], "super-secret-key")
            self.assertEqual(store.get_image_storage_settings()["webdav_password"], "super-webdav-pw")

    def test_config_update_preserves_secrets_on_masked_placeholder(self) -> None:
        # Bugfix R1: when client sends "********" (the masked placeholder),
        # the real secret must be preserved, not overwritten.
        from services.config import ConfigStore

        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            store = ConfigStore(config_path)
            store.data["backup"] = {
                "provider": "cloudflare_r2",
                "secret_access_key": "real-secret",
                "passphrase": "real-passphrase",
            }
            store._save()
            # Client sends masked placeholder back (as returned by GET)
            store.update({"backup": {
                "provider": "cloudflare_r2",
                "secret_access_key": "********",
                "passphrase": "********",
            }})
            # Real secrets must be preserved
            self.assertEqual(store.get_backup_settings()["secret_access_key"], "real-secret")
            self.assertEqual(store.get_backup_settings()["passphrase"], "real-passphrase")

    def test_proxy_runtime_settings_redact_url_credentials(self) -> None:
        # Bugfix P7: proxy_url / flaresolverr_url with credentials must be
        # redacted in the public settings returned to the frontend.
        from services.config import ConfigStore

        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            store = ConfigStore(config_path)
            store.data["proxy_runtime"] = {
                "enabled": True,
                "proxy_url": "http://admin:secret@proxy.example.com:8080",
                "resource_proxy_url": "http://user:pw@10.0.0.1:3128",
                "clearance": {
                    "flaresolverr_url": "http://fs:pass@flaresolverr.example.com:8191",
                },
            }
            store._save()
            public = store.get_public_proxy_runtime_settings()
            self.assertNotIn("secret", public["proxy_url"])
            self.assertNotIn("pw", public["resource_proxy_url"])
            clearance = public.get("clearance", {})
            self.assertNotIn("pass", clearance.get("flaresolverr_url", ""))


    def test_backend_headers_inject_clearance_cookies(self) -> None:
        # Bugfix: OpenAIBackendAPI._headers must call proxy_settings.build_headers
        # so FlareSolverr / manual cf_clearance actually reach chatgpt.com.
        from services.openai_backend_api import OpenAIBackendAPI

        with patch(
            "services.openai_backend_api.proxy_settings.build_headers",
            return_value={
                "Authorization": "Bearer tok",
                "Cookie": "cf_clearance=manual-token",
                "X-OpenAI-Target-Path": "/backend-api/me",
                "X-OpenAI-Target-Route": "/backend-api/me",
            },
        ) as build_headers:
            backend = OpenAIBackendAPI(access_token="tok")
            try:
                headers = backend._headers("/backend-api/me")
            finally:
                backend.close()
        build_headers.assert_called()
        self.assertEqual(headers.get("Cookie"), "cf_clearance=manual-token")


class TokenLogTests(unittest.TestCase):

    def test_anonymize_token_hides_raw_value(self) -> None:
        token = "super-secret-token"
        token_ref = anonymize_token(token)

        self.assertTrue(token_ref.startswith("token:"))
        self.assertNotIn(token, token_ref)


class AuthServiceTests(unittest.TestCase):
    def test_create_authenticate_disable_and_delete_user_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))

            item, raw_key = service.create_key(role="user", name="Alice")

            self.assertEqual(item["role"], "user")
            self.assertEqual(item["name"], "Alice")
            self.assertTrue(item["enabled"])
            self.assertTrue(raw_key.startswith("sk-"))

            authed = service.authenticate(raw_key)
            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])
            self.assertEqual(authed["role"], "user")
            self.assertIsNotNone(authed["last_used_at"])

            updated = service.update_key(item["id"], {"enabled": False}, role="user")
            self.assertIsNotNone(updated)
            self.assertFalse(updated["enabled"])
            self.assertIsNone(service.authenticate(raw_key))

            self.assertTrue(service.delete_key(item["id"], role="user"))
            self.assertFalse(service.delete_key(item["id"], role="user"))
            self.assertEqual(service.list_keys(role="user"), [])

    def test_authenticate_ignores_last_used_save_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            item, raw_key = service.create_key(role="user", name="Alice")

            def fail_save() -> None:
                raise OSError("disk unavailable")

            service._save = fail_save

            authed = service.authenticate(raw_key)

            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])
            self.assertIsNotNone(authed["last_used_at"])

    def test_update_user_key_replaces_raw_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            item, raw_key = service.create_key(role="user", name="Alice")

            updated = service.update_key(item["id"], {"key": "sk-user-custom-key"}, role="user")

            self.assertIsNotNone(updated)
            self.assertIsNone(service.authenticate(raw_key))

            authed = service.authenticate("sk-user-custom-key")
            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])

    def test_user_key_name_must_be_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            first, _ = service.create_key(role="user", name="Alice")
            second, _ = service.create_key(role="user", name="Bob")

            with self.assertRaisesRegex(ValueError, "这个名称已经在使用中了"):
                service.create_key(role="user", name="Alice")

            with self.assertRaisesRegex(ValueError, "这个名称已经在使用中了"):
                service.update_key(second["id"], {"name": "Alice"}, role="user")

            updated = service.update_key(first["id"], {"name": "Alice"}, role="user")
            self.assertIsNotNone(updated)
            self.assertEqual(updated["name"], "Alice")


if __name__ == "__main__":
    unittest.main()
