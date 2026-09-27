"""存储加载失败不得退化成空列表。

调用方（AuthService.create_key、AccountService._save_accounts）都是「读出来再存回去」，
所以把读取失败当成「没有数据」，下一次保存就会把文件覆盖成空 —— 损坏变成丢失。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.auth_service import AuthService
from services.storage.base import StorageLoadError
from services.storage.json_storage import JSONStorageBackend


class JSONStorageLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.accounts = self.dir / "accounts.json"
        self.auth_keys = self.dir / "auth_keys.json"

    def _backend(self) -> JSONStorageBackend:
        return JSONStorageBackend(self.accounts)

    def test_missing_files_are_empty_not_an_error(self) -> None:
        self.assertEqual(self._backend().load_accounts(), [])
        self.assertEqual(self._backend().load_auth_keys(), [])

    def test_empty_file_is_treated_as_no_data(self) -> None:
        self.accounts.write_text("", encoding="utf-8")
        self.assertEqual(self._backend().load_accounts(), [])

    def test_corrupt_accounts_file_raises(self) -> None:
        self.accounts.write_text('[{"access_token": "a"', encoding="utf-8")
        with self.assertRaises(StorageLoadError):
            self._backend().load_accounts()

    def test_corrupt_auth_keys_file_raises(self) -> None:
        self.auth_keys.write_text('{"items": [{"id": tru', encoding="utf-8")
        with self.assertRaises(StorageLoadError):
            self._backend().load_auth_keys()

    def test_unexpected_top_level_type_raises(self) -> None:
        self.accounts.write_text('{"not": "a list"}', encoding="utf-8")
        with self.assertRaises(StorageLoadError):
            self._backend().load_accounts()


class AuthKeyWipeRegressionTests(unittest.TestCase):
    """create_key 会先 reload 再 append 再整表写回。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.backend = JSONStorageBackend(self.dir / "accounts.json")
        self.auth_keys = self.dir / "auth_keys.json"

    def test_creating_a_key_keeps_the_existing_ones(self) -> None:
        service = AuthService(self.backend)
        service.create_key(name="k1", role="user")
        service.create_key(name="k2", role="user")
        stored = json.loads(self.auth_keys.read_text(encoding="utf-8"))
        self.assertEqual(len(stored["items"]), 2)

    def test_corrupt_store_refuses_to_load_instead_of_wiping(self) -> None:
        service = AuthService(self.backend)
        service.create_key(name="k1", role="user")
        original = self.auth_keys.read_text(encoding="utf-8")

        self.auth_keys.write_text(original[: len(original) // 2], encoding="utf-8")

        with self.assertRaises(StorageLoadError):
            AuthService(self.backend)
        # 关键断言：文件没有被覆盖，运维还能手工修回来
        self.assertEqual(
            self.auth_keys.read_text(encoding="utf-8"),
            original[: len(original) // 2],
        )


if __name__ == "__main__":
    unittest.main()
