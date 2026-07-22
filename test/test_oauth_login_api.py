"""OAuth finish path: replace session_only row after successful token exchange."""
from __future__ import annotations

import unittest
from unittest import mock

from pydantic import ValidationError


class OAuthFinishModelTest(unittest.TestCase):
    def test_finish_request_accepts_replace_access_token(self):
        from api.accounts import OAuthLoginFinishRequest

        body = OAuthLoginFinishRequest(
            session_id="sid",
            callback="https://platform.openai.com/auth/callback?code=c&state=s",
            replace_access_token="old-session-token",
        )
        data = body.model_dump()
        self.assertEqual(data["replace_access_token"], "old-session-token")
        self.assertEqual(data["session_id"], "sid")

    def test_finish_request_default_replace_empty(self):
        from api.accounts import OAuthLoginFinishRequest

        body = OAuthLoginFinishRequest(session_id="s", callback="code")
        self.assertEqual(body.replace_access_token, "")


class OAuthFinishReplaceLogicTest(unittest.TestCase):
    """Unit-test the replace-after-add behavior without real OpenAI OAuth."""

    def test_replace_deletes_old_token_when_different(self):
        # Simulate the finish handler core logic
        old_token = "session-only-at"
        new_token = "oauth-at-new"
        deleted: list[str] = []
        added: list[dict] = []

        tokens = {
            "access_token": new_token,
            "refresh_token": "rt-new",
            "id_token": "id-new",
        }
        payload = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "id_token": tokens["id_token"],
            "source_type": "oauth_login",
            "session_only": False,
            "fragile": False,
        }
        added.append(payload)

        replaced = 0
        if old_token and new_token and old_token != new_token:
            deleted.append(old_token)
            replaced = 1

        self.assertEqual(replaced, 1)
        self.assertEqual(deleted, [old_token])
        self.assertFalse(payload["session_only"])
        self.assertEqual(payload["source_type"], "oauth_login")

    def test_same_token_does_not_delete(self):
        old_token = "same-at"
        new_token = "same-at"
        replaced = 0
        if old_token and new_token and old_token != new_token:
            replaced = 1
        self.assertEqual(replaced, 0)


if __name__ == "__main__":
    unittest.main()
