"""Regression tests for the image_gen tool rate-limit taxonomy.

Both refusal kinds arrive as HTTP 200 with headers byte-identical to a success
stream (no retry-after, no x-rate-limit-*), so the tool message itself is the
only signal. Before this taxonomy the caller spent the full
image_poll_timeout_secs budget chasing an image that was never queued, then
parked the credential for the generic 60s transient cooldown — which handed the
same exhausted account straight back to the picker.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services.account_service import AccountService
from services.config import config
from services.image_task_service import ImageTaskService
from services.openai_backend_api import (
    DEFAULT_TOOL_COOLDOWN_SECS,
    OpenAIBackendAPI,
    ToolCooldownError,
    ToolQuotaError,
    classify_image_tool_refusal,
    parse_reset_window_secs,
)
from services.protocol.conversation import (
    ConversationRequest,
    ImageGenerationError,
    ImageOutput,
    _generate_single_image,
)
from services.storage.json_storage import JSONStorageBackend

# Verbatim payloads captured from upstream SSE (see .verify/exhausted_gen.txt and
# .verify/empty2_m1.txt). Do not "clean up" the wording: the classifier keys on it.
QUOTA_TOOL_MESSAGE = {
    "message": {
        "author": {"role": "tool", "name": "t2uay3k.sj1i4kz", "metadata": {}},
        "content": {
            "content_type": "system_error",
            "name": "ChatGPTAgentToolRateLimitException",
            "text": (
                "Before doing anything else, explicitly explain to the user that you were "
                'unable to invoke the image_gen.text2im tool right now. Make sure to begin '
                'your response with "你已达到 Free 套餐的图像生成请求上限。上限将在 10小时 '
                '后重置，届时可创建更多图像。". DO NOT UNDER ANY CIRCUMSTANCES retry using '
                "this tool until the next user message."
            ),
        },
        "status": "finished_successfully",
    }
}

COOLDOWN_TOOL_MESSAGE = {
    "message": {
        "author": {"role": "tool", "name": "t2uay3k.sj1i4kz", "metadata": {}},
        "content": {
            "content_type": "text",
            "parts": [
                "You're generating images too quickly. To ensure the best experience for "
                "everyone, we have rate limits in place. Please wait for an hour before "
                "generating more images. Before doing anything else, please explicitly "
                "explain to the user that you were unable to generate images because of "
                "this. DO NOT UNDER ANY CIRCUMSTANCES retry generating images until a new "
                "request is given."
            ],
        },
        "status": "finished_successfully",
        "metadata": {"is_error": True},
    }
}

SUCCESS_TOOL_MESSAGE = {
    "message": {
        "author": {"role": "tool", "name": "t2uay3k.sj1i4kz", "metadata": {}},
        "content": {
            "content_type": "multimodal_text",
            "parts": [
                {
                    "content_type": "image_asset_pointer",
                    "asset_pointer": "sediment://file_00000000715c81f595eee70602faa7ed",
                    "mime_type": "image/png",
                    "width": 1254,
                    "height": 1254,
                }
            ],
        },
        "metadata": {"async_task_type": "image_gen"},
    }
}


class ClassifierTests(unittest.TestCase):
    def test_quota_refusal_is_classified_with_stated_window(self) -> None:
        kind, text, retry_after = classify_image_tool_refusal(QUOTA_TOOL_MESSAGE)
        self.assertEqual(kind, "quota")
        self.assertIn("图像生成请求上限", text)
        self.assertAlmostEqual(retry_after, 10 * 3600.0, places=3)

    def test_cooldown_refusal_is_classified_with_hour_window(self) -> None:
        kind, text, retry_after = classify_image_tool_refusal(COOLDOWN_TOOL_MESSAGE)
        self.assertEqual(kind, "cooldown")
        self.assertIn("too quickly", text)
        self.assertAlmostEqual(retry_after, 3600.0, places=3)

    def test_successful_tool_message_is_not_a_refusal(self) -> None:
        # The success path is content_type multimodal_text with a sediment://
        # pointer; classifying it would break every working generation.
        self.assertIsNone(classify_image_tool_refusal(SUCCESS_TOOL_MESSAGE))

    def test_assistant_text_mentioning_limits_is_not_a_refusal(self) -> None:
        # The model paraphrases the tool directive into user-visible text, and
        # that paraphrase must never be mistaken for the tool message itself.
        paraphrase = {
            "message": {
                "author": {"role": "assistant", "name": None},
                "content": {"content_type": "text", "parts": ["由于我这边发生了错误，我未能生成图片。"]},
            }
        }
        self.assertIsNone(classify_image_tool_refusal(paraphrase))

    def test_accepts_conversation_mapping_node_and_sse_event(self) -> None:
        node = {"id": "x", "message": QUOTA_TOOL_MESSAGE["message"]}
        self.assertEqual(classify_image_tool_refusal(node)[0], "quota")
        event = {"o": "add", "v": {"message": COOLDOWN_TOOL_MESSAGE["message"]}}
        self.assertEqual(classify_image_tool_refusal(event)[0], "cooldown")

    def test_unknown_window_falls_back_to_hour_for_cooldown(self) -> None:
        payload = {
            "message": {
                "author": {"role": "tool"},
                "content": {
                    "content_type": "text",
                    "parts": ["You're generating images too quickly. we have rate limits in place."],
                },
            }
        }
        kind, _text, retry_after = classify_image_tool_refusal(payload)
        self.assertEqual(kind, "cooldown")
        self.assertAlmostEqual(retry_after, DEFAULT_TOOL_COOLDOWN_SECS, places=3)

    def test_parse_reset_window_handles_hours_and_minutes(self) -> None:
        self.assertAlmostEqual(parse_reset_window_secs("上限将在 5小时 后重置"), 5 * 3600.0, places=3)
        self.assertAlmostEqual(parse_reset_window_secs("reset in 2 hours"), 2 * 3600.0, places=3)
        self.assertAlmostEqual(parse_reset_window_secs("45 minutes remaining"), 45 * 60.0, places=3)
        # Unknown must be 0.0 so callers apply their own default instead of
        # reading "no window" as "already reset".
        self.assertEqual(parse_reset_window_secs("please try again later"), 0.0)


class SseDetectionTests(unittest.TestCase):
    def _backend(self) -> OpenAIBackendAPI:
        backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        backend.progress_callback = None
        return backend

    def test_quota_payload_raises_tool_quota_error(self) -> None:
        backend = self._backend()
        payload = json.dumps({"o": "add", "v": QUOTA_TOOL_MESSAGE})
        with self.assertRaises(ToolQuotaError) as caught:
            backend._raise_if_tool_refusal(payload)
        self.assertAlmostEqual(caught.exception.retry_after_secs, 10 * 3600.0, places=3)

    def test_cooldown_payload_raises_tool_cooldown_error(self) -> None:
        backend = self._backend()
        payload = json.dumps({"o": "add", "v": COOLDOWN_TOOL_MESSAGE})
        with self.assertRaises(ToolCooldownError) as caught:
            backend._raise_if_tool_refusal(payload)
        self.assertAlmostEqual(caught.exception.retry_after_secs, 3600.0, places=3)

    def test_success_and_plain_payloads_pass_through(self) -> None:
        backend = self._backend()
        for payload in (
            json.dumps({"o": "add", "v": SUCCESS_TOOL_MESSAGE}),
            json.dumps({"type": "message_marker", "marker": "user_visible_token"}),
            '{"v1"}',
            "[DONE]",
            "not-json-at-all",
            "",
        ):
            backend._raise_if_tool_refusal(payload)

    def test_poll_scan_finds_refusal_in_conversation_document(self) -> None:
        backend = self._backend()
        document = {
            "mapping": {
                "a": {"message": {"author": {"role": "user"}, "content": {"content_type": "text"}}},
                "b": {"message": SUCCESS_TOOL_MESSAGE["message"]},
                "c": {"message": COOLDOWN_TOOL_MESSAGE["message"]},
            }
        }
        classified = backend._find_tool_refusal_in_conversation(document)
        self.assertIsNotNone(classified)
        self.assertEqual(classified[0], "cooldown")

    def test_poll_scan_returns_none_for_clean_document(self) -> None:
        backend = self._backend()
        document = {"mapping": {"b": {"message": SUCCESS_TOOL_MESSAGE["message"]}}}
        self.assertIsNone(backend._find_tool_refusal_in_conversation(document))


class GateProfileTests(unittest.TestCase):
    def test_gate_extracted_from_blocked_features(self) -> None:
        # Verbatim shape captured live from conversation/init.
        blocked = [{
            "name": "image_gen",
            "resets_after": "2026-10-05T08:13:43.226963+00:00",
            "resets_after_text": "9小时 内",
            "limit": 25.0,
            "description": "你当前的图片生成次数已用完。",
            "call_to_action": ["get_business"],
            "upsell_context_id": "image_gen",
        }]
        gate = OpenAIBackendAPI._extract_image_gate(blocked)
        self.assertIsNotNone(gate)
        self.assertEqual(gate["limit"], 25.0)
        self.assertEqual(gate["resets_after_text"], "9小时 内")
        self.assertGreater(gate["resets_at"], 0)

    def test_empty_blocked_features_means_open_gate(self) -> None:
        # blocked_features is always present; an empty list is the open state.
        self.assertIsNone(OpenAIBackendAPI._extract_image_gate([]))
        self.assertIsNone(OpenAIBackendAPI._extract_image_gate(None))
        self.assertIsNone(OpenAIBackendAPI._extract_image_gate([{"name": "file_upload"}]))


class ParkedAccountTests(unittest.TestCase):
    def _service(self, tmp_dir: str) -> AccountService:
        return AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))

    def _account(self, **overrides) -> dict:
        base = {
            "access_token": "tok-park",
            "status": "正常",
            "quota": 20,
            "session_token": "sess",
            "type": "free",
            "email": "park@example.test",
        }
        base.update(overrides)
        return base

    def test_closed_gate_hides_positive_advertised_remainder(self) -> None:
        # The ledger keeps advertising a remainder after the gate shuts
        # (measured: remaining=20 on an account that hard-blocked at 5), so a
        # positive quota must not by itself make the account selectable.
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self._service(tmp_dir)
            service.add_account_items([self._account()])
            token = service.list_tokens()[0]
            self.assertTrue(service._is_image_account_available(service.get_account(token)))
            future = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3600))
            service.update_account(token, {"image_gate": {"name": "image_gen", "resets_at": 0,
                                                          "resets_after": future, "limit": 5.0}}, quiet=True)
            service.update_account(token, {"image_gate_park_until": time.time() + 3600}, quiet=True)
            self.assertFalse(service._is_image_account_available(service.get_account(token)))

    def test_expired_park_reopens_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self._service(tmp_dir)
            service.add_account_items([self._account()])
            token = service.list_tokens()[0]
            service.update_account(token, {"image_gate_park_until": time.time() - 5}, quiet=True)
            self.assertTrue(service._is_image_account_available(service.get_account(token)))

    def test_park_does_not_change_status(self) -> None:
        # Parking must never set 限流: auto_remove_rate_limited_accounts deletes
        # rate-limited credentials outright, which would silently shrink the pool.
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self._service(tmp_dir)
            service.add_account_items([self._account()])
            token = service.list_tokens()[0]
            service.park_image_account(token, until_ts=time.time() + 600, kind="quota", reason="test")
            account = service.get_account(token)
            self.assertEqual(account["status"], "正常")
            self.assertGreater(account["image_gate_park_until"], time.time())

    def test_stale_gate_without_window_does_not_park_forever(self) -> None:
        # A gate snapshot with no parseable reset must not re-arm on every read.
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self._service(tmp_dir)
            service.add_account_items([self._account()])
            token = service.list_tokens()[0]
            service.update_account(
                token,
                {"image_gate": {"name": "image_gen", "resets_at": 0, "resets_after": ""},
                 "image_gate_park_until": 0},
                quiet=True,
            )
            account = service.get_account(token)
            first = service._image_gate_park_until(account)
            self.assertGreater(first, time.time())
            # Re-reading the same snapshot must not extend the window.
            self.assertAlmostEqual(service._image_gate_park_until(account), first, places=3)

    def test_gate_scheduling_switch_disables_the_park(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self._service(tmp_dir)
            service.add_account_items([self._account()])
            token = service.list_tokens()[0]
            service.update_account(token, {"image_gate_park_until": time.time() + 3600}, quiet=True)
            account = service.get_account(token)
            with patch.dict(config.data, {"image_gate_scheduling_enabled": False}):
                self.assertEqual(service._image_gate_park_until(account), 0.0)
                self.assertTrue(service._is_image_account_available(account))
            with patch.dict(config.data, {"image_gate_scheduling_enabled": True}):
                self.assertGreater(service._image_gate_park_until(account), 0.0)

    def test_open_gate_probe_unparks_a_quota_park(self) -> None:
        # A refusal text can overstate the window (observed: refusal said 10小时
        # while blocked_features said 9小时), so a probe showing an open gate must
        # be allowed to return the credential early.
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self._service(tmp_dir)
            service.add_account_items([self._account()])
            token = service.list_tokens()[0]
            service.park_image_account(token, until_ts=time.time() + 3600, kind="quota")
            account = service.get_account(token)
            self.assertFalse(service._is_image_account_available(account))

            unparked = service._reconcile_image_park_with_probe(
                token, {"image_gate": None, "image_gate_probe_ok": True, "quota": 25}, account
            )

            self.assertTrue(unparked)
            account = service.get_account(token)
            self.assertEqual(float(account.get("image_gate_park_until") or 0), 0.0)
            self.assertTrue(service._is_image_account_available(account))

    def test_open_gate_probe_keeps_a_cooldown_park(self) -> None:
        # L3 pacing refusals consume no quota, so conversation/init still shows an
        # open gate; clearing on that signal would re-hand the account out mid-cooldown.
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self._service(tmp_dir)
            service.add_account_items([self._account()])
            token = service.list_tokens()[0]
            service.park_image_account(token, until_ts=time.time() + 3600, kind="cooldown")
            account = service.get_account(token)

            unparked = service._reconcile_image_park_with_probe(
                token, {"image_gate": None, "image_gate_probe_ok": True, "quota": 25}, account
            )

            self.assertFalse(unparked)
            account = service.get_account(token)
            self.assertFalse(service._is_image_account_available(account))
            self.assertGreater(service._image_gate_park_until(account), 0.0)

    def test_probe_that_closed_the_gate_again_keeps_the_park(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self._service(tmp_dir)
            service.add_account_items([self._account()])
            token = service.list_tokens()[0]
            service.park_image_account(token, until_ts=time.time() + 3600, kind="quota")
            account = service.get_account(token)
            future = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 7200))
            unparked = service._reconcile_image_park_with_probe(
                token,
                {"image_gate": {"name": "image_gen", "resets_at": 0, "resets_after": future},
                 "image_gate_probe_ok": True},
                account,
            )
            self.assertFalse(unparked)
            self.assertFalse(service._is_image_account_available(service.get_account(token)))

    def test_probe_without_gate_field_does_not_reopen(self) -> None:
        # A payload that simply lacks the key is not proof of an open gate.
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self._service(tmp_dir)
            service.add_account_items([self._account()])
            token = service.list_tokens()[0]
            service.park_image_account(token, until_ts=time.time() + 3600, kind="quota")
            account = service.get_account(token)
            self.assertFalse(service._reconcile_image_park_with_probe(token, {"quota": 25}, account))
            self.assertFalse(service._is_image_account_available(service.get_account(token)))

    def test_get_user_info_marks_whether_the_gate_list_was_present(self) -> None:
        # The unpark decision keys on this flag, so its contract must hold for
        # both an empty list (open, provable) and a missing key (unknown).
        backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        backend.access_token = "tok"
        backend._get_me = lambda: {"email": "a@b.test", "id": "u"}
        backend._get_default_account = lambda: {"plan_type": "free"}
        backend._get_conversation_init = lambda: {"limits_progress": [], "blocked_features": []}
        info = backend.get_user_info()
        self.assertIsNone(info["image_gate"])
        self.assertTrue(info["image_gate_probe_ok"])

        backend._get_conversation_init = lambda: {"limits_progress": []}
        info = backend.get_user_info()
        self.assertIsNone(info["image_gate"])
        self.assertFalse(info["image_gate_probe_ok"])


class FailureRetryAtTests(unittest.TestCase):
    def test_quota_uses_upstream_window_not_generic_cooldown(self) -> None:
        retry_at = ImageTaskService._failure_retry_at({
            "failure_kind": "quota", "retry_after_secs": 10 * 3600.0,
        })
        self.assertAlmostEqual(retry_at - time.time(), 10 * 3600.0, delta=5)
        self.assertGreater(retry_at - time.time(), float(config.image_transient_failure_cooldown_secs) * 2)

    def test_cooldown_uses_stated_hour(self) -> None:
        retry_at = ImageTaskService._failure_retry_at({
            "failure_kind": "cooldown", "retry_after_secs": 3600.0,
        })
        self.assertAlmostEqual(retry_at - time.time(), 3600.0, delta=5)

    def test_missing_window_falls_back_per_kind(self) -> None:
        quota_at = ImageTaskService._failure_retry_at({"failure_kind": "quota"})
        self.assertAlmostEqual(
            quota_at - time.time(), float(config.image_tool_quota_cooldown_secs), delta=5
        )
        cooldown_at = ImageTaskService._failure_retry_at({"failure_kind": "cooldown"})
        self.assertAlmostEqual(
            cooldown_at - time.time(), float(config.image_tool_short_cooldown_secs), delta=5
        )

    def test_transient_keeps_generic_cooldown(self) -> None:
        retry_at = ImageTaskService._failure_retry_at({"failure_kind": "transient"})
        self.assertAlmostEqual(
            retry_at - time.time(), ImageTaskService._account_failure_cooldown_secs(), delta=5
        )


class ToolRefusalPoolTests(unittest.TestCase):
    """Drive the real pool loop: a tool refusal must switch accounts, park the
    credential for the upstream-stated window, and never be treated as a dead
    token (the account is alive, only its image budget is spent)."""

    def _run(self, refused: dict[str, ToolQuotaError | ToolCooldownError]):
        from services.protocol.conversation import _generate_single_image

        created: list[str] = []

        class FakeBackend:
            def __init__(self, access_token: str = "", *, force_proxy: str | None = None) -> None:
                self.access_token = access_token
                self.progress_callback = None
                created.append(access_token)

            def close(self) -> None:
                return None

        def fake_stream(backend, request, index, total):
            error = refused.get(backend.access_token)
            if error is not None:
                raise error
            yield ImageOutput(
                kind="result", model=request.model, index=index, total=total,
                data=[{"url": "http://example.test/ok.png"}],
            )

        def get_token(**kwargs):
            excluded = kwargs.get("excluded_tokens") or set()
            for token in ("token-a", "token-b", "token-c"):
                if token not in excluded:
                    return token
            return ""

        with (
            patch("services.protocol.conversation.account_service") as accounts,
            patch("services.protocol.conversation.OpenAIBackendAPI", FakeBackend),
            patch("services.protocol.conversation.stream_image_outputs", fake_stream),
            patch(
                "services.protocol.conversation.proxy_settings.list_egress_candidates",
                return_value=[("global", "socks5h://live.example:1080")],
            ),
        ):
            accounts.get_available_access_token.side_effect = get_token
            accounts.get_account.side_effect = lambda token: {"email": f"{token}@example.com"}
            try:
                outputs = _generate_single_image(ConversationRequest(model="gpt-image-2", prompt="cat"), 1, 1)
                error = None
            except ImageGenerationError as exc:
                outputs, error = [], exc
        return created, outputs, error, accounts

    def test_quota_refusal_switches_account_and_parks_the_window(self) -> None:
        refused = {"token-a": ToolQuotaError("图像生成请求上限", retry_after_secs=28800.0,
                                             resets_after_text="8小时 内")}
        created, outputs, error, accounts = self._run(refused)

        self.assertIsNone(error)
        self.assertEqual(created, ["token-a", "token-b"])
        self.assertEqual(outputs[-1].data[0]["url"], "http://example.test/ok.png")
        # 额度耗尽不是废号：不刷新、不删号
        accounts.remove_invalid_token.assert_not_called()
        accounts.refresh_access_token.assert_not_called()
        accounts.mark_image_result.assert_any_call("token-a", False, release_slot=False)
        # 关键：按上游给的真实窗口停放，而不是通用 60s 冷却
        park_kwargs = accounts.park_image_account.call_args.kwargs
        self.assertEqual(park_kwargs["kind"], "quota")
        self.assertAlmostEqual(
            park_kwargs["until_ts"] - time.time(), 28800.0, delta=10
        )

    def test_cooldown_refusal_parks_short_window(self) -> None:
        refused = {"token-a": ToolCooldownError("too quickly", retry_after_secs=3600.0)}
        created, _outputs, error, accounts = self._run(refused)

        self.assertIsNone(error)
        self.assertEqual(created, ["token-a", "token-b"])
        accounts.remove_invalid_token.assert_not_called()
        park_kwargs = accounts.park_image_account.call_args.kwargs
        self.assertEqual(park_kwargs["kind"], "cooldown")
        self.assertAlmostEqual(park_kwargs["until_ts"] - time.time(), 3600.0, delta=10)

    def test_quota_on_every_account_reports_insufficient_quota(self) -> None:
        from services.config import config

        refused = {
            "token-a": ToolQuotaError("图像生成请求上限", retry_after_secs=28800.0),
            "token-b": ToolQuotaError("图像生成请求上限", retry_after_secs=28800.0),
            "token-c": ToolQuotaError("图像生成请求上限", retry_after_secs=28800.0),
        }
        with patch.dict(config.data, {"image_account_failover_retries": 2}):
            created, _outputs, error, accounts = self._run(refused)

        self.assertEqual(created, ["token-a", "token-b"])
        self.assertIsNotNone(error)
        self.assertEqual(error.status_code, 429)
        self.assertEqual(error.error_type, "insufficient_quota")
        self.assertEqual(error.code, "tool_quota_exhausted")
        accounts.remove_invalid_token.assert_not_called()
        self.assertEqual(accounts.park_image_account.call_count, 2)

    def test_checkpoint_carries_window_to_the_task_service(self) -> None:
        """The window must survive the pool -> checkpoint -> task-service hop."""
        from services.protocol.conversation import _generate_single_image

        checkpoints: list[dict] = []
        refused = {"token-a": ToolQuotaError("图像生成请求上限", retry_after_secs=28800.0)}

        class FakeBackend:
            def __init__(self, access_token: str = "", *, force_proxy: str | None = None) -> None:
                self.access_token = access_token
                self.progress_callback = None

            def close(self) -> None:
                return None

        def fake_stream(backend, request, index, total):
            if backend.access_token in refused:
                raise refused[backend.access_token]
            yield ImageOutput(
                kind="result", model=request.model, index=index, total=total,
                data=[{"url": "http://example.test/ok.png"}],
            )

        def get_token(**kwargs):
            # token-a first so the refusal path is actually exercised, then
            # token-b to prove the request recovers on another credential.
            excluded = kwargs.get("excluded_tokens") or set()
            for token in ("token-a", "token-b"):
                if token not in excluded:
                    return token
            return ""

        with (
            patch("services.protocol.conversation.account_service") as accounts,
            patch("services.protocol.conversation.OpenAIBackendAPI", FakeBackend),
            patch("services.protocol.conversation.stream_image_outputs", fake_stream),
            patch(
                "services.protocol.conversation.proxy_settings.list_egress_candidates",
                return_value=[("global", "socks5h://live.example:1080")],
            ),
        ):
            accounts.get_available_access_token.side_effect = get_token
            accounts.get_account.side_effect = lambda token: {"email": f"{token}@example.com"}
            _generate_single_image(
                ConversationRequest(
                    model="gpt-image-2", prompt="cat",
                    checkpoint_callback=checkpoints.append,
                    progress_callback=lambda _step: None,
                ),
                1, 1,
            )

        self.assertTrue(checkpoints, "checkpoint 未上报失败账号")
        # The stream also emits bare conversation_id checkpoints, so select the
        # failure one instead of assuming ordering.
        failure_checkpoints = [c for c in checkpoints if "failure_kind" in c]
        self.assertTrue(failure_checkpoints, f"没有失败 checkpoint: {checkpoints}")
        first = failure_checkpoints[0]
        self.assertEqual(first["failure_kind"], "quota")
        self.assertAlmostEqual(first["retry_after_secs"], 28800.0, places=3)
        # And the task service turns that into an 8h park, not a 60s one.
        self.assertAlmostEqual(
            ImageTaskService._failure_retry_at(first) - time.time(), 28800.0, delta=10
        )


if __name__ == "__main__":
    unittest.main()
