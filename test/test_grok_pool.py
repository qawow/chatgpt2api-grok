"""Grok pool isolation + normalize + routing helpers."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.grok_account_service import GrokAccountService
from utils.grok_models import (
    is_grok_image_model,
    is_grok_text_model,
    resolve_grok_image_model,
)


class GrokModelHelpersTest(unittest.TestCase):
    def test_image_models(self):
        self.assertTrue(is_grok_image_model("grok-2-image"))
        self.assertTrue(is_grok_image_model("grok-imagine"))
        self.assertTrue(is_grok_image_model("grok-imagine-image"))
        self.assertTrue(is_grok_image_model("GROK-2-Image-1212"))
        self.assertFalse(is_grok_image_model("gpt-image-2"))
        self.assertFalse(is_grok_image_model("codex-gpt-image-2"))
        self.assertFalse(is_grok_image_model("grok-4.5"))
        self.assertFalse(is_grok_image_model("grok-4"))

    def test_text_models(self):
        self.assertTrue(is_grok_text_model("grok-4.5"))
        self.assertFalse(is_grok_text_model("grok-2-image"))
        self.assertFalse(is_grok_text_model("gpt-4o"))

    def test_resolve_default(self):
        self.assertEqual(resolve_grok_image_model(None), "grok-2-image")
        self.assertEqual(resolve_grok_image_model("grok-imagine"), "grok-imagine-image")
        self.assertEqual(resolve_grok_image_model("grok-imagine-image"), "grok-imagine-image")
        self.assertEqual(resolve_grok_image_model("grok-4.5"), "grok-2-image")

    def test_free_image_models_skip_grok3_grok4(self):
        from services.grok_backend_api import _free_image_response_models

        models = _free_image_response_models("grok-2-image", "grok-4.5")
        self.assertEqual(models, ["grok-4.5"])
        self.assertNotIn("grok-3", models)
        self.assertNotIn("grok-4", models)


class GrokAccountServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "grok_accounts.json"
        self.svc = GrokAccountService(path=self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_normalize_cliproxy(self):
        item = {
            "type": "xai",
            "email": "u@example.com",
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "id_token": "id-1",
            "base_url": "https://api.x.ai/v1",  # must rewrite
            "headers": {"X-XAI-Token-Auth": "xai-grok-cli"},
        }
        normalized = self.svc.normalize_account(item)
        assert normalized is not None
        self.assertEqual(normalized["type"], "xai")
        self.assertEqual(normalized["provider"], "grok")
        self.assertIn("cli-chat-proxy.grok.com", normalized["base_url"])
        self.assertEqual(normalized["email"], "u@example.com")

    def test_reject_openai_type_without_grok_markers(self):
        item = {
            "type": "codex",
            "access_token": "openai-token",
            "base_url": "https://api.openai.com/v1",
        }
        self.assertIsNone(self.svc.normalize_account(item))

    def test_add_and_list_isolated_file(self):
        result = self.svc.add_account_items(
            [
                {
                    "type": "xai",
                    "access_token": "at-a",
                    "refresh_token": "rt",
                    "email": "a@x.ai",
                },
                {
                    "type": "xai",
                    "access_token": "at-b",
                    "email": "b@x.ai",
                },
            ]
        )
        self.assertEqual(result["added"], 2)
        self.assertEqual(self.svc.count(), 2)
        self.assertTrue(self.path.exists())
        # re-add same → skipped merge
        result2 = self.svc.add_account_items([{"access_token": "at-a", "type": "xai"}])
        self.assertEqual(result2["skipped"], 1)
        self.assertEqual(self.svc.count(), 2)

    def test_get_next_skips_disabled(self):
        self.svc.add_account_items(
            [
                {"access_token": "good", "type": "xai", "status": "正常"},
                {"access_token": "bad", "type": "xai", "disabled": True},
            ]
        )
        picked = {self.svc.get_next_account()["access_token"] for _ in range(4)}
        self.assertEqual(picked, {"good"})

    def test_get_next_skips_rate_limited(self):
        # 限流 (429) accounts must NOT be selected — bugfix for _is_available.
        self.svc.add_account_items(
            [
                {"access_token": "good", "type": "xai", "status": "正常"},
                {"access_token": "limited", "type": "xai", "status": "限流"},
            ]
        )
        picked = {self.svc.get_next_account()["access_token"] for _ in range(4)}
        self.assertEqual(picked, {"good"})

    def test_get_next_skips_recent_error_cooldown(self):
        # Account that just failed must cool down for _ERROR_COOLDOWN_SECONDS.
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.svc.add_account_items(
            [
                {"access_token": "good", "type": "xai", "status": "正常"},
                {
                    "access_token": "just-failed",
                    "type": "xai",
                    "status": "正常",
                    "last_error": "boom",
                    "last_error_at": now_iso,
                },
            ]
        )
        picked = {self.svc.get_next_account()["access_token"] for _ in range(4)}
        self.assertEqual(picked, {"good"})

    def test_mark_result_failure_marks_abnormal_on_auth_error(self):
        # 401/403 failure must flip status to 异常 so _is_available excludes it.
        self.svc.add_account_items(
            [{"access_token": "at", "type": "xai", "status": "正常"}]
        )
        self.svc.mark_result("at", False, error="responses failed: HTTP 401: unauthorized")
        account = self.svc.list_accounts()[0]
        self.assertEqual(account["status"], "异常")
        self.assertEqual(account["fail"], 1)
        self.assertIsNotNone(account["last_error_at"])

    def test_mark_result_failure_marks_abnormal_on_403(self):
        self.svc.add_account_items(
            [{"access_token": "at", "type": "xai", "status": "正常"}]
        )
        self.svc.mark_result("at", False, error="auth_403")
        account = self.svc.list_accounts()[0]
        self.assertEqual(account["status"], "异常")

    def test_mark_result_failure_marks_limited_on_429(self):
        self.svc.add_account_items(
            [{"access_token": "at", "type": "xai", "status": "正常"}]
        )
        self.svc.mark_result("at", False, error="rate limited (429)")
        account = self.svc.list_accounts()[0]
        self.assertEqual(account["status"], "限流")

    def test_mark_result_success_clears_abnormal(self):
        self.svc.add_account_items(
            [{"access_token": "at", "type": "xai", "status": "异常"}]
        )
        self.svc.mark_result("at", True)
        account = self.svc.list_accounts()[0]
        self.assertEqual(account["status"], "正常")
        self.assertIsNone(account["last_error"])

    def test_list_watchable_tokens_excludes_disabled_and_no_refresh(self):
        self.svc.add_account_items(
            [
                {"access_token": "ok", "type": "xai", "status": "正常", "refresh_token": "rt"},
                {"access_token": "off", "type": "xai", "disabled": True, "refresh_token": "rt"},
                {"access_token": "norec", "type": "xai", "status": "正常"},  # no refresh_token
            ]
        )
        watchable = self.svc.list_watchable_tokens()
        self.assertIn("ok", watchable)
        self.assertNotIn("off", watchable)
        self.assertNotIn("norec", watchable)

    def test_list_watchable_tokens_includes_abnormal_for_recovery(self):
        # 异常 accounts with refresh_token should be probed by the watcher
        # so they can recover (status cleared) if the token is actually alive.
        self.svc.add_account_items(
            [
                {"access_token": "ab", "type": "xai", "status": "异常", "refresh_token": "rt"},
                {"access_token": "lim", "type": "xai", "status": "限流", "refresh_token": "rt"},
            ]
        )
        watchable = self.svc.list_watchable_tokens()
        self.assertIn("ab", watchable)
        self.assertIn("lim", watchable)

    def test_get_next_auto_refreshes_expired_token(self):
        self.svc.add_account_items(
            [
                {
                    "access_token": "old-at",
                    "refresh_token": "rt-1",
                    "type": "xai",
                    "status": "正常",
                    "expired": "2020-01-01T00:00:00Z",
                    "token_endpoint": "https://auth.x.ai/oauth2/token",
                }
            ]
        )
        with mock.patch(
            "services.grok_account_service.refresh_access_token",
            return_value={
                "access_token": "new-at",
                "refresh_token": "rt-1",
                "expires_in": 3600,
                "expires_at": 4_000_000_000,
            },
        ) as refresh:
            picked = self.svc.get_next_account()
        self.assertIsNotNone(picked)
        self.assertEqual(picked["access_token"], "new-at")
        refresh.assert_called_once()
        # Pool re-keyed to new access token.
        self.assertEqual(self.svc.count(), 1)
        self.assertEqual(self.svc.list_accounts()[0]["access_token"], "new-at")

    def test_token_needs_refresh_within_skew(self):
        from datetime import datetime, timedelta, timezone

        soon = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        account = {
            "access_token": "at",
            "refresh_token": "rt",
            "expired": soon,
        }
        self.assertTrue(self.svc._token_needs_refresh(account, skew_seconds=300))
        far = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        account["expired"] = far
        self.assertFalse(self.svc._token_needs_refresh(account, skew_seconds=300))

    def test_delete(self):
        self.svc.add_account_items([{"access_token": "x", "type": "xai"}])
        out = self.svc.delete_accounts(["x"])
        self.assertEqual(out["removed"], 1)
        self.assertEqual(self.svc.count(), 0)

    def test_ensure_fresh_account_marks_abnormal_on_refresh_auth_failure(self):
        # Bugfix G3: refresh_token revocation (401/403) must mark the account 异常
        # instead of silently returning the old account.
        self.svc.add_account_items(
            [
                {
                    "access_token": "at",
                    "refresh_token": "rt",
                    "type": "xai",
                    "status": "正常",
                    "expired": "2020-01-01T00:00:00Z",
                    "token_endpoint": "https://auth.x.ai/oauth2/token",
                }
            ]
        )
        from services.grok_backend_api import GrokBackendError

        with mock.patch(
            "services.grok_account_service.refresh_access_token",
            side_effect=GrokBackendError("refresh failed: HTTP 401", status=401),
        ):
            self.svc.ensure_fresh_account(self.svc.list_accounts()[0], force=True)
        account = self.svc.list_accounts()[0]
        self.assertEqual(account["status"], "异常")
        self.assertIsNotNone(account["last_error_at"])

    def test_ensure_fresh_account_transient_error_does_not_mark_abnormal(self):
        # Network errors (GrokBackendError with non-auth status) must NOT mark 异常.
        self.svc.add_account_items(
            [
                {
                    "access_token": "at",
                    "refresh_token": "rt",
                    "type": "xai",
                    "status": "正常",
                    "expired": "2020-01-01T00:00:00Z",
                    "token_endpoint": "https://auth.x.ai/oauth2/token",
                }
            ]
        )
        from services.grok_backend_api import GrokBackendError

        with mock.patch(
            "services.grok_account_service.refresh_access_token",
            side_effect=GrokBackendError("refresh failed: HTTP 500", status=500),
        ):
            result = self.svc.ensure_fresh_account(self.svc.list_accounts()[0], force=True)
        account = self.svc.list_accounts()[0]
        self.assertEqual(account["status"], "正常")
        # Should still return the old account for caller to try
        self.assertEqual(result["access_token"], "at")

    def test_refresh_accounts_updates_last_error_on_non_backend_exception(self):
        # Bugfix A6: non-GrokBackendError exceptions must also update last_error
        # so the account doesn't look healthy to the next selection.
        self.svc.add_account_items(
            [
                {
                    "access_token": "at",
                    "refresh_token": "rt",
                    "type": "xai",
                    "status": "正常",
                    "token_endpoint": "https://auth.x.ai/oauth2/token",
                }
            ]
        )
        with mock.patch(
            "services.grok_account_service.refresh_access_token",
            side_effect=ValueError("unexpected JSON parse error"),
        ):
            self.svc.refresh_accounts(["at"])
        account = self.svc.list_accounts()[0]
        self.assertIsNotNone(account.get("last_error"))
        self.assertIsNotNone(account.get("last_error_at"))

    def test_replace_token_rollback_on_save_failure(self):
        # Bugfix S6: if _save fails after pop old + set new, the old token
        # must be restored so the account isn't lost.
        self.svc.add_account_items(
            [{"access_token": "old", "type": "xai", "status": "正常", "refresh_token": "rt"}]
        )
        with mock.patch.object(self.svc, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.svc.replace_token("old", {"access_token": "new"})
        # Old token must still be in the pool
        accounts = self.svc.list_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["access_token"], "old")

    def test_atomic_write_json(self):
        # Bugfix S1: JSON writes must be atomic (tmp + os.replace)
        from utils.atomic import atomic_write_json
        import json as _json

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = Path(f.name)
        try:
            atomic_write_json(path, {"key": "value", "num": 42})
            data = _json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data, {"key": "value", "num": 42})
        finally:
            path.unlink(missing_ok=True)


class GrokImageHandlerIsolationTest(unittest.TestCase):
    def test_handler_uses_only_grok_service(self):
        from services.protocol import grok_v1_image_generations

        fake_account = {
            "access_token": "grok-at",
            "base_url": "https://cli-chat-proxy.grok.com/v1",
            "headers": {},
        }
        with mock.patch(
            "services.protocol.grok_v1_image_generations.grok_account_service"
        ) as grok_svc, mock.patch(
            "services.protocol.grok_v1_image_generations.generate_image"
        ) as gen, mock.patch(
            "services.account_service.account_service.get_available_access_token"
        ) as chatgpt_pick, mock.patch(
            "services.image_storage_service.image_storage_service.save",
            side_effect=Exception("skip store"),
        ):
            grok_svc.get_next_account.return_value = fake_account
            gen.return_value = {
                "created": 1,
                "data": [{"b64_json": "QQ=="}],
                "_meta": {"upstream_path": "images/generations"},
            }
            result = grok_v1_image_generations.handle(
                {"prompt": "cube", "model": "grok-2-image", "n": 1}
            )
            self.assertEqual(result["data"][0]["b64_json"], "QQ==")
            chatgpt_pick.assert_not_called()
            grok_svc.get_next_account.assert_called()
            grok_svc.mark_result.assert_called()


class GrokFreeImageExtractionTest(unittest.TestCase):
    def test_extract_image_generation_call_result_jpeg(self):
        from services.grok_backend_api import _extract_images_from_responses

        # Minimal JPEG magic as base64 (/9j/...)
        jpeg_b64 = (
            "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
            "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwh"
            "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAAR"
            "CAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAn/xAAUEAEAAAAAAAAAAAAA"
            "AAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oA"
            "DAMBAAIRAxEAPwCwAA//2Q=="
        )
        payload = {
            "model": "grok-4.5-build-free",
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "plan"}]},
                {
                    "type": "image_generation_call",
                    "status": "completed",
                    "result": jpeg_b64,
                    "prompt": "a red apple",
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Here is the image."}],
                },
            ],
        }
        images = _extract_images_from_responses(payload)
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0]["b64_json"].startswith("/9j/"))

    def test_generate_image_prefers_free_responses_tool(self):
        from services import grok_backend_api as gba

        account = {
            "access_token": "at",
            "base_url": "https://cli-chat-proxy.grok.com/v1",
            "proxy": "socks5h://127.0.0.1:1080",
        }
        jpeg_b64 = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBD" + ("A" * 80)
        fake_resp = {
            "model": "grok-4.5-build-free",
            "status": "completed",
            "output": [
                {
                    "type": "image_generation_call",
                    "status": "completed",
                    "result": jpeg_b64,
                }
            ],
        }
        with mock.patch.object(gba, "create_response", return_value=fake_resp) as cr, mock.patch.object(
            gba, "list_upstream_models", side_effect=AssertionError("should not list models first")
        ), mock.patch.object(gba.requests, "post") as post:
            result = gba.generate_image(account, prompt="red apple", model="grok-2-image", n=1)
            self.assertEqual(result["_meta"]["upstream_path"], "responses+image_generation")
            self.assertTrue(result["data"][0]["b64_json"].startswith("/9j/"))
            cr.assert_called()
            # paid images endpoint must not be hit when free path works
            post.assert_not_called()
            kwargs = cr.call_args.kwargs
            self.assertEqual(kwargs.get("tools"), [{"type": "image_generation"}])
            self.assertIsNone(kwargs.get("tool_choice"))
            self.assertEqual(kwargs.get("model"), "grok-4.5")

    def test_generate_image_reraises_auth_error_instead_of_swallowing(self):
        # Bugfix G1: 401 from free Build path must be re-raised with the
        # original status, not swallowed into a 502 — otherwise the caller's
        # `if exc.status in {401, 403}` refresh-retry branch is dead code.
        from services import grok_backend_api as gba
        from services.grok_backend_api import GrokBackendError

        account = {
            "access_token": "dead-at",
            "base_url": "https://cli-chat-proxy.grok.com/v1",
        }
        with mock.patch.object(
            gba,
            "create_response",
            side_effect=GrokBackendError("responses failed: HTTP 401", status=401),
        ), mock.patch.object(gba.requests, "post") as post:
            with self.assertRaises(GrokBackendError) as ctx:
                gba.generate_image(account, prompt="test", model="grok-2-image", n=1)
            # The caller relies on exc.status being 401 to trigger refresh+retry.
            self.assertEqual(ctx.exception.status, 401)
            # paid path must not be tried when free path already got auth error
            post.assert_not_called()

    def test_http_session_is_reused_on_same_thread(self):
        from services.grok_backend_api import _http

        first = _http()
        second = _http()
        self.assertIs(first, second)

    def test_generate_image_reraises_429_instead_of_trying_more_models(self):
        from services import grok_backend_api as gba
        from services.grok_backend_api import GrokBackendError

        account = {
            "access_token": "at",
            "base_url": "https://cli-chat-proxy.grok.com/v1",
        }
        with mock.patch.object(
            gba,
            "create_response",
            side_effect=GrokBackendError("responses failed: HTTP 429", status=429),
        ), mock.patch.object(gba, "list_upstream_models") as catalog, mock.patch.object(
            gba, "_http"
        ) as http:
            with self.assertRaises(GrokBackendError) as ctx:
                gba.generate_image(account, prompt="test", model="grok-2-image", n=1)
            self.assertEqual(ctx.exception.status, 429)
            catalog.assert_not_called()
            http.assert_not_called()


class GrokImageRoutingTest(unittest.TestCase):
    def test_chat_completions_routes_grok_image_to_grok_pool(self):
        from services.protocol import openai_v1_chat_complete

        fake = {
            "created": 1,
            "data": [{"b64_json": "/9j/QQ=="}],
        }
        with mock.patch(
            "services.protocol.grok_v1_image_generations.handle",
            return_value=fake,
        ) as grok_handle, mock.patch(
            "services.protocol.openai_v1_chat_complete.stream_image_outputs_with_pool"
        ) as chatgpt_pool:
            result = openai_v1_chat_complete.handle(
                {"model": "grok-2-image", "messages": [{"role": "user", "content": "a cat"}]}
            )
        grok_handle.assert_called_once()
        chatgpt_pool.assert_not_called()
        self.assertIn("choices", result)

    def test_image_handle_rejects_grok_45_chat_model(self):
        from services.protocol import grok_v1_image_generations

        with self.assertRaises(ValueError) as ctx:
            grok_v1_image_generations.handle(
                {"prompt": "a cat", "model": "grok-4.5"}
            )
        self.assertIn("chat model", str(ctx.exception))

    def test_chat_completions_rejects_text_models(self):
        from fastapi import HTTPException
        from services.protocol import openai_v1_chat_complete

        with self.assertRaises(HTTPException) as ctx:
            openai_v1_chat_complete.handle(
                {"model": "gpt-5-mini", "messages": [{"role": "user", "content": "hello"}]}
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("image models", str(ctx.exception.detail))

    def test_chat_completions_rejects_grok_image_edit(self):
        from fastapi import HTTPException
        from services.protocol import openai_v1_chat_complete

        with mock.patch(
            "services.protocol.openai_v1_chat_complete.extract_chat_image",
            return_value=[(b"\x89PNG", "image/png")],
        ):
            with self.assertRaises(HTTPException) as ctx:
                openai_v1_chat_complete.handle(
                    {
                        "model": "grok-2-image",
                        "messages": [{"role": "user", "content": "edit this"}],
                    }
                )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("图生图", str(ctx.exception.detail))


if __name__ == "__main__":
    unittest.main()
