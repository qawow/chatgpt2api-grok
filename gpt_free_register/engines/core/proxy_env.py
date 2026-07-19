"""出站代理环境解析。

优先顺序:
1. 显式传入的 proxy 参数
2. REGISTER_PROXY / AAR_PROXY
3. ALL_PROXY / all_proxy / HTTPS_PROXY / HTTP_PROXY
4. REGISTER_PROXY_DEFAULT（可选默认代理，推荐写在 .env）

不会在源码里硬编码账号密码。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def load_dotenv(path: str | Path | None = None) -> Path | None:
    """轻量加载 .env 到 os.environ（不覆盖已有环境变量）。"""
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    else:
        here = Path(__file__).resolve().parent.parent
        candidates.extend([
            here / ".env",
            here / "data" / ".env",
            Path.cwd() / ".env",
        ])
    for env_path in candidates:
        if not env_path.is_file():
            continue
        try:
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'").strip('"')
                if key and key not in os.environ:
                    os.environ[key] = value
            return env_path
        except Exception:
            continue
    return None


def _first_env(*keys: str) -> str:
    for key in keys:
        value = str(os.getenv(key, "") or "").strip()
        if value:
            return value
    return ""


def resolve_proxy(explicit: Optional[str] = None, *, allow_default: bool = True) -> Optional[str]:
    if explicit is not None:
        value = str(explicit).strip()
        return value or None

    env_proxy = _first_env(
        "REGISTER_PROXY",
        "AAR_PROXY",
        "ALL_PROXY",
        "all_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
    )
    if env_proxy:
        return env_proxy

    if not allow_default:
        return None

    disabled = str(os.getenv("REGISTER_PROXY_DISABLE", "") or "").strip().lower() in {
        "1", "true", "yes", "on", "disable", "disabled",
    }
    if disabled:
        return None

    return _first_env("REGISTER_PROXY_DEFAULT") or None


def proxy_dict(proxy: Optional[str]) -> Optional[dict]:
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def mask_proxy(proxy: Optional[str]) -> str:
    if not proxy:
        return ""
    text = str(proxy)
    if "@" not in text:
        return text
    scheme, rest = text.split("://", 1) if "://" in text else ("", text)
    creds, host = rest.rsplit("@", 1)
    if ":" in creds:
        user, _password = creds.split(":", 1)
        hidden = f"{user}:***"
    else:
        hidden = "***"
    return f"{scheme}://{hidden}@{host}" if scheme else f"{hidden}@{host}"
