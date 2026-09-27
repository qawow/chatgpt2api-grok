from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from services.storage.base import StorageBackend, StorageLoadError
from utils.atomic import atomic_write_json


class JSONStorageBackend(StorageBackend):
    """本地 JSON 文件存储后端"""

    def __init__(self, file_path: Path, auth_keys_path: Path | None = None):
        self.file_path = file_path
        self.auth_keys_path = auth_keys_path or file_path.with_name("auth_keys.json")
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.auth_keys_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _read_json(file_path: Path) -> Any:
        """Read stored JSON, distinguishing "absent" from "unreadable".

        A missing file legitimately means "nothing stored yet". Anything else —
        corrupt JSON, a truncated write, an unexpected top-level type, an I/O
        error — must not degrade to empty: callers save over what they loaded,
        so returning [] here is what turns a damaged file into a lost one.
        """
        if not file_path.exists():
            return None
        try:
            raw = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageLoadError(f"无法读取 {file_path}：{exc}") from exc
        if not raw.strip():
            # An empty file is the one ambiguous case; treat it as "no data"
            # since atomic_write_json never produces one.
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StorageLoadError(
                f"{file_path} 内容已损坏，无法解析：{exc}。"
                f"请先修复或移走该文件再启动，否则保存操作会覆盖掉原有数据。"
            ) from exc

    @classmethod
    def _load_json_list(cls, file_path: Path) -> list[dict[str, Any]]:
        data = cls._read_json(file_path)
        if data is None:
            return []
        if not isinstance(data, list):
            raise StorageLoadError(f"{file_path} 顶层结构应为列表，实际是 {type(data).__name__}")
        return data

    @staticmethod
    def _save_json_list(file_path: Path, items: list[dict[str, Any]]) -> None:
        atomic_write_json(file_path, items)

    def load_accounts(self) -> list[dict[str, Any]]:
        """从 JSON 文件加载账号数据"""
        return self._load_json_list(self.file_path)

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """保存账号数据到 JSON 文件"""
        self._save_json_list(self.file_path, accounts)

    def load_auth_keys(self) -> list[dict[str, Any]]:
        """从 JSON 文件加载鉴权密钥数据"""
        data = self._read_json(self.auth_keys_path)
        if data is None:
            return []
        if isinstance(data, dict):
            data = data.get("items")
        if not isinstance(data, list):
            raise StorageLoadError(f"{self.auth_keys_path} 结构异常，未找到密钥列表")
        return data

    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """保存鉴权密钥数据到 JSON 文件"""
        atomic_write_json(self.auth_keys_path, {"items": auth_keys})

    def health_check(self) -> dict[str, Any]:
        """健康检查"""
        try:
            # 检查文件是否可读写
            if self.file_path.exists():
                self.file_path.read_text(encoding="utf-8")
            return {
                "status": "healthy",
                "backend": "json",
                "file_exists": self.file_path.exists(),
                "file_path": str(self.file_path),
                "auth_keys_file_exists": self.auth_keys_path.exists(),
                "auth_keys_file_path": str(self.auth_keys_path),
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "backend": "json",
                "error": str(e),
            }

    def get_backend_info(self) -> dict[str, Any]:
        """获取存储后端信息"""
        return {
            "type": "json",
            "description": "本地 JSON 文件存储",
            "file_path": str(self.file_path),
            "file_exists": self.file_path.exists(),
            "auth_keys_file_path": str(self.auth_keys_path),
            "auth_keys_file_exists": self.auth_keys_path.exists(),
        }
