from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from services.image_storage_service import ImageStorageError, ImageStorageService, WebDAVClient


def png_bytes() -> bytes:
    path = Path(tempfile.gettempdir()) / "chatgpt2api-test-image.png"
    Image.new("RGB", (2, 2), color=(255, 0, 0)).save(path, format="PNG")
    return path.read_bytes()


class FakeWebDAVClient:
    uploaded: dict[str, bytes] = {}
    deleted: list[str] = []
    instances: list["FakeWebDAVClient"] = []

    def __init__(self, _settings):
        self.closed = False
        self.instances.append(self)

    def close(self):
        self.closed = True

    def put(self, rel: str, payload: bytes) -> str:
        self.uploaded[rel] = payload
        return f"https://dav.example.test/{rel}"

    def get(self, rel: str) -> bytes:
        return self.uploaded[rel]

    def delete(self, rel: str) -> bool:
        self.deleted.append(rel)
        self.uploaded.pop(rel, None)
        return True

    def test(self) -> dict[str, object]:
        self.put(".chatgpt2api_webdav_test.txt", b"chatgpt2api webdav test\n")
        self.delete(".chatgpt2api_webdav_test.txt")
        return {"ok": True, "status": 200, "error": None}


class ImageStorageServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.images_dir = self.data_dir / "images"
        self.settings = {
            "enabled": False,
            "mode": "local",
            "webdav_url": "",
            "webdav_username": "",
            "webdav_password": "",
            "webdav_root_path": "chatgpt2api/images",
            "public_base_url": "",
        }
        self.config_patcher = mock.patch("services.image_storage_service.config")
        self.mock_config = self.config_patcher.start()
        self.addCleanup(self.config_patcher.stop)
        self.mock_config.images_dir = self.images_dir
        self.mock_config.base_url = "http://app.test"
        self.mock_config.cleanup_old_images.return_value = 0
        self.mock_config.get_image_storage_settings.side_effect = lambda: dict(self.settings)
        FakeWebDAVClient.uploaded = {}
        FakeWebDAVClient.deleted = []
        FakeWebDAVClient.instances = []

    def service(self) -> ImageStorageService:
        return ImageStorageService(self.data_dir / "image_index.json")

    def test_local_mode_saves_to_local_directory(self):
        stored = self.service().save(png_bytes(), "http://app.test")

        self.assertEqual(stored.storage, "local")
        self.assertTrue((self.images_dir / stored.rel).is_file())
        self.assertEqual(stored.url, f"http://app.test/images/{stored.rel}")

    def test_webdav_mode_uploads_without_local_file(self):
        self.settings.update({
            "enabled": True,
            "mode": "webdav",
            "webdav_url": "https://dav.example.test",
            "webdav_password": "secret",
        })
        with mock.patch("services.image_storage_service.WebDAVClient", FakeWebDAVClient):
            stored = self.service().save(png_bytes(), "http://app.test")
            payload = self.service().get_bytes(stored.rel)

        self.assertEqual(stored.storage, "webdav")
        self.assertFalse((self.images_dir / stored.rel).exists())
        self.assertIn(stored.rel, FakeWebDAVClient.uploaded)
        self.assertEqual(payload, FakeWebDAVClient.uploaded[stored.rel])
        self.assertEqual(len(FakeWebDAVClient.instances), 2)
        self.assertTrue(all(client.closed for client in FakeWebDAVClient.instances))

    def test_list_items_ignores_non_image_files(self):
        image = png_bytes()
        image_path = self.images_dir / "2026" / "05" / "07" / "sample.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(image)
        (self.images_dir / ".DS_Store").write_text("not an image", encoding="utf-8")
        (self.images_dir / "2026" / ".DS_Store").write_text("not an image", encoding="utf-8")

        items = self.service().list_items("http://app.test")

        self.assertEqual([item["rel"] for item in items], ["2026/05/07/sample.png"])
        self.assertEqual(items[0]["storage"], "local")

    def test_both_mode_saves_to_local_and_webdav(self):
        self.settings.update({
            "enabled": True,
            "mode": "both",
            "webdav_url": "https://dav.example.test",
            "webdav_password": "secret",
            "public_base_url": "https://cdn.example.test/images",
        })
        with mock.patch("services.image_storage_service.WebDAVClient", FakeWebDAVClient):
            stored = self.service().save(png_bytes(), "http://app.test")

        self.assertEqual(stored.storage, "both")
        self.assertTrue((self.images_dir / stored.rel).is_file())
        self.assertIn(stored.rel, FakeWebDAVClient.uploaded)
        self.assertEqual(stored.url, f"https://cdn.example.test/images/{stored.rel}")

    def test_test_webdav_writes_and_deletes_probe_file(self):
        self.settings.update({
            "enabled": True,
            "mode": "webdav",
            "webdav_url": "https://dav.example.test",
            "webdav_password": "secret",
        })
        with mock.patch("services.image_storage_service.WebDAVClient", FakeWebDAVClient):
            result = self.service().test_webdav()

        self.assertTrue(result["ok"])
        self.assertIn(".chatgpt2api_webdav_test.txt", FakeWebDAVClient.deleted)
        self.assertTrue(FakeWebDAVClient.instances[0].closed)

    def test_delete_and_bulk_sync_close_clients(self):
        self.settings.update({"enabled": True, "mode": "both", "webdav_url": "https://dav.example.test"})
        service = self.service()
        with mock.patch("services.image_storage_service.WebDAVClient", FakeWebDAVClient):
            stored = service.save(png_bytes())
            self.assertTrue(service.delete(stored.rel))
            local = self.images_dir / "sync.jpg"
            local.write_bytes(png_bytes())
            self.assertEqual(service.sync_all()["uploaded"], 1)
        self.assertEqual(len(FakeWebDAVClient.instances), 3)
        self.assertTrue(all(client.closed for client in FakeWebDAVClient.instances))

    def test_webdav_clients_close_on_each_failure_path(self):
        self.settings.update({"enabled": True, "mode": "webdav", "webdav_url": "https://dav.example.test"})
        rel = "2026/01/01/failure.png"
        for operation in ("save", "get_bytes", "delete", "test_webdav", "sync_all"):
            with self.subTest(operation=operation):
                service = self.service()
                service._save_index({rel: {"webdav": operation != "sync_all"}})
                path = self.images_dir / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.unlink(missing_ok=True)
                client = mock.Mock()
                for method in ("put", "get", "delete", "test"):
                    getattr(client, method).side_effect = ImageStorageError("fixture failure")
                with mock.patch("services.image_storage_service.WebDAVClient", return_value=client):
                    if operation == "sync_all":
                        path.write_bytes(png_bytes())
                        self.assertEqual(service.sync_all()["failed"], 1)
                    else:
                        with self.assertRaises(ImageStorageError):
                            getattr(service, operation)(*(
                                (png_bytes(),) if operation == "save" else
                                () if operation == "test_webdav" else (rel,)
                            ))
                client.close.assert_called_once_with()

    def test_real_webdav_probe_closes_session_once_including_invalid_urls(self):
        for url in ("https://dav.example.test", "", "invalid://dav.example.test"):
            with self.subTest(url=url):
                self.settings.update({"webdav_url": url})
                session = mock.Mock()
                with (
                    mock.patch("services.image_storage_service.requests.Session", return_value=session),
                    mock.patch.object(WebDAVClient, "put"),
                    mock.patch.object(WebDAVClient, "delete"),
                ):
                    result = self.service().test_webdav()
                self.assertEqual(result["ok"], url.startswith("https://"))
                session.close.assert_called_once_with()

    def test_sync_closes_client_when_index_write_fails(self):
        self.settings.update({"enabled": True, "mode": "webdav", "webdav_url": "https://dav.example.test"})
        service = self.service()
        client = mock.Mock()
        with (
            mock.patch("services.image_storage_service.WebDAVClient", return_value=client),
            mock.patch.object(service, "_save_index", side_effect=OSError("fixture disk error")),
        ):
            with self.assertRaises(OSError):
                service.sync_all()
        client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
