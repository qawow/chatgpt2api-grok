"""Account manager must stay readable while image leases are active."""
from __future__ import annotations

import tempfile
import asyncio
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx

from api import accounts
from services.account_service import AccountService
from services.storage.json_storage import JSONStorageBackend


class AccountListApiTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.service = AccountService(JSONStorageBackend(Path(scratch.name) / "accounts.json"))
        self.service.add_account_items([{
            "access_token": "fixture-account", "quota": 10, "session_token": "fixture-session",
        }])
        patcher = mock.patch.object(accounts, "account_service", self.service)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(accounts.create_router())
        self.client = TestClient(app)

    def inflight(self):
        response = self.client.get("/api/accounts", headers={"Authorization": "Bearer chatgpt2api"})
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()["items"][0]["image_inflight"]
        self.assertIsInstance(value, int)
        return value

    def test_list_while_real_slot_is_acquired_and_after_release(self):
        token = self.service._acquire_next_candidate_token()
        try:
            self.assertIsInstance(self.service._image_inflight[token], list)
            self.assertEqual(self.inflight(), 1)
        finally:
            self.service.release_image_slot(token)
        self.assertEqual(self.inflight(), 0)

    def test_only_unexpired_leases_are_counted(self):
        now = time.monotonic()
        self.service._image_inflight["fixture-account"] = [now - 10, now + 60, now + 120]
        self.assertEqual(self.inflight(), 2)
        self.service._image_inflight["fixture-account"] = [now - 10]
        self.assertEqual(self.inflight(), 0)

    def test_legacy_integer_counters_remain_readable(self):
        self.service._image_inflight["fixture-account"] = 2
        self.assertEqual(self.inflight(), 2)


class AccountEndpointConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_for_account_io_keeps_other_endpoints_responsive(self):
        started = threading.Event()
        release = threading.Event()

        def slow_list():
            started.set()
            if not release.wait(timeout=3):
                raise RuntimeError("fixture timed out waiting for release")
            return []

        app = FastAPI()
        app.include_router(accounts.create_router())

        @app.get("/probe")
        async def probe():
            return {"ready": True}

        with mock.patch.object(accounts.account_service, "list_accounts", side_effect=slow_list):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
                pending = asyncio.create_task(client.get("/api/accounts", headers={"Authorization": "Bearer chatgpt2api"}))
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 1))
                    response = await asyncio.wait_for(client.get("/probe"), timeout=1)
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(pending.done())
                finally:
                    release.set()
                self.assertEqual((await pending).status_code, 200)

if __name__ == "__main__":
    unittest.main()
