from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
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
