import base64
import json
import socket
import sys
import time
import unittest
import urllib.request
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit


ROOT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT_DIR / "data" / "output"
BASE_URL = "http://127.0.0.1:8000"
LIVE_SERVICE_TIMEOUT = 0.2

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


@lru_cache(maxsize=None)
def service_is_live(base_url: str = BASE_URL, timeout: float = LIVE_SERVICE_TIMEOUT) -> bool:
    """本地服务是否在监听（只做 TCP 连通性探测，不发 HTTP 请求）。"""
    parts = urlsplit(base_url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def requires_live_service(base_url: str = BASE_URL):
    """给需要真实起服务的 HTTP 用例加守卫：没起服务就 skip，而不是 error。"""
    return unittest.skipUnless(
        service_is_live(base_url),
        f"本地服务未监听 {base_url}，跳过 HTTP 用例",
    )


def load_auth_key() -> str:
    return json.loads((ROOT_DIR / "config.json").read_text(encoding="utf-8"))["auth-key"]


def post_json(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {load_auth_key()}"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode())


def detect_ext(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return ".webp"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    return ".png"


def save_image(image_b64: str, name: str) -> Path:
    image_bytes = base64.b64decode(image_b64)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{name}_{int(time.time())}{detect_ext(image_bytes)}"
    path.write_bytes(image_bytes)
    return path
