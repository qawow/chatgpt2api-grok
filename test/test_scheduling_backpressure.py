"""P0 scheduling hardening regressions.

Covers: image slot leases (expiry reclaim, bounded wait, legacy counters),
text soft-failure penalty window, forced-refresh convergence on soft errors,
and persistence of cleared refresh markers.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import PropertyMock

from services.account_service import AccountService
from services.config import config
from services.storage.json_storage import JSONStorageBackend
from utils.helper import anonymize_token

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


def _service(tmp_dir: str, accounts: list[dict]) -> AccountService:
    service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
    service.add_account_items(accounts)
    return service


class ImageSlotLeaseTests(unittest.TestCase):
    def test_expired_lease_is_reclaimed_instead_of_wedging_picker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir, [
                {"access_token": "live", "status": "正常", "quota": 5, "session_token": "s"},
            ])
            # A crashed worker leaked a lease whose deadline already passed.
            service._image_inflight["live"] = [time.monotonic() - 1.0]
            picked = service._acquire_next_candidate_token()
            self.assertEqual(picked, "live")
            self.assertEqual(len(service._image_inflight["live"]), 1)
            service.release_image_slot("live")
            self.assertNotIn("live", service._image_inflight)

    def test_wait_budget_exhausted_raises_busy_instead_of_hanging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir, [
                {"access_token": "live", "status": "正常", "quota": 5, "session_token": "s"},
            ])
            service._image_inflight["live"] = [time.monotonic() + 300]
            with (
                mock.patch.object(type(config), "image_account_concurrency", new_callable=PropertyMock, return_value=1),
                mock.patch.object(service, "_image_slot_wait_secs", return_value=0.05),
            ):
                started = time.monotonic()
                with self.assertRaises(RuntimeError) as ctx:
                    service._acquire_next_candidate_token()
                self.assertLess(time.monotonic() - started, 5.0)
            self.assertIn("wait budget exhausted", str(ctx.exception))

    def test_release_dequeues_one_lease_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir, [
                {"access_token": "live", "status": "正常", "quota": 5, "session_token": "s"},
            ])
            first = service._acquire_next_candidate_token()
            service._image_inflight[first].append(time.monotonic() + 300)  # simulate 2 in flight
            service.release_image_slot(first)
            self.assertEqual(len(service._image_inflight[first]), 1)
            service.release_image_slot(first)
            self.assertNotIn(first, service._image_inflight)
            service.release_image_slot(first)  # over-release must be harmless

    def test_legacy_int_counter_still_counts_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir, [
                {"access_token": "live", "status": "正常", "quota": 5, "session_token": "s"},
            ])
            service._image_inflight["live"] = 1  # type: ignore[assignment]
            with mock.patch.object(type(config), "image_account_concurrency", new_callable=PropertyMock, return_value=1):
                self.assertEqual(service._list_available_candidate_tokens(), [])
            service.release_image_slot("live")
            self.assertNotIn("live", service._image_inflight)


class TextPenaltyTests(unittest.TestCase):
    def test_soft_failure_holds_account_out_of_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir, [
                {"access_token": "tok-a", "status": "正常", "quota": 1, "session_token": "s"},
                {"access_token": "tok-b", "status": "正常", "quota": 1, "session_token": "s"},
            ])
            with mock.patch.object(service, "refresh_access_token", side_effect=lambda t, **kw: t):
                service.note_text_penalty("tok-a", "curl: (28) Connection timed out after 12074 milliseconds")
                picks = {service.get_text_access_token() for _ in range(6)}
            self.assertEqual(picks, {"tok-b"})

    def test_hard_auth_error_is_not_penalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir, [
                {"access_token": "tok-a", "status": "正常", "quota": 1, "session_token": "s"},
            ])
            service.note_text_penalty("tok-a", "token invalidated (/backend-api/me)")
            self.assertEqual(service._text_penalty, {})

    def test_penalties_expire(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir, [
                {"access_token": "tok-a", "status": "正常", "quota": 1, "session_token": "s"},
                {"access_token": "tok-b", "status": "正常", "quota": 1, "session_token": "s"},
            ])
            with mock.patch.object(service, "refresh_access_token", side_effect=lambda t, **kw: t):
                service.note_text_penalty("tok-a", "curl: (28) Connection timed out")
                service._text_penalty["tok-a"] = time.monotonic() - 1
                picks = {service.get_text_access_token() for _ in range(6)}
            self.assertIn("tok-a", picks)

    def test_soft_error_no_longer_forces_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir, [
                {"access_token": "tok-a", "status": "正常", "quota": 1, "session_token": "s"},
            ])
            service.update_account(
                "tok-a",
                {"last_refresh_error": "curl: (28) Connection timed out after 12074 milliseconds"},
                quiet=True,
            )
            seen: list[bool] = []

            def spy(token: str, force: bool = False, event: str = "") -> str:
                seen.append(force)
                return token

            with mock.patch.object(service, "refresh_access_token", side_effect=spy):
                with mock.patch.object(AccountService, "_token_needs_refresh", return_value=False):
                    service.get_text_access_token()
            self.assertEqual(seen, [False])


class RefreshSuccessPersistenceTests(unittest.TestCase):
    def test_cleared_markers_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "accounts.json"
            service = AccountService(JSONStorageBackend(path))
            service.add_account_items([
                {"access_token": "tok-a", "status": "正常", "quota": 1, "session_token": "s"},
            ])
            service.update_account(
                "tok-a",
                {"last_refresh_error": "boom", "last_refresh_error_at": "2026-01-01T00:00:00+00:00"},
                quiet=True,
            )
            service._record_refresh_success("tok-a")
            reloaded = AccountService(JSONStorageBackend(path))
            acc = reloaded.get_account("tok-a") or {}
            self.assertIsNone(acc.get("last_refresh_error"))


PROXY_A = "socks5h://u:p@1.1.1.1:1080"
PROXY_B = "socks5h://u:p@2.2.2.2:1080"


class EgressSpreadTests(unittest.TestCase):
    def _three_accounts(self, service: AccountService) -> None:
        service.update_account("e1", {"proxy": PROXY_A}, quiet=True)
        service.update_account("e2", {"proxy": PROXY_A}, quiet=True)
        service.update_account("e3", {"proxy": PROXY_B}, quiet=True)

    def test_pick_spreads_away_from_busy_egress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir, [
                {"access_token": "e1", "status": "正常", "quota": 5, "session_token": "s"},
                {"access_token": "e2", "status": "正常", "quota": 5, "session_token": "s"},
                {"access_token": "e3", "status": "正常", "quota": 5, "session_token": "s"},
            ])
            self._three_accounts(service)
            with service._image_slot_condition:
                # e1 already runs a generation: egress A is loaded, so the next
                # pick must prefer e3 (egress B) over same-egress e2.
                service._image_inflight["e1"] = [time.monotonic() + 300]
                picked = service._pick_image_candidate_token(["e2", "e3"])
            self.assertEqual(picked, "e3")

    def test_single_egress_pool_keeps_per_account_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir, [
                {"access_token": "e1", "status": "正常", "quota": 5, "session_token": "s"},
                {"access_token": "e2", "status": "正常", "quota": 5, "session_token": "s"},
            ])
            service.update_account("e1", {"proxy": PROXY_A}, quiet=True)
            service.update_account("e2", {"proxy": PROXY_A}, quiet=True)
            with service._image_slot_condition:
                service._image_inflight["e1"] = [time.monotonic() + 300]
                # e2 shares the busy egress but must still be selectable.
                with mock.patch.object(type(config), "image_account_concurrency", new_callable=PropertyMock, return_value=3):
                    self.assertIn("e2", service._list_available_candidate_tokens())


class ProbeCacheTests(unittest.TestCase):
    def test_concurrent_picks_share_one_remote_probe(self) -> None:
        import threading

        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir, [
                {"access_token": "live", "status": "正常", "quota": 5, "session_token": "s"},
            ])
            calls = {"n": 0}

            def fake_fetch(token: str, event: str = "") -> dict:
                calls["n"] += 1
                time.sleep(0.1)
                return {"access_token": token, "quota": 5, "status": "正常"}

            with mock.patch.object(service, "fetch_remote_info", side_effect=fake_fetch):
                results: list[dict] = []
                threads = [threading.Thread(target=lambda: results.append(service._probe_remote_account("live", "t"))) for _ in range(4)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
            self.assertEqual(calls["n"], 1)
            self.assertEqual(len(results), 4)

    def test_failure_is_shared_but_message_and_first_error_survive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir, [
                {"access_token": "live", "status": "正常", "quota": 5, "session_token": "s"},
            ])
            calls = {"n": 0}

            def fake_fetch(token: str, event: str = "") -> dict:
                calls["n"] += 1
                raise ValueError("token invalidated (/backend-api/me)")

            with mock.patch.object(service, "fetch_remote_info", side_effect=fake_fetch):
                with self.assertRaises(ValueError):
                    service._probe_remote_account("live", "t1")
                with self.assertRaises(RuntimeError) as ctx:
                    service._probe_remote_account("live", "t2")
            self.assertIn("token invalidated", str(ctx.exception))
            self.assertEqual(calls["n"], 1)


class ParkClockAlignmentTests(unittest.TestCase):
    def test_task_retry_at_never_earlier_than_account_park(self) -> None:
        import hashlib

        from services import account_service as account_service_module
        from services.image_task_service import ImageTaskService

        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir, [
                {"access_token": "tok-a", "status": "正常", "quota": 1, "session_token": "s"},
            ])
            park_until = time.time() + 3600
            service.update_account("tok-a", {"image_gate_park_until": park_until}, quiet=True)
            token_hash = hashlib.sha256(b"tok-a").hexdigest()
            self.assertAlmostEqual(service.image_park_until_for_token_hash(token_hash), park_until, places=3)
            with mock.patch.object(account_service_module, "account_service", service):
                retry_at = ImageTaskService._failure_retry_at({
                    "failure_kind": "cooldown",
                    "retry_after_secs": 60,
                    "failed_token_hash": token_hash,
                })
                self.assertGreaterEqual(retry_at, park_until)
                retry_unknown = ImageTaskService._failure_retry_at({
                    "failure_kind": "cooldown",
                    "retry_after_secs": 60,
                    "failed_token_hash": "0" * 64,
                })
            self.assertLess(retry_unknown, park_until)
            self.assertGreater(retry_unknown, time.time())


class FifoFairnessTests(unittest.TestCase):
    def test_forbidden_tokens_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir, [
                {"access_token": "f1", "status": "正常", "quota": 5, "session_token": "s"},
                {"access_token": "f2", "status": "正常", "quota": 5, "session_token": "s"},
            ])
            with service._image_slot_condition:
                self.assertEqual(service._pick_image_candidate_token(["f1", "f2"], forbidden={"f1"}), "f2")
                self.assertEqual(service._pick_image_candidate_token(["f1", "f2"], forbidden={"f1", "f2"}), "")

    def test_elder_waiter_gets_the_slot_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir, [
                {"access_token": "only", "status": "正常", "quota": 5, "session_token": "s"},
            ])
            order: list[str] = []
            errors: list[Exception] = []

            def waiter(name: str) -> None:
                try:
                    token = service._acquire_next_candidate_token()
                    order.append(name)
                    service.release_image_slot(token)
                except Exception as exc:  # noqa: BLE001 - recorded for assertion
                    errors.append(exc)

            with service._image_slot_condition:
                service._image_inflight["only"] = [time.monotonic() + 300]
            with mock.patch.object(service, "_image_slot_wait_secs", return_value=12.0):
                thread_a = threading.Thread(target=waiter, args=("a",))
                thread_a.start()
                time.sleep(0.3)  # A registers its ticket first
                thread_b = threading.Thread(target=waiter, args=("b",))
                thread_b.start()
                time.sleep(0.3)
                with service._image_slot_condition:
                    service._image_inflight.pop("only", None)  # occupant finishes
                    service._image_slot_condition.notify_all()
                thread_a.join(20)
                thread_b.join(20)
            self.assertEqual(errors, [])
            self.assertEqual(order, ["a", "b"])


class SkipDiagnosticsTests(unittest.TestCase):
    def test_first_failing_filter_reported_per_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir, [
                {"access_token": "ok", "status": "正常", "quota": 5, "session_token": "s"},
                {"access_token": "parker", "status": "正常", "quota": 5, "session_token": "s"},
                {"access_token": "dry", "status": "正常", "quota": 0, "refresh_token": "rt"},
                {"access_token": "planb", "status": "正常", "quota": 5, "session_token": "s", "type": "plus"},
            ])
            service.update_account("parker", {"image_gate_park_until": time.time() + 600}, quiet=True)
            diag = {row["reason"] for row in service.image_pick_skip_diag(plan_types=("plus",))}
            self.assertIn("gate_park", diag)
            self.assertIn("no_quota", diag)
            self.assertIn("plan_filter", diag)
            ready_tokens = {
                anonymize_token("ok")
                for row in service.image_pick_skip_diag()
                if row["token"] == anonymize_token("ok")
            }
            self.assertEqual(ready_tokens, set())  # healthy accounts are not listed


class PoolPressureEventTests(unittest.TestCase):
    def test_empty_pick_wakes_replenish_and_notify_is_throttled(self) -> None:
        from services.gpt_register_service import gpt_register_service

        saved_at = gpt_register_service._pool_pressure_at
        gpt_register_service._pool_pressure.clear()
        gpt_register_service._pool_pressure_at = 0.0
        try:
            self.assertFalse(gpt_register_service.wait_pool_pressure(0.05))
            with tempfile.TemporaryDirectory() as tmp_dir:
                service = _service(tmp_dir, [])  # empty pool
                with self.assertRaises(RuntimeError):
                    service.get_available_access_token()
            self.assertTrue(gpt_register_service.wait_pool_pressure(0.05))
            # throttled re-notify within 60s must not re-set the event
            gpt_register_service.notify_pool_pressure()
            self.assertFalse(gpt_register_service.wait_pool_pressure(0.05))
        finally:
            gpt_register_service._pool_pressure.clear()
            gpt_register_service._pool_pressure_at = saved_at


class PlanSplitTests(unittest.TestCase):
    def test_image_available_counts_split_by_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(tmp_dir, [
                {"access_token": "p1", "status": "正常", "quota": 3, "session_token": "s", "type": "plus"},
                {"access_token": "f1", "status": "正常", "quota": 3, "session_token": "s", "type": "free"},
                {"access_token": "dead", "status": "异常", "quota": 3, "session_token": "s", "type": "plus"},
            ])
            counts = service.count_image_available_by_plan()
            self.assertEqual(counts.get("plus"), 1)
            self.assertEqual(counts.get("free"), 1)


if __name__ == "__main__":
    unittest.main()
