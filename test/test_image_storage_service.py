from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi.responses import FileResponse
from PIL import Image

from services import image_service
from services.image_storage_service import ImageStorageError, ImageStorageService, WebDAVClient
from utils.image_media import sniff_image_media


def image_bytes(image_format: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color=(255, 0, 0)).save(buffer, format=image_format)
    return buffer.getvalue()


def png_bytes() -> bytes:
    return image_bytes("PNG")


def animated_gif_bytes() -> bytes:
    buffer = io.BytesIO()
    first = Image.new("RGB", (2, 2), color=(255, 0, 0))
    second = Image.new("RGB", (2, 2), color=(0, 0, 255))
    first.save(
        buffer, format="GIF", save_all=True, append_images=[second],
        duration=[100, 200], loop=2, disposal=2,
    )
    return buffer.getvalue()


def image_fixtures() -> list[tuple[bytes, str, str]]:
    return [
        (png_bytes(), "image/png", ".png"),
        (image_bytes("JPEG"), "image/jpeg", ".jpg"),
        (image_bytes("WEBP"), "image/webp", ".webp"),
        (animated_gif_bytes(), "image/gif", ".gif"),
    ]


class FakeWebDAVClient:
    uploaded: dict[str, bytes] = {}
    content_types: dict[str, str] = {}
    deleted: list[str] = []
    instances: list["FakeWebDAVClient"] = []

    def __init__(self, _settings):
        self.closed = False
        self.instances.append(self)

    def close(self):
        self.closed = True

    def put(self, rel: str, payload: bytes, content_type: str = "image/png") -> str:
        self.uploaded[rel] = payload
        self.content_types[rel] = content_type
        return f"https://dav.example.test/{rel}"

    def get(self, rel: str) -> bytes:
        return self.uploaded[rel]

    def delete(self, rel: str) -> bool:
        self.deleted.append(rel)
        self.uploaded.pop(rel, None)
        self.content_types.pop(rel, None)
        return True

    def test(self) -> dict[str, object]:
        self.put(".chatgpt2api_webdav_test.txt", b"chatgpt2api webdav test\n")
        self.delete(".chatgpt2api_webdav_test.txt")
        return {"ok": True, "status": 200, "error": None}


