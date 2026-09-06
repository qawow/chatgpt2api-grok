"""Load tiktoken encodings without inheriting a dead process-wide SOCKS proxy.

tiktoken downloads ``o200k_base.tiktoken`` from Azure Blob on first use via
``requests.get()``, which honors ``HTTP(S)_PROXY``. Image generation only needs
token counts for usage stats — a refused proxy must not fail the whole request.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

_BASE_DIR = Path(__file__).resolve().parents[1]
_DATA_DIR = _BASE_DIR / "data"
_BAKED_CACHE = _BASE_DIR / ".tiktoken_cache"

_LOCK = threading.Lock()
_ENCODINGS: dict[str, Any] = {}
_FALLBACK: Any | None = None
_PATCHED = False


class FallbackEncoding:
    """UTF-8 length / 4 — good enough for usage accounting when tiktoken cannot load."""

    name = "fallback_utf8_approx"

    def encode(self, text: str, **_kwargs: object) -> list[int]:
        raw = str(text or "").encode("utf-8")
        n = (len(raw) + 3) // 4
        return [0] * n


def _cache_dir() -> Path:
    env = str(os.environ.get("TIKTOKEN_CACHE_DIR") or "").strip()
    if env:
        path = Path(env)
    elif _BAKED_CACHE.is_dir():
        path = _BAKED_CACHE
    else:
        path = _DATA_DIR / "tiktoken"
    path.mkdir(parents=True, exist_ok=True)
    os.environ["TIKTOKEN_CACHE_DIR"] = str(path)
    return path


def _install_direct_download() -> None:
    """Force tiktoken's HTTP fetch to ignore env proxies (dead SOCKS, privoxy, etc.)."""
    global _PATCHED
    if _PATCHED:
        return
    try:
        import tiktoken.load as tiktoken_load
    except Exception:
        return
    original = tiktoken_load.read_file

    def read_file_direct(blobpath: str) -> bytes:
        if not str(blobpath).startswith(("http://", "https://")):
            return original(blobpath)
        import requests

        session = requests.Session()
        session.trust_env = False
        response = session.get(blobpath, timeout=30)
        response.raise_for_status()
        return response.content

    tiktoken_load.read_file = read_file_direct
    _PATCHED = True


def encoding_for_model(model: str):
    key = str(model or "").strip() or "o200k_base"
    with _LOCK:
        cached = _ENCODINGS.get(key)
        if cached is not None:
            return cached
        _cache_dir()
        _install_direct_download()
        encoding = None
        try:
            import tiktoken

            try:
                encoding = tiktoken.encoding_for_model(key)
            except KeyError:
                encoding = None
            except Exception:
                encoding = None
            if encoding is None:
                for name in ("o200k_base", "cl100k_base"):
                    try:
                        encoding = tiktoken.get_encoding(name)
                        break
                    except Exception:
                        continue
        except Exception:
            encoding = None
        if encoding is None:
            global _FALLBACK
            if _FALLBACK is None:
                _FALLBACK = FallbackEncoding()
            encoding = _FALLBACK
        _ENCODINGS[key] = encoding
        return encoding


def warmup() -> None:
    """Best-effort preload so the first image request does not wait on Azure Blob."""
    try:
        encoding_for_model("gpt-image-2")
    except Exception:
        return
