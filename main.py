from __future__ import annotations

import os

import uvicorn
from api import create_app

app = create_app()


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name) or "").strip())
    except (TypeError, ValueError):
        return default
    return value


if __name__ == "__main__":
    # Bind all interfaces so LAN clients can reach this host.
    # Override with CHATGPT2API_HOST / CHATGPT2API_PORT if needed.
    host = str(os.environ.get("CHATGPT2API_HOST") or "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.environ.get("CHATGPT2API_PORT") or "8000")
    # Keep access-log behaviour aligned with the container entrypoint
    # (Dockerfile runs uvicorn with --access-log); local `uv run main.py`
    # previously stayed silent, which made local traffic invisible.
    access_log = _env_bool("CHATGPT2API_ACCESS_LOG", True)
    # Cap concurrent connections so slow / oversized clients cannot exhaust the
    # event loop + thread pool. 0 disables the cap.
    limit_concurrency = _env_int("CHATGPT2API_LIMIT_CONCURRENCY", 256)
    uvicorn.run(
        app,
        host=host,
        port=port,
        access_log=access_log,
        limit_concurrency=limit_concurrency if limit_concurrency > 0 else None,
        log_level="info",
    )
