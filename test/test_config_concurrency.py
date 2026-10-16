from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from services.config import ConfigStore
from services import image_tags_service


class ConfigConcurrencyTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.store = ConfigStore(self.root / "config.json")

    def test_concurrent_partial_updates_survive_in_memory_and_on_disk(self):
        workers = 12
        barrier = threading.Barrier(workers)

        def update(index):
            barrier.wait(timeout=5)
            self.store.update({f"fixture_{index}": {"value": index}})
            self.store.get()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(update, range(workers)))
        persisted = json.loads(self.store.path.read_text())
        for index in range(workers):
            self.assertEqual(persisted[f"fixture_{index}"], {"value": index})
            self.assertEqual(self.store.get()[f"fixture_{index}"], {"value": index})

    def test_nested_settings_are_independent_snapshots(self):
        payload = {"grok": {"nested": [1]}, "custom": {"nested": [2]}}
        self.store.update(payload)
        payload["grok"]["nested"].append(3)
        snapshot = self.store.get()
        snapshot["custom"]["nested"].append(4)
        grok = self.store.get_grok_settings()
        grok["nested"].append(5)
        self.assertEqual(self.store.get_grok_settings()["nested"], [1])
        self.assertEqual(self.store.get()["custom"]["nested"], [2])

    def test_storage_backend_is_created_once_under_contention(self):
        barrier = threading.Barrier(8)
        backend = object()

        def read(_):
            barrier.wait(timeout=5)
            return self.store.get_storage_backend()

        with mock.patch("services.storage.factory.create_storage_backend", return_value=backend) as factory:
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(read, range(8)))
        factory.assert_called_once()
        self.assertTrue(all(result is backend for result in results))

    def test_tag_cleanup_preserves_concurrent_unrelated_updates(self):
        workers = 10
        barrier = threading.Barrier(workers + 1)
        with mock.patch.object(image_tags_service, "TAGS_FILE", self.root / "tags.json"):
            image_tags_service.set_tags("expired.png", ["old"])

            def update(index):
                barrier.wait(timeout=5)
                image_tags_service.set_tags(f"live_{index}.png", [str(index)])

            def cleanup():
                barrier.wait(timeout=5)
                image_tags_service.remove_tags_batch(["expired.png"])

            with ThreadPoolExecutor(max_workers=workers + 1) as executor:
                futures = [executor.submit(update, i) for i in range(workers)] + [executor.submit(cleanup)]
                for future in futures:
                    future.result(timeout=10)
            tags = image_tags_service.load_tags()
        self.assertNotIn("expired.png", tags)
        self.assertEqual(len(tags), workers)
