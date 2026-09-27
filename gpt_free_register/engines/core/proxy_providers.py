"""动态代理 IP 提供者 — 具体实现已迁移到 providers/proxy/

支持两种模式:
  1. 静态代理: 从数据库读取固定代理列表（现有逻辑）
  2. 动态代理: 从第三方 API 实时获取代理 IP

动态代理 provider 通过 provider_settings 配置；如果未配置则自动回退到静态代理池。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class BaseProxyProvider(ABC):
    """动态代理提供者基类。"""

    @abstractmethod
    def get_proxy(self) -> Optional[str]:
        """获取一个代理 URL，格式: http://host:port 或 http://user:pass@host:port。
        返回 None 表示无可用代理。"""
        ...


# ---------------------------------------------------------------------------
# Lazy re-exports for backward compatibility
# (concrete classes now live under providers/proxy/)
#
# WARNING: neither module below exists in this tree — providers/proxy/ only ships
# an __init__.py. Every dynamic-proxy provider therefore raises ModuleNotFoundError
# at creation time. The matching provider definitions are seeded with
# enabled=False (see infrastructure/provider_definitions_repository.py) so the
# settings page cannot offer a provider that is guaranteed to fail.
# ---------------------------------------------------------------------------
_LAZY_IMPORTS = {
    "ApiExtractProvider": "providers.proxy.api_extract",
    "RotatingProxyProvider": "providers.proxy.rotating_gateway",
}


def __getattr__(name: str):
    module_path = _LAZY_IMPORTS.get(name)
    if module_path is not None:
        import importlib
        try:
            mod = importlib.import_module(module_path)
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                f"代理适配器缺失: {module_path} 未实现（providers/proxy/ 为空），"
                f"无法创建 {name}。请改用静态代理池。"
            ) from exc
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_proxy_provider(provider_key: str, config: dict) -> BaseProxyProvider:
    """根据 provider_key 和配置创建代理提供者。"""
    if provider_key == "api_extract":
        api_url = config.get("proxy_api_url", "")
        if not api_url:
            raise RuntimeError("动态代理未配置 API URL")
        provider_cls = __getattr__("ApiExtractProvider")
        return provider_cls(
            api_url=api_url,
            protocol=config.get("proxy_protocol", "http"),
            username=config.get("proxy_username", ""),
            password=config.get("proxy_password", ""),
        )

    if provider_key == "rotating_gateway":
        gateway = config.get("proxy_gateway_url", "")
        if not gateway:
            raise RuntimeError("旋转代理未配置网关地址")
        provider_cls = __getattr__("RotatingProxyProvider")
        return provider_cls(gateway_url=gateway)

    raise RuntimeError(f"未知的代理 provider: {provider_key}")


def get_dynamic_proxy(extra: dict | None = None) -> Optional[str]:
    """尝试从配置的动态代理 provider 获取代理。

    如果未配置动态代理，返回 None（回退到静态代理池）。
    """
    try:
        from infrastructure.provider_settings_repository import ProviderSettingsRepository
        repo = ProviderSettingsRepository()
        settings = repo.list_enabled("proxy")
        for setting in settings:
            if not setting.enabled:
                continue
            config = setting.get_config()
            auth = setting.get_auth()
            merged = {**config, **auth, **(extra or {})}
            try:
                provider = create_proxy_provider(setting.provider_key, merged)
                proxy = provider.get_proxy()
                if proxy:
                    return proxy
            except Exception as exc:
                # warning, not debug: a user who enabled a dynamic proxy on the
                # settings page must see why it silently fell back to the static
                # pool (most often: the adapter module does not exist at all).
                logger.warning("[ProxyProvider] %s 获取代理失败，回退静态池: %s", setting.provider_key, exc)
                continue
    except Exception as exc:
        logger.warning("[ProxyProvider] 读取动态代理配置失败，回退静态池: %s", exc)
    return None
