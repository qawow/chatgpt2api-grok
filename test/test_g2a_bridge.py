"""G2A (grokcli2api-go) config + payload helpers."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.g2a_service import (
    G2ABridgeService,
    G2AClient,
    G2AClientError,
    G2AConfig,
    _account_to_cliproxy_payload,
    _extract_credential_list,
    _normalize_base_url,
    credential_to_account_row,
    parse_remote_account_id,
    remote_account_id,
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
            api_key="client-api",
            prefer_for_image=True,
        )
        self.assertEqual(server["base_url"], "http://127.0.0.1:8088")
        public = sanitize_g2a_server(server)
        assert public is not None
        self.assertNotIn("admin_key", public)
        self.assertNotIn("api_key", public)
        self.assertTrue(public["has_admin_key"])
        self.assertTrue(public["has_api_key"])
        self.assertTrue(public["can_proxy_image"])
        self.assertTrue(public["prefer_for_image"])
        listed = self.cfg.list_servers()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["admin_key"], "secret-admin")
        self.assertEqual(listed[0]["api_key"], "client-api")

    def test_update_keeps_key_when_empty(self):
        server = self.cfg.add_server(name="a", base_url="http://x", admin_key="k1", api_key="a1")
        updated = self.cfg.update_server(server["id"], {"admin_key": "", "api_key": "", "name": "b"})
        assert updated is not None
        self.assertEqual(updated["admin_key"], "k1")
        self.assertEqual(updated["api_key"], "a1")
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

    def test_list_image_proxy_servers_respects_prefer_flag(self):
        self.cfg.add_server(
            name="img",
            base_url="http://img:8088",
            admin_key="k",
            prefer_for_image=True,
        )
        off = self.cfg.add_server(
            name="off",
            base_url="http://off:8088",
            admin_key="k2",
            prefer_for_image=False,
        )
        servers = self.cfg.list_image_proxy_servers()
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["name"], "img")
        self.cfg.update_server(off["id"], {"prefer_for_image": True})
        self.assertEqual(len(self.cfg.list_image_proxy_servers()), 2)


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

    def test_generate_image_uses_api_key_and_normalizes(self):
        client = G2AClient(
            {
                "id": "s1",
                "name": "remote",
                "base_url": "http://example.invalid",
                "admin_key": "adm",
                "api_key": "client-key",
            }
        )
        with mock.patch.object(client._session, "request") as req:
            resp = mock.Mock()
            resp.status_code = 200
            resp.text = '{"created":1,"data":[{"b64_json":"abc123"}]}'
            resp.json.return_value = {"created": 1, "data": [{"b64_json": "abc123"}]}
            req.return_value = resp
            out = client.generate_image(prompt="a cat", model="grok-2-image", n=1)
            self.assertEqual(out["data"][0]["b64_json"], "abc123")
            self.assertEqual(out["_meta"]["upstream"], "g2a")
            args, kwargs = req.call_args
            self.assertEqual(args[0], "POST")
            self.assertTrue(str(args[1]).endswith("/v1/images/generations"))
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer client-key")
            self.assertNotIn("X-Admin-Key", kwargs["headers"])


class G2ABridgeStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "g2a.json"
        self.cfg = G2AConfig(store_file=self.path)
        self.bridge = G2ABridgeService(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_remote_account_id_roundtrip(self):
        token = remote_account_id("srv1", "cred9")
        self.assertEqual(token, "g2a:srv1:cred9")
        self.assertEqual(parse_remote_account_id(token), ("srv1", "cred9"))
        self.assertIsNone(parse_remote_account_id("not-remote"))

    def test_credential_to_account_row_readonly(self):
        row = credential_to_account_row(
            {"id": "s1", "name": "n1", "base_url": "http://h"},
            {"id": "c1", "email": "a@b.c", "disabled": False, "status": "active"},
        )
        self.assertEqual(row["access_token"], "g2a:s1:c1")
        self.assertTrue(row["readonly"])
        self.assertEqual(row["provider"], "g2a")
        self.assertEqual(row["status"], "正常")

    def test_list_remote_pool_status_maps_items(self):
        server = self.cfg.add_server(
            name="local",
            base_url="http://127.0.0.1:8088",
            admin_key="adm",
        )
        with mock.patch.object(G2AClient, "list_credentials") as list_creds:
            list_creds.return_value = {
                "items": [
                    {"id": "c1", "email": "one@x", "disabled": False, "status": "ok"},
                    {"id": "c2", "email": "two@x", "disabled": True, "status": "disabled"},
                ]
            }
            result = self.bridge.list_remote_pool_status()
        self.assertEqual(result["total"], 2)
        self.assertTrue(result["readonly"])
        self.assertEqual(result["items"][0]["email"], "one@x")
        self.assertEqual(result["items"][1]["status"], "禁用")
        self.assertEqual(result["servers"][0]["id"], server["id"])
        self.assertTrue(result["servers"][0]["ok"])

    def test_has_image_proxy_false_without_servers(self):
        self.assertFalse(self.bridge.has_image_proxy())

    def test_generate_image_bridge_tries_proxy(self):
        server = self.cfg.add_server(
            name="img",
            base_url="http://127.0.0.1:8088",
            admin_key="adm",
            prefer_for_image=True,
        )
        with mock.patch.object(G2AClient, "generate_image") as gen:
            gen.return_value = {
                "created": 1,
                "data": [{"b64_json": "zzz"}],
                "_meta": {"upstream": "g2a", "server_id": server["id"]},
            }
            out = self.bridge.generate_image(prompt="hi", model="grok-2-image")
        self.assertEqual(out["data"][0]["b64_json"], "zzz")
        gen.assert_called_once()

    def test_generate_image_bridge_no_server(self):
        with self.assertRaises(G2AClientError):
            self.bridge.generate_image(prompt="hi")


class GrokImageRoutePreferG2ATest(unittest.TestCase):
    def test_handle_prefers_g2a_when_proxy_available(self):
        from services.protocol import grok_v1_image_generations as mod

        with mock.patch.object(mod.g2a_bridge, "has_image_proxy", return_value=True), mock.patch.object(
            mod.g2a_bridge,
            "generate_image",
            return_value={
                "created": 11,
                "data": [{"b64_json": "img"}],
                "_meta": {"server_id": "s1"},
            },
        ), mock.patch.object(mod, "_persist_urls", side_effect=lambda items, **_: items):
            out = mod.handle({"prompt": "cat", "model": "grok-2-image", "n": 1})
        self.assertEqual(out["data"][0]["b64_json"], "img")
        self.assertEqual(out["_grok_meta"]["upstream"], "g2a")

    def test_handle_falls_back_local_on_g2a_error(self):
        from services.protocol import grok_v1_image_generations as mod

        with mock.patch.object(mod.g2a_bridge, "has_image_proxy", return_value=True), mock.patch.object(
            mod.g2a_bridge,
            "generate_image",
            side_effect=G2AClientError("remote down", status=502),
        ), mock.patch.object(mod.grok_account_service, "count", return_value=1), mock.patch.object(
            mod,
            "_handle_via_local_pool",
            return_value={"created": 2, "data": [{"b64_json": "local"}], "_grok_meta": {"upstream": "local"}},
        ) as local:
            out = mod.handle({"prompt": "cat", "model": "grok-2-image"})
        self.assertEqual(out["_grok_meta"]["upstream"], "local")
        local.assert_called_once()


if __name__ == "__main__":
    unittest.main()