class ImageMediaTests(unittest.TestCase):
    def test_supported_media_signatures(self):
        for payload, mime_type, extension in image_fixtures():
            with self.subTest(mime_type=mime_type):
                self.assertEqual(sniff_image_media(payload), (mime_type, extension))

    def test_both_gif_signature_versions(self):
        for signature in (b"GIF87a", b"GIF89a"):
            with self.subTest(signature=signature):
                self.assertEqual(sniff_image_media(signature), ("image/gif", ".gif"))

    def test_unknown_and_short_payloads_keep_png_fallback(self):
        for payload in (b"", b"opaque", b"GIF", b"GIF90a", b"RIFF", b"RIFF\x00\x00\x00\x00WAVE"):
            with self.subTest(payload=payload):
                self.assertEqual(sniff_image_media(payload), ("image/png", ".png"))


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
        FakeWebDAVClient.content_types = {}
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

    def _assert_gif_animation(self, payload: bytes):
        with Image.open(io.BytesIO(payload)) as image:
            self.assertEqual(image.format, "GIF")
            self.assertEqual(image.n_frames, 2)
            self.assertEqual(image.info.get("loop"), 2)
            durations = []
            for index in range(image.n_frames):
                image.seek(index)
                durations.append(image.info.get("duration"))
            self.assertEqual(durations, [100, 200])

    def test_png_relative_path_rule_is_unchanged(self):
        payload = png_bytes()
        date_parts = {"%Y": "2026", "%m": "11", "%d": "05"}
        with (
            mock.patch("services.image_storage_service.time.time", return_value=1234567890),
            mock.patch("services.image_storage_service.time.strftime", side_effect=date_parts.__getitem__),
        ):
            rel = self.service().make_relative_path(payload)
        self.assertEqual(rel, f"2026/11/05/1234567890_{hashlib.md5(payload).hexdigest()}.png")

    def test_unknown_payload_keeps_png_suffix_and_original_bytes(self):
        payload = b"opaque legacy payload"
        service = self.service()
        stored = service.save(payload)
        self.assertEqual(Path(stored.rel).suffix, ".png")
        self.assertEqual(service.get_bytes(stored.rel), payload)

    def test_local_saves_preserve_each_media_format(self):
        service = self.service()
        for payload, mime_type, extension in image_fixtures():
            with self.subTest(mime_type=mime_type):
                stored = service.save(payload, "http://app.test")
                self.assertEqual(Path(stored.rel).suffix, extension)
                self.assertEqual((self.images_dir / stored.rel).read_bytes(), payload)
                self.assertEqual(service.get_bytes(stored.rel), payload)
                self.assertTrue(service.exists(stored.rel))
                self.assertEqual(stored.url, f"http://app.test/images/{stored.rel}")
                item = service._load_index()[stored.rel]
                self.assertNotIn("mime_type", item)
                self.assertNotIn("content_type", item)

    def test_webdav_saves_preserve_each_media_format_and_content_type(self):
        self.settings.update({"enabled": True, "mode": "webdav", "webdav_url": "https://dav.example.test"})
        service = self.service()
        with mock.patch("services.image_storage_service.WebDAVClient", FakeWebDAVClient):
            for payload, mime_type, extension in image_fixtures():
                with self.subTest(mime_type=mime_type):
                    stored = service.save(payload)
                    self.assertEqual(Path(stored.rel).suffix, extension)
                    self.assertEqual(FakeWebDAVClient.uploaded[stored.rel], payload)
                    self.assertEqual(FakeWebDAVClient.content_types[stored.rel], mime_type)
                    self.assertEqual(service.get_bytes(stored.rel), payload)
                    self.assertFalse((self.images_dir / stored.rel).exists())
                    self.assertTrue(service.exists(stored.rel))
        self.assertTrue(all(client.closed for client in FakeWebDAVClient.instances))

    def test_sync_uses_payload_mime_without_renaming_legacy_paths(self):
        self.settings.update({"enabled": True, "mode": "both", "webdav_url": "https://dav.example.test"})
        self.images_dir.mkdir(parents=True)
        fixtures = {f"sync{extension}": (payload, mime_type)
                    for payload, mime_type, extension in image_fixtures()}
        fixtures["legacy.png"] = (animated_gif_bytes(), "image/gif")
        for rel, (payload, _mime_type) in fixtures.items():
            (self.images_dir / rel).write_bytes(payload)
        service = self.service()
        with mock.patch("services.image_storage_service.WebDAVClient", FakeWebDAVClient):
            result = service.sync_all()
        self.assertEqual(result, {"uploaded": len(fixtures), "skipped": 0, "failed": 0})
        self.assertEqual(set(service._load_index()), set(fixtures))
        for rel, (payload, mime_type) in fixtures.items():
            with self.subTest(rel=rel):
                self.assertEqual(FakeWebDAVClient.uploaded[rel], payload)
                self.assertEqual(FakeWebDAVClient.content_types[rel], mime_type)
                self.assertEqual((self.images_dir / rel).read_bytes(), payload)
        self.assertTrue((self.images_dir / "legacy.png").is_file())
        self.assertTrue(all(client.closed for client in FakeWebDAVClient.instances))

    def test_both_mode_preserves_gif_frames_durations_and_loop(self):
        self.settings.update({"enabled": True, "mode": "both", "webdav_url": "https://dav.example.test"})
        payload = animated_gif_bytes()
        with mock.patch("services.image_storage_service.WebDAVClient", FakeWebDAVClient):
            stored = self.service().save(payload)
        self.assertEqual(Path(stored.rel).suffix, ".gif")
        self.assertEqual(FakeWebDAVClient.content_types[stored.rel], "image/gif")
        for saved_payload in ((self.images_dir / stored.rel).read_bytes(), FakeWebDAVClient.uploaded[stored.rel]):
            self.assertEqual(saved_payload, payload)
            self.assertEqual(hashlib.sha256(saved_payload).digest(), hashlib.sha256(payload).digest())
            self._assert_gif_animation(saved_payload)

    def test_local_http_headers_use_payload_mime(self):
        service = self.service()
        with (
            mock.patch("services.image_service.config", self.mock_config),
            mock.patch("services.image_service.image_storage_service", service),
        ):
            for payload, mime_type, _extension in image_fixtures():
                stored = service.save(payload)
                for handler in (image_service.get_image_response, image_service.get_image_download_response):
                    with self.subTest(mime_type=mime_type, handler=handler.__name__):
                        with mock.patch.object(service, "get_bytes", side_effect=AssertionError("local must stream")) as get_bytes:
                            response = handler(stored.rel)
                        get_bytes.assert_not_called()
                        self.assertIsInstance(response, FileResponse)
                        self.assertEqual(response.headers["content-type"], mime_type)
                        self.assertEqual(response.headers["access-control-allow-origin"], "*")
                        self.assertEqual(Path(response.path), (self.images_dir / stored.rel).resolve())
                        self.assertEqual((self.images_dir / stored.rel).read_bytes(), payload)

    def test_local_header_sniff_reads_only_twelve_bytes(self):
        path = mock.MagicMock(spec=Path)
        handle = path.open.return_value.__enter__.return_value
        handle.read.return_value = animated_gif_bytes()[:12]
        self.assertEqual(image_service._local_image_media_type(path), "image/gif")
        path.open.assert_called_once_with("rb")
        handle.read.assert_called_once_with(12)

    def test_remote_http_headers_use_payload_mime_and_fetch_once(self):
        self.settings.update({"enabled": True, "mode": "webdav", "webdav_url": "https://dav.example.test"})
        service = self.service()
        with (
            mock.patch("services.image_storage_service.WebDAVClient", FakeWebDAVClient),
            mock.patch("services.image_service.config", self.mock_config),
            mock.patch("services.image_service.image_storage_service", service),
        ):
            for payload, mime_type, _extension in image_fixtures():
                stored = service.save(payload)
                for handler in (image_service.get_image_response, image_service.get_image_download_response):
                    with self.subTest(mime_type=mime_type, handler=handler.__name__):
                        with mock.patch.object(service, "get_bytes", wraps=service.get_bytes) as get_bytes:
                            response = handler(stored.rel)
                        get_bytes.assert_called_once_with(stored.rel)
                        self.assertEqual(response.headers["content-type"], mime_type)
                        self.assertEqual(response.headers["access-control-allow-origin"], "*")
                        self.assertEqual(response.body, payload)
                        if mime_type == "image/gif":
                            self._assert_gif_animation(response.body)
        self.assertTrue(all(client.closed for client in FakeWebDAVClient.instances))

    def test_local_legacy_png_url_serves_original_gif(self):
        rel = "2026/11/05/legacy.png"
        payload = animated_gif_bytes()
        path = self.images_dir / rel
        path.parent.mkdir(parents=True)
        path.write_bytes(payload)
        service = self.service()
        with (
            mock.patch("services.image_service.config", self.mock_config),
            mock.patch("services.image_service.image_storage_service", service),
        ):
            response = image_service.get_image_response(rel)
            download = image_service.get_image_download_response(rel)
        self.assertEqual(response.headers["content-type"], "image/gif")
        self.assertEqual(download.headers["content-type"], "image/gif")
        self.assertEqual(download.headers["content-disposition"], 'attachment; filename="legacy.png"')
        self.assertEqual(Path(response.path), path.resolve())
        self.assertEqual(Path(download.path), path.resolve())
        self.assertEqual(path.read_bytes(), payload)
        self._assert_gif_animation(path.read_bytes())
        self.assertEqual(service.list_items("http://app.test")[0]["url"], f"http://app.test/images/{rel}")

    def test_remote_legacy_png_url_serves_original_gif_without_index_changes(self):
        self.settings.update({"enabled": True, "mode": "webdav", "webdav_url": "https://dav.example.test"})
        rel = "2026/11/05/legacy.png"
        payload = animated_gif_bytes()
        item = {"rel": rel, "path": rel, "name": "legacy.png", "size": len(payload),
                "storage": "webdav", "local": False, "webdav": True,
                "remote_url": f"https://dav.example.test/{rel}"}
        service = self.service()
        service._save_index({rel: item})
        FakeWebDAVClient.uploaded[rel] = payload
        with (
            mock.patch("services.image_storage_service.WebDAVClient", FakeWebDAVClient),
            mock.patch("services.image_service.config", self.mock_config),
            mock.patch("services.image_service.image_storage_service", service),
        ):
            for handler in (image_service.get_image_response, image_service.get_image_download_response):
                with self.subTest(handler=handler.__name__):
                    with mock.patch.object(service, "get_bytes", wraps=service.get_bytes) as get_bytes:
                        response = handler(rel)
                    get_bytes.assert_called_once_with(rel)
                    self.assertEqual(response.headers["content-type"], "image/gif")
                    self.assertEqual(response.body, payload)
                    self._assert_gif_animation(response.body)
                    if handler is image_service.get_image_download_response:
                        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="legacy.png"')
        self.assertEqual(service._load_index()[rel], item)
        self.assertFalse((self.images_dir / rel).exists())
        self.assertTrue(all(client.closed for client in FakeWebDAVClient.instances))

    def test_gif_listing_and_zip_download_preserve_original_bytes(self):
        service = self.service()
        payload = animated_gif_bytes()
        stored = service.save(payload, "http://app.test")
        self.assertEqual([item["rel"] for item in service.list_items("http://app.test")], [stored.rel])
        self.assertTrue(service.has_local(stored.rel))
        with (
            mock.patch("services.image_service.config", self.mock_config),
            mock.patch("services.image_service.image_storage_service", service),
        ):
            archive_bytes = image_service.download_images_zip([stored.rel])
        with zipfile.ZipFile(archive_bytes) as archive:
            self.assertEqual(archive.namelist(), [Path(stored.rel).name])
            saved_payload = archive.read(Path(stored.rel).name)
        self.assertEqual(saved_payload, payload)
        self._assert_gif_animation(saved_payload)

    def test_capacity_cleanup_dry_run_includes_gif(self):
        service = self.service()
        stored = service.save(animated_gif_bytes())
        with (
            mock.patch("services.image_service.config", self.mock_config),
            mock.patch("services.image_service.shutil.disk_usage", return_value=mock.Mock(free=0)),
        ):
            result = image_service.delete_to_target(1, dry_run=True)
        self.assertEqual(result["removed"], 1)
        self.assertTrue((self.images_dir / stored.rel).is_file())


if __name__ == "__main__":
    unittest.main()
