"""Quota-based auto-replenish trigger (OR with the count-based water level)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.account_service import AccountService
from services.gpt_register_service import (
    DEFAULT_SETTINGS,
    GptRegisterService,
    normalize_settings,
)
from services.storage.json_storage import JSONStorageBackend

# Scoped to this module only — see the same note in
# test/test_account_image_capabilities.py. A module-level setdefault() here
# leaked "test-auth" into every later test module and 401'd the API tests.
_AUTH_KEY_PATCHER: mock._patch_dict | None = None


def setUpModule() -> None:
    global _AUTH_KEY_PATCHER
    _AUTH_KEY_PATCHER = mock.patch.dict(os.environ, {"CHATGPT2API_AUTH_KEY": "test-auth"})
    _AUTH_KEY_PATCHER.start()


def tearDownModule() -> None:
    global _AUTH_KEY_PATCHER
    if _AUTH_KEY_PATCHER is not None:
        _AUTH_KEY_PATCHER.stop()
        _AUTH_KEY_PATCHER = None


class _FakeConfig:
    """Minimal GptRegisterConfig stand-in: returns raw settings to be normalized."""

    def __init__(self, **overrides: object):
        self.settings = {"push_enabled": True, **overrides}

    def get(self) -> dict:
        return self.settings


def _service(**overrides: object) -> GptRegisterService:
    svc = GptRegisterService(config_store=_FakeConfig(**overrides))
    # Do not read or run real registration jobs in these tests.
    svc._jobs = {}
    svc.has_active_job = lambda: False  # type: ignore[method-assign]
    svc.start_job = mock.Mock(return_value={"job_id": "fake-job"})  # type: ignore[method-assign]
    return svc


class NormalizeSettingsTests(unittest.TestCase):
    def test_default_is_disabled(self) -> None:
        self.assertEqual(DEFAULT_SETTINGS["auto_replenish_min_total_quota"], 0)
        self.assertEqual(normalize_settings({})["auto_replenish_min_total_quota"], 0)

    def test_clamps_and_preserves(self) -> None:
        self.assertEqual(normalize_settings({"auto_replenish_min_total_quota": 25})[
            "auto_replenish_min_total_quota"], 25)
        self.assertEqual(normalize_settings({"auto_replenish_min_total_quota": -5})[
            "auto_replenish_min_total_quota"], 0)
        self.assertEqual(normalize_settings({"auto_replenish_min_total_quota": 99999})[
            "auto_replenish_min_total_quota"], 10000)


class TotalQuotaTests(unittest.TestCase):
    def test_sums_quota_over_image_available_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_account_items([
                {"access_token": "live1", "status": "正常", "quota": 5},
                {"access_token": "live2", "status": "正常", "quota": 0,
                 "session_token": "sess", "type": "free"},
                {"access_token": "abnormal", "status": "异常", "quota": 100},
                {"access_token": "disabled", "status": "禁用", "quota": 100},
            ])
            # abnormal/disabled quotas must not count; live2 is
            # bootstrap-available but contributes quota 0.
            self.assertEqual(service.total_image_available_quota(), 5)
            self.assertEqual(service.count_image_available_accounts(), 2)


class QuotaTriggerTests(unittest.TestCase):
    def _patch_pool(self, available: int, total_quota: int) -> mock._patch:
        patcher = mock.patch("services.account_service.account_service")
        started = patcher.start()
        started.count_image_available_accounts.return_value = available
        started.total_image_available_quota.return_value = total_quota
        self.addCleanup(patcher.stop)
        return started

    def test_quota_short_triggers_despite_enough_accounts(self) -> None:
        # Count water level is stocked (4 >= target 4) but total quota is low.
        self._patch_pool(available=4, total_quota=3)
        svc = _service(auto_replenish_enabled=True, auto_replenish_min_total_quota=20)
        result = svc.maybe_replenish_pool()
        self.assertEqual(result["action"], "started")
        self.assertEqual(result["reason"], "low_quota")
        self.assertEqual(result["total_quota"], 3)
        self.assertEqual(result["min_total_quota"], 20)
        svc.start_job.assert_called_once()
        self.assertEqual(svc.start_job.call_args.kwargs.get("trigger"), "auto_replenish")

    def test_stocked_when_both_satisfied(self) -> None:
        self._patch_pool(available=4, total_quota=60)
        svc = _service(auto_replenish_enabled=True, auto_replenish_min_total_quota=20)
        result = svc.maybe_replenish_pool()
        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["reason"], "stocked")

    def test_quota_disabled_falls_back_to_count_only(self) -> None:
        # min_total_quota=0 (default) → quota never triggers; stocked by count.
        self._patch_pool(available=4, total_quota=0)
        svc = _service(auto_replenish_enabled=True)
        result = svc.maybe_replenish_pool()
        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["reason"], "stocked")

    def test_quota_and_count_are_or(self) -> None:
        # Quota is fine but available is below target → still triggers.
        self._patch_pool(available=1, total_quota=500)
        svc = _service(auto_replenish_enabled=True, auto_replenish_min_total_quota=20)
        result = svc.maybe_replenish_pool()
        self.assertEqual(result["action"], "started")
        self.assertEqual(result["reason"], "below_min")

    def test_spacing_still_applies_to_quota_trigger(self) -> None:
        # Non-emergency (available >= min) quota replenish must respect spacing.
        self._patch_pool(available=3, total_quota=3)
        svc = _service(
            auto_replenish_enabled=True,
            auto_replenish_min_total_quota=20,
            auto_replenish_spacing_secs=600,
        )
        svc._jobs["j1"] = {
            "job_id": "j1",
            "status": "done",
            "added": 1,
            "finished_at": "2099-01-01T00:00:00+00:00",  # far future → elapsed < 0
            "trigger": "auto_replenish",
        }
        result = svc.maybe_replenish_pool()
        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["reason"], "spacing")
        svc.start_job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
