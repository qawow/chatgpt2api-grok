from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from services import config as config_module
from services import image_service, image_storage_service, image_tags_service


class LocalImageCleanupTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.patch(config_module, "DATA_DIR", self.root / "data")
        self.store = config_module.ConfigStore(self.root / "config.json")
        self.store.update({"image_retention_days": 1})
        self.storage = image_storage_service.ImageStorageService(self.root / "index.json")
        self.patch(image_service, "config", self.store)
        self.patch(image_storage_service, "config", self.store)
        self.patch(image_service, "image_storage_service", self.storage)
        self.patch(image_storage_service, "image_storage_service", self.storage)
        self.patch(image_tags_service, "TAGS_FILE", self.root / "tags.json")
        self.webdav = self.patch(image_storage_service, "WebDAVClient")
        self.addCleanup(self.webdav.assert_not_called)

    def patch(self, target, attr, value=mock.DEFAULT):
        patcher = mock.patch.object(target, attr, value)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def image(self, rel, *, days=2, megabytes=0):
        path = self.store.images_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as output:
            output.write(b"fixture")
            if megabytes:
                output.truncate(megabytes * 1024 * 1024)
        timestamp = time.time() - days * 86400
        os.utime(path, (timestamp, timestamp))
        thumb = self.store.image_thumbnails_dir / f"{rel}.png"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"thumbnail")
        return path, thumb

    def test_retention_preserves_remote_copies_and_remote_tags(self):
        local, local_thumb = self.image("2026/01/01/local.jpg")
        both, both_thumb = self.image("2026/01/01/both.webp")
        recent, _ = self.image("2026/01/02/recent.png", days=0)
        self.storage._save_index({
            "2026/01/01/local.jpg": {"local": True, "webdav": False},
            "2026/01/01/both.webp": {"local": True, "webdav": True, "storage": "both"},
            "remote.png": {"local": False, "webdav": True},
        })
        image_tags_service.save_tags({"2026/01/01/local.jpg": ["local"], "2026/01/01/both.webp": ["archive"], "remote.png": ["remote"]})
        self.assertEqual(self.store.cleanup_old_images(force=True), 2)
        self.assertFalse(local.exists())
        self.assertFalse(both.exists())
        self.assertFalse(local_thumb.exists())
        self.assertFalse(both_thumb.exists())
        self.assertTrue(recent.exists())
        index = self.storage._load_index()
        self.assertNotIn("2026/01/01/local.jpg", index)
        self.assertEqual(index["2026/01/01/both.webp"]["storage"], "webdav")
        self.assertFalse(index["2026/01/01/both.webp"]["local"])
        self.assertIn("remote.png", index)
        self.assertEqual(image_tags_service.load_tags(), {"2026/01/01/both.webp": ["archive"], "remote.png": ["remote"]})

    def test_empty_dirs_are_removed_even_without_expired_files(self):
        empty = self.store.images_dir / "empty" / "nested"
        empty.mkdir(parents=True)
        self.assertEqual(self.store.cleanup_old_images(force=True), 0)
        self.assertFalse(empty.parent.exists())

    def test_repeated_requests_are_throttled_but_force_scans(self):
        self.assertEqual(self.store.cleanup_old_images(), 0)
        old, _ = self.image("old.png")
        self.assertEqual(self.store.cleanup_old_images(), 0)
        self.assertTrue(old.exists())
        self.assertEqual(self.store.cleanup_old_images(force=True), 1)

    def test_retention_update_invalidates_scan_throttle(self):
        self.store.update({"image_retention_days": 30})
        old, _ = self.image("old.png", days=10)
        self.assertEqual(self.store.cleanup_old_images(), 0)
        self.store.update({"image_retention_days": 1})
        self.assertEqual(self.store.cleanup_old_images(), 1)
        self.assertFalse(old.exists())

    def test_disk_cleanup_handles_all_image_extensions_and_preserves_archives(self):
        local, _ = self.image("local.jpg", days=4, megabytes=1)
        both, _ = self.image("both.webp", days=3, megabytes=1)
        newest, _ = self.image("new.png", days=2, megabytes=1)
        self.storage._save_index({"local.jpg": {"local": True}, "both.webp": {"local": True, "webdav": True}})
        image_tags_service.save_tags({"local.jpg": ["delete"], "both.webp": ["keep"]})
        with mock.patch("shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            result = image_service.delete_to_target(2)
        self.assertEqual(result["removed"], 2)
        self.assertTrue(result["done"])
        self.assertFalse(local.exists())
        self.assertFalse(both.exists())
        self.assertTrue(newest.exists())
        self.assertEqual(image_tags_service.load_tags(), {"both.webp": ["keep"]})
        self.assertEqual(self.storage._load_index()["both.webp"]["storage"], "webdav")

    def test_disk_cleanup_dry_run_preserves_files_and_metadata(self):
        path, thumb = self.image("local.png", megabytes=1)
        self.storage._save_index({"local.png": {"local": True}})
        image_tags_service.save_tags({"local.png": ["keep"]})
        before_index = self.storage.index_file.read_bytes()
        with mock.patch("shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            result = image_service.delete_to_target(1, dry_run=True)
        self.assertEqual(result["removed"], 1)
        self.assertTrue(path.exists())
        self.assertTrue(thumb.exists())
        self.assertEqual(self.storage.index_file.read_bytes(), before_index)
        self.assertEqual(image_tags_service.load_tags(), {"local.png": ["keep"]})

    def test_thumbnail_cleanup_keeps_local_and_remote_sources(self):
        self.image("local.png", days=0)
        self.storage._save_index({"remote.png": {"webdav": True}})
        remote = self.store.image_thumbnails_dir / "remote.png.png"
        orphan = self.store.image_thumbnails_dir / "missing.png.png"
        for path in (remote, orphan):
            path.write_bytes(b"thumbnail")
        self.assertEqual(image_service.cleanup_image_thumbnails(), 1)
        self.assertTrue(remote.exists())
        self.assertFalse(orphan.exists())

    def test_worker_reads_updated_free_space_threshold_each_pass(self):
        stop = mock.Mock()
        stop.wait.side_effect = [False, False, True]
        with (
            mock.patch.object(type(self.store), "image_min_free_mb", new_callable=mock.PropertyMock, side_effect=[10, 20]),
            mock.patch.object(self.store, "cleanup_old_images"),
            mock.patch.object(image_service, "cleanup_image_thumbnails"),
            mock.patch.object(image_service, "delete_to_target", return_value={}) as cleanup,
            mock.patch("shutil.disk_usage", return_value=SimpleNamespace(free=0)),
        ):
            image_service._auto_cleanup_worker(stop)
        self.assertEqual(cleanup.call_args_list, [mock.call(10), mock.call(20)])
