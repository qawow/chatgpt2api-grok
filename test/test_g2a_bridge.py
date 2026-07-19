"""G2A (grokcli2api-go) config + payload helpers."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.g2a_service import (
    G2AClient,
    G2AConfig,
    _account_to_cliproxy_payload,
    _extract_credential_list,
    _normalize_base_url,
    sanitize_g2a_server,
)


class G2AConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "g2a.json"
        self.cfg = G2AConfig(store_file=self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_list_sanitize(self):
        server = self.cfg.add_server(
            name="local",
            base_url="http://127.0.0.1:8088/",
            admin_key="secret-admin",
        )
        self.assertEqual(server["base_url"], "http://127.0.0.1:8088")
        public = sanitize_g2a_server(server)
        assert public is not None
        self.assertNotIn("admin_key", public)
        self.assertTrue(public["has_admin_key"])
        listed = self.cfg.list_servers()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["admin_key"], "secret-admin")

    def test_update_keeps_key_when_empty(self):
        server = self.cfg.add_server(name="a", base_url="http://x", admin_key="k1")
        updated = self.cfg.update_server(server["id"], {"admin_key": "", "name": "b"})
        assert updated is not None
        self.assertEqual(updated["admin_key"], "k1")
        self.assertEqual(updated["name"], "b")

    def test_strips_v1_suffix_and_stores_proxy(self):
        server = self.cfg.add_server(
            name="remote",
            base_url="http://10.0.0.2:8088/v1",
            admin_key="k",
            proxy="socks5h://127.0.0.1:1080",
        )
        self.assertEqual(server["base_url"], "http://10.0.0.2:8088")
        self.assertEqual(server["proxy"], "socks5h://127.0.0.1:1080")


class G2APayloadTest(unittest.TestCase):
    def test_cliproxy_payload(self):
        payload = _account_to_cliproxy_payload(
            {
                "access_token": "at",
                "refresh_token": "rt",
                "id_token": "id",
                "email": "u@x.ai",
                "base_url": "https://api.x.ai/v1",
            }
        )
        self.assertEqual(payload["type"], "xai")
        self.assertEqual(payload["access_token"], "at")
        self.assertIn("X-XAI-Token-Auth", payload["headers"])

    def test_extract_list_variants(self):
        items = _extract_credential_list(
            {"credentials": [{"id": "abc123", "email": "a@b.c", "disabled": False}]}
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "abc123")

    def test_normalize_base_url(self):
        self.assertEqual(_normalize_base_url("http://h:8088/v1/"), "http://h:8088")
        self.assertEqual(
            _normalize_base_url("http://h:8088/v1/admin/credentials"),
            "http://h:8088",
        )


class G2AClientTest(unittest.TestCase):
    def test_upload_posts_json_bytes_direct(self):
        client = G2AClient({"base_url": "http://example.invalid", "admin_key": "adm"})
        self.assertFalse(client._session.trust_env)
        self.assertEqual(client._session.proxies.get("http"), None)
        with mock.patch.object(client._session, "request") as req:
            resp = mock.Mock()
            resp.status_code = 200
            resp.text = '{"created":true}'
            resp.json.return_value = {"created": True}
            req.return_value = resp
            out = client.upload_credential(
                {"access_token": "at", "refresh_token": "rt", "email": "e@x"}
            )
            self.assertTrue(out.get("created"))
            args, kwargs = req.call_args
            self.assertEqual(args[0], "POST")
            self.assertTrue(str(args[1]).endswith("/v1/admin/credentials"))
            self.assertIn("Authorization", kwargs["headers"])
            self.assertIsInstance(kwargs["data"], (bytes, bytearray))
            self.assertIn(b'"type": "xai"', kwargs["data"])

    def test_connect_only_proxy_error_message(self):
        client = G2AClient({"base_url": "http://127.0.0.1:7890", "admin_key": "adm"})
        with mock.patch.object(client._session, "request") as req:
            resp = mock.Mock()
            resp.status_code = 405
            resp.text = (
                "<!DOCTYPE HTML><html><body><h1>Error response</h1>"
                "<p>Error code: 405</p><p>Message: only CONNECT supported.</p></body></html>"
            )
            req.return_value = resp
            with self.assertRaises(Exception) as ctx:
                client.list_credentials()
            msg = str(ctx.exception).lower()
            self.assertIn("connect-only proxy", msg)
            self.assertIn("405", msg)

    def test_explicit_proxy_enables_session_proxies(self):
        client = G2AClient(
            {
                "base_url": "http://example.invalid",
                "admin_key": "adm",
                "proxy": "http://127.0.0.1:8888",
            }
        )
        self.assertEqual(client._session.proxies.get("http"), "http://127.0.0.1:8888")
        self.assertFalse(client._session.trust_env)


if __name__ == "__main__":
    unittest.main()
