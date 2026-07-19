"""注册引擎启动入口。

加载平台插件 + provider 驱动，初始化最小 SQLite。
不启动 FastAPI / Electron / 任务队列 / 生命周期调度。
"""
from __future__ import annotations

from core.db import init_db
from core.proxy_env import load_dotenv, mask_proxy, resolve_proxy
from core.registry import load_all as load_platforms
from core.registry import list_platforms
from providers.registry import load_all as load_providers


def bootstrap() -> list[dict]:
    load_dotenv()
    proxy = resolve_proxy(None)
    if proxy:
        print(f"[proxy] default={mask_proxy(proxy)}")
    init_db()
    load_platforms()
    load_providers()
    platforms = list_platforms()
    return platforms


if __name__ == "__main__":
    items = bootstrap()
    print(f"[OK] loaded {len(items)} platforms:")
    for item in items:
        executors = ",".join(item.get("supported_executors") or [])
        modes = ",".join(item.get("supported_identity_modes") or [])
        print(f"  - {item['name']:16s} {item['display_name']:16s} executors=[{executors}] identity=[{modes}]")
