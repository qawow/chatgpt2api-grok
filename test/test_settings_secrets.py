"""设置接口的密钥泄漏与回写回归测试。

覆盖三个此前无测试的行为：
1. ``GET /api/settings`` 不得回传任何明文凭据；
2. 前端整体回传（settings/store.ts 的 ``{...config}``）不得把掩码写回存储；
3. 来自 .env 的密钥不得被 ``update()`` 固化进 config.json —— 那会顺带进备份包。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import system
from services.config import ConfigStore

AUTH_HEADERS = {"Authorization": "Bearer chatgpt2api"}

PROXY_URL = "socks5h://user:hunter2@192.0.2.19:7890"
AI_REVIEW_KEY = "sk-review-plaintext"


def _store(**overrides: object) -> tuple[ConfigStore, Path]:
    path = Path(tempfile.mkdtemp()) / "config.json"
    data: dict[str, object] = {
        "auth-key": "test-auth",
        "proxy": PROXY_URL,
        "ai_review": {"enabled": True, "api_key": AI_REVIEW_KEY, "base_url": "https://review.example"},
        "doubao": {},
        "zhitu360": {},
        "waifu2x": {},
    }
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")
    return ConfigStore(path), path


class SettingsRedactionTests(unittest.TestCase):
    def test_get_redacts_legacy_proxy_credentials(self) -> None:
        store, _ = _store()
        self.assertNotIn("hunter2", json.dumps(store.get()))
        self.assertEqual(store.get()["proxy"], "socks5h://[REDACTED]@192.0.2.19:7890")

    def test_get_masks_ai_review_api_key(self) -> None:
        store, _ = _store()
        ai_review = store.get()["ai_review"]
        self.assertEqual(ai_review["api_key"], "********")
        self.assertTrue(ai_review["has_api_key"])
        self.assertNotIn(AI_REVIEW_KEY, json.dumps(store.get()))

    def test_get_reports_proxy_verbatim_apart_from_redaction(self) -> None:
        """脱敏不得顺带规范化：get() 报告存储原值，get_proxy_settings() 才 strip。"""
        store, _ = _store(proxy="  http://plain.example:8080  ")
        self.assertEqual(store.get()["proxy"], "  http://plain.example:8080  ")
        self.assertEqual(store.get_proxy_settings(), "http://plain.example:8080")

    def test_saving_masked_values_preserves_stored_secrets(self) -> None:
        """前端把整个 config 回传时，掩码/占位不能覆盖掉真凭据。"""
        store, path = _store()
        store.update(dict(store.get()))
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["proxy"], PROXY_URL)
        self.assertEqual(raw["ai_review"]["api_key"], AI_REVIEW_KEY)

    def test_editing_around_redacted_credentials_is_rejected(self) -> None:
        """改了主机却留着 [REDACTED]：此前会静默换回原地址，修改直接丢失。"""
        store, path = _store()
        with self.assertRaisesRegex(ValueError, "全局代理"):
            store.update({"proxy": "socks5h://[REDACTED]@10.0.0.9:7890"})
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["proxy"], PROXY_URL)

    def test_runtime_proxy_edit_with_placeholder_names_the_field(self) -> None:
        store, _ = _store(proxy_runtime={"proxy_url": "http://user:hunter2@10.0.0.1:8118"})
        runtime = store.get()["proxy_runtime"]
        self.assertEqual(runtime["proxy_url"], "http://[REDACTED]@10.0.0.1:8118")
        runtime["proxy_url"] = "http://[REDACTED]@10.0.0.2:8118"
        with self.assertRaisesRegex(ValueError, "清障代理 URL"):
            store.update({"proxy_runtime": runtime})

    def test_colon_shorthand_proxy_is_redacted_and_round_trips(self) -> None:
        """设置页推荐的 主机:端口:账号:密码 写法此前绕过了 URL 脱敏，明文回传。"""
        shorthand = "192.0.2.19:7890:user:hunter2"
        store, path = _store(proxy=shorthand)
        self.assertEqual(store.get()["proxy"], "http://[REDACTED]@192.0.2.19:7890")
        self.assertNotIn("hunter2", json.dumps(store.get()))
        store.update(dict(store.get()))
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["proxy"], shorthand)

    def test_env_only_secrets_are_not_persisted(self) -> None:
        env = {
            "DOUBAO_COOKIES": "sessionid=from-env",
            "WAIFU2X_CAPSOLVER_KEY": "cap-from-env",
            "ZHITU360_COOKIES": "zhitu-from-env",
        }
        with mock.patch.dict("os.environ", env):
            store, path = _store()
            # 读取时 env 仍然优先
            self.assertEqual(store.get_doubao_settings()["cookies"], "sessionid=from-env")
            self.assertEqual(store.get_waifu2x_settings()["capsolver_key"], "cap-from-env")

            store.update(dict(store.get()))
            raw = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(raw["doubao"]["cookies"], "")
        self.assertEqual(raw["waifu2x"]["capsolver_key"], "")
        self.assertEqual(raw["zhitu360"]["cookies"], "")
        self.assertNotIn("from-env", json.dumps(raw))

    def test_explicitly_entered_secrets_still_persist(self) -> None:
        """env 隔离不能误伤用户在设置页真填进去的值。"""
        store, path = _store()
        store.update({"doubao": {"cookies": "typed-by-user"}})
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["doubao"]["cookies"], "typed-by-user")

    def test_secret_key_lists_cover_every_masked_field(self) -> None:
        """掩码清单与还原清单必须同源，否则又会漏掉一个 solve_url。"""
        from services import config as config_module

        for keys, getter, sanitize in (
            (config_module._WAIFU2X_SECRET_KEYS, "get_waifu2x_settings", "_sanitize_waifu2x_settings"),
            (config_module._DOUBAO_SECRET_KEYS, "get_doubao_settings", None),
            (config_module._ZHITU360_SECRET_KEYS, "get_zhitu360_settings", None),
        ):
            store, _ = _store()
            settings = getattr(store, getter)()
            for key in keys:
                self.assertIn(key, settings, f"{getter} 缺少被掩码的字段 {key}")
            if sanitize:
                masked = getattr(ConfigStore, sanitize)(dict(settings, **{k: "x" for k in keys}))
                for key in keys:
                    self.assertEqual(masked[key], "********")


class HealthDisclosureTests(unittest.TestCase):
    """/health 无鉴权可达，因此不能带存储路径 / DB 连接串 / 代理拓扑。"""

    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(system.create_router("9.9.9"))
        self.client = TestClient(app)

    def test_anonymous_health_omits_storage_and_proxy(self) -> None:
        body = self.client.get("/health", params={"format": "json"}).json()
        self.assertEqual(body["version"], "9.9.9")
        self.assertIn("accounts", body)
        self.assertNotIn("storage", body)
        self.assertNotIn("proxy_runtime", body)

    def test_admin_health_includes_storage_and_proxy(self) -> None:
        body = self.client.get("/health", params={"format": "json"}, headers=AUTH_HEADERS).json()
        self.assertIn("storage", body)
        self.assertIn("proxy_runtime", body)

    def test_anonymous_health_still_answers_liveness(self) -> None:
        response = self.client.get("/health", params={"format": "json"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(response.json()["status"], {"ok", "degraded"})


class StorageHealthRedactionTests(unittest.TestCase):
    """health_check() 的异常分支曾直接 str(e)，把 token / DSN 带进 /health。"""

    def test_git_backend_scrubs_token_from_error(self) -> None:
        from services.storage.git_storage import GitStorageBackend

        backend = GitStorageBackend.__new__(GitStorageBackend)
        backend.token = "ghp_SUPERSECRET"
        backend.repo_url = "https://github.com/acme/private.git"
        message = (
            "Cmd('git') failed: git clone -v "
            "https://ghp_SUPERSECRET@github.com/acme/private.git /tmp/x"
        )
        redacted = backend._redact(RuntimeError(message))
        self.assertNotIn("ghp_SUPERSECRET", redacted)
        self.assertIn("****", redacted)

    def test_database_backend_scrubs_password_from_error(self) -> None:
        from services.storage.database_storage import DatabaseStorageBackend

        backend = DatabaseStorageBackend.__new__(DatabaseStorageBackend)
        backend.database_url = "postgresql://admin:p4ssw0rd@db.internal:5432/pool"
        message = "could not connect to postgresql://admin:p4ssw0rd@db.internal:5432/pool"
        redacted = backend._redact(RuntimeError(message))
        self.assertNotIn("p4ssw0rd", redacted)
        self.assertIn("****", redacted)


if __name__ == "__main__":
    unittest.main()
