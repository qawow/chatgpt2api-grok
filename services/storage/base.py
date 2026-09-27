from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

_URL_CREDENTIALS_RE = re.compile(r"(?<=://)[^/\s@]+(?=@)")


class StorageLoadError(RuntimeError):
    """Raised when existing stored data is present but unreadable.

    Deliberately distinct from "no data yet": callers treat an empty list as
    authoritative and will happily save over it, so a parse failure that
    degrades to ``[]`` destroys the file on the next write.
    """


def redact_url_credentials(value: Any) -> str:
    """Strip ``scheme://credentials@host`` secrets out of arbitrary text.

    health_check() error branches surface raw driver/GitPython exceptions, and
    those carry the full command line or DSN — including the token we spliced
    into the remote URL. /health is unauthenticated, so anything returned from
    here is world-readable.
    """
    return _URL_CREDENTIALS_RE.sub("****", str(value))


class StorageBackend(ABC):
    """抽象存储后端基类"""

    @abstractmethod
    def load_accounts(self) -> list[dict[str, Any]]:
        """加载所有账号数据"""
        pass

    @abstractmethod
    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """保存所有账号数据"""
        pass

    @abstractmethod
    def load_auth_keys(self) -> list[dict[str, Any]]:
        """加载所有鉴权密钥数据"""
        pass

    @abstractmethod
    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """保存所有鉴权密钥数据"""
        pass

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """健康检查，返回存储后端状态"""
        pass

    @abstractmethod
    def get_backend_info(self) -> dict[str, Any]:
        """获取存储后端信息"""
        pass
