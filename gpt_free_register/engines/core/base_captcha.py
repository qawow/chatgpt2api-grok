"""验证码解决器基类 — 具体实现已迁移到 providers/captcha/"""
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class BaseCaptcha(ABC):
    @abstractmethod
    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        """返回 Turnstile token"""
        ...

    @abstractmethod
    def solve_image(self, image_b64: str) -> str:
        """返回图片验证码文字"""
        ...


# ---------------------------------------------------------------------------
# Lazy re-exports for backward compatibility
# (concrete classes now live under providers/captcha/)
#
# WARNING: none of the modules below exist in this tree — providers/captcha/ only
# ships an __init__.py. Every captcha solver therefore raises ModuleNotFoundError
# at creation time. The matching provider definitions are seeded with
# enabled=False (see infrastructure/provider_definitions_repository.py) so the
# settings page cannot offer a provider that is guaranteed to fail.
# The built-in ChatGPT protocol flow never requests a captcha solver
# (ProtocolMailboxAdapter.use_captcha_for_mailbox is False), so this only affects
# flows that opt in explicitly.
# ---------------------------------------------------------------------------
_LAZY_IMPORTS = {
    "YesCaptcha": "providers.captcha.yescaptcha",
    "TwoCaptcha": "providers.captcha.twocaptcha",
    "ManualCaptcha": "providers.captcha.manual",
    "LocalSolverCaptcha": "providers.captcha.local_solver",
}


def _load_solver_class(name: str):
    """Import a captcha adapter class, failing loudly when it is not shipped."""
    import importlib

    module_path = _LAZY_IMPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        mod = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        logger.warning("[Captcha] 适配器缺失: %s (%s)", module_path, exc)
        raise RuntimeError(
            f"验证码适配器缺失: {module_path} 未实现（providers/captcha/ 为空），无法创建 {name}"
        ) from exc
    return getattr(mod, name)


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        return _load_solver_class(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _definition_auth_fields(definition) -> list[str]:
    if not definition:
        return []
    return [
        str(field.get("key") or "")
        for field in definition.get_fields()
        if str(field.get("category") or "") == "auth" and str(field.get("key") or "")
    ]


def has_captcha_configured(provider_key: str, extra: dict | None = None) -> bool:
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from infrastructure.provider_settings_repository import ProviderSettingsRepository

    key = str(provider_key or "").strip()
    if key == "manual":
        return True

    definition = ProviderDefinitionsRepository().get_by_key("captcha", key)
    if not definition or not definition.enabled:
        return False

    merged = ProviderSettingsRepository().resolve_runtime_settings("captcha", key, extra or {})
    auth_fields = _definition_auth_fields(definition)
    if not auth_fields:
        return True
    return any(str(merged.get(field_key, "")).strip() for field_key in auth_fields)


def create_captcha_solver(provider_key: str, extra: dict | None = None) -> BaseCaptcha:
    # Adapter classes are resolved lazily (see _load_solver_class): importing all
    # four eagerly used to blow up the whole function with ModuleNotFoundError
    # before the "provider not configured" checks below could run.
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from infrastructure.provider_settings_repository import ProviderSettingsRepository

    key = str(provider_key or "").strip().lower()
    if key == "manual":
        return _load_solver_class("ManualCaptcha")()

    definition = ProviderDefinitionsRepository().get_by_key("captcha", key)
    if not definition or not definition.enabled:
        raise RuntimeError(f"验证码 provider 不存在或未启用: {key}")
    merged = ProviderSettingsRepository().resolve_runtime_settings("captcha", key, extra or {})
    driver_type = (definition.driver_type if definition else key).lower()

    if driver_type == "local_solver":
        return _load_solver_class("LocalSolverCaptcha")(str(merged.get("solver_url", "") or ""))
    if driver_type == "yescaptcha_api":
        client_key = str(merged.get("yescaptcha_key", "") or "")
        if not client_key:
            raise RuntimeError("YesCaptcha Key 未配置，无法继续协议注册")
        return _load_solver_class("YesCaptcha")(client_key)
    if driver_type == "twocaptcha_api":
        api_key = str(merged.get("twocaptcha_key", "") or "")
        if not api_key:
            raise RuntimeError("2Captcha Key 未配置，无法继续协议注册")
        return _load_solver_class("TwoCaptcha")(api_key)
    raise ValueError(f"未知验证码解决器: {provider_key}")
