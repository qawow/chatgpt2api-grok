"""Integration with Futureppo/grokcli2api-go.

Remote admin surface (requires GROK_ADMIN_KEY on the remote):

  GET    /v1/admin/credentials
  POST   /v1/admin/credentials   (JSON body or multipart file)
  DELETE /v1/admin/credentials/{id}

OpenAI-compatible client surface (API key / admin key as Bearer):

  POST   /v1/responses           # 0.4.x image primary: tools=[{type:image_generation}]
  POST   /v1/chat/completions
  GET    /v1/models
  POST   /v1/images/generations  # not on 0.4.x; kept as fallback if a future remote adds it

List responses are intentionally masked (no tokens / paths). Therefore:

  local Grok pool  ──push──►  grokcli2api-go auths
  chatgpt2api Grok 生图 ──proxy──►  grokcli2api-go /v1/responses + image_generation
  remote credentials ──status only──►  号池管理展示（脱敏，不可反向导入 token）

This never writes into the ChatGPT account pool.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import requests

from services.config import DATA_DIR
from services.grok_account_service import grok_account_service

G2A_CONFIG_FILE = DATA_DIR / "g2a_config.json"


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: object) -> str:
    return str(value or "").strip()


def _normalize_base_url(value: object) -> str:
    """Normalize grokcli2api-go root.

    Admin paths are absolute under the service root, e.g. /v1/admin/credentials.
    Users often paste ``http://host:8088/v1`` (OpenAI base); strip trailing /v1
    so we don't double-prefix. Also strip accidental full admin paths.
    """
    base = _clean(value).rstrip("/")
    if not base:
        return ""
    lower = base.lower()
    for suffix in (
        "/v1/admin/credentials",
        "/v1/admin",
        "/admin/credentials",
        "/admin",
        "/v1",
    ):
        if lower.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
            lower = base.lower()
            break
    return base


def _looks_like_image_b64(value: str) -> bool:
    """Cheap check for raw/data-url image base64 (JPEG/PNG/GIF/WEBP)."""
    import base64
    import re as _re

    s = _re.sub(r"\s+", "", value or "")
    if len(s) < 64:
        return False
    if s.startswith("data:image"):
        return True
    if s.startswith(("/9j/", "iVBOR", "R0lGOD", "UklGR")):
        return True
    try:
        raw = base64.b64decode(s[:96] + "====", validate=False)
    except Exception:
        return False
    return raw.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF"))


def _extract_g2a_images_from_responses(data: dict[str, Any]) -> list[dict[str, str]]:
    """Pull image payloads from grokcli2api-go /v1/responses output.

    Free Build path yields items like::

        {"type": "image_generation_call", "status": "completed",
         "result": "<jpeg-or-png-base64>", "prompt": "..."}

    Also accepts OpenAI-style b64_json/url fields and data:image URLs in text.
    Kept local so g2a_service does not import the full Grok backend client.
    """
    import re as _re

    data_url_re = _re.compile(
        r"data:(?P<mime>image/[-+.\w]+);base64,(?P<data>[A-Za-z0-9+/=\s]+)",
        _re.I,
    )
    found: list[dict[str, str]] = []
    text_blobs: list[str] = []

    def push_b64(raw: str) -> None:
        s = _re.sub(r"\s+", "", raw or "")
        if s.startswith("data:image"):
            m = data_url_re.search(s)
            if m:
                s = _re.sub(r"\s+", "", m.group("data"))
        if s and _looks_like_image_b64(s):
            found.append({"b64_json": s})

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            node_type = str(node.get("type") or "").strip().lower()
            if node_type in {
                "image_generation_call",
                "image_generation",
                "image_generation_result",
            }:
                result = (
                    node.get("result")
                    or node.get("image")
                    or node.get("b64_json")
                    or node.get("base64")
                )
                if isinstance(result, str) and result.strip():
                    push_b64(result)
                if isinstance(result, dict):
                    walk(result)
            b64 = node.get("b64_json") or node.get("base64")
            if isinstance(b64, str) and b64.strip():
                push_b64(b64)
            for key in ("result", "image", "image_base64", "data"):
                val = node.get(key)
                if isinstance(val, str) and _looks_like_image_b64(val):
                    push_b64(val)
            url = node.get("url") or node.get("image_url")
            if isinstance(url, str) and url.startswith(
                ("http://", "https://", "data:image")
            ):
                if url.startswith("data:image"):
                    m = data_url_re.search(url)
                    if m:
                        found.append({"b64_json": _re.sub(r"\s+", "", m.group("data"))})
                    else:
                        found.append({"url": url})
                else:
                    found.append({"url": url})
            for key in ("text", "content", "output_text"):
                val = node.get(key)
                if isinstance(val, str) and val:
                    text_blobs.append(val)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    for blob in text_blobs:
        for match in data_url_re.finditer(blob):
            found.append({"b64_json": _re.sub(r"\s+", "", match.group("data"))})
    uniq: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in found:
        key = item.get("b64_json") or item.get("url") or ""
        if key and key not in seen:
            seen.add(key)
            uniq.append(item)
    return uniq


def _normalize_server(raw: dict) -> dict:
    # api_key: OpenAI-compatible client auth. Empty → fall back to admin_key.
    # prefer_for_image: when true (default), Grok 生图优先走该远程而不是本地号池。
    prefer_raw = raw.get("prefer_for_image", True)
    if isinstance(prefer_raw, str):
        prefer_for_image = prefer_raw.strip().lower() not in {"0", "false", "no", "off"}
    else:
        prefer_for_image = bool(prefer_raw)
    return {
        "id": _clean(raw.get("id")) or _new_id(),
        "name": _clean(raw.get("name")),
        "base_url": _normalize_base_url(raw.get("base_url")),
        "admin_key": _clean(raw.get("admin_key") or raw.get("secret_key")),
        "api_key": _clean(raw.get("api_key") or raw.get("client_key")),
        "prefer_for_image": prefer_for_image,
        "enabled": bool(raw.get("enabled", True)),
        "note": _clean(raw.get("note")),
        # Optional HTTP(S)/SOCKS proxy for this admin connection only.
        # Empty / missing means direct (ignore process HTTP(S)_PROXY).
        "proxy": _clean(raw.get("proxy")),
        "created_at": _clean(raw.get("created_at")) or _now_iso(),
        "updated_at": _clean(raw.get("updated_at")) or _now_iso(),
        "last_error": _clean(raw.get("last_error")) or None,
        "last_ok_at": _clean(raw.get("last_ok_at")) or None,
    }


def sanitize_g2a_server(server: dict | None) -> dict | None:
    if not isinstance(server, dict):
        return None
    item = {
        key: value
        for key, value in server.items()
        if key not in {"admin_key", "api_key"}
    }
    item["has_admin_key"] = bool(_clean(server.get("admin_key")))
    item["has_api_key"] = bool(_clean(server.get("api_key")))
    # Effective client auth available for OpenAI-compatible proxying.
    item["can_proxy_image"] = bool(
        _clean(server.get("api_key")) or _clean(server.get("admin_key"))
    ) and bool(server.get("enabled", True))
    return item


def sanitize_g2a_servers(servers: list[dict]) -> list[dict]:
    return [s for server in servers if (s := sanitize_g2a_server(server)) is not None]


def remote_account_id(server_id: str, credential_id: str) -> str:
    """Stable synthetic id for UI selection (not a real access token)."""
    return f"g2a:{_clean(server_id)}:{_clean(credential_id)}"


def parse_remote_account_id(value: object) -> tuple[str, str] | None:
    text = _clean(value)
    if not text.startswith("g2a:"):
        return None
    parts = text.split(":", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def credential_to_account_row(server: dict, credential: dict[str, Any]) -> dict[str, Any]:
    """Map a desensitized remote credential into the Account table shape."""
    server_id = _clean(server.get("id"))
    cred_id = _clean(credential.get("id"))
    email = _clean(credential.get("email")) or None
    disabled = bool(credential.get("disabled"))
    status_raw = _clean(credential.get("status")).lower()
    if disabled or status_raw in {"disabled", "禁用"}:
        status = "禁用"
    elif status_raw in {"error", "异常", "invalid", "revoked", "banned"}:
        status = "异常"
    elif status_raw in {"rate_limited", "限流", "throttled"}:
        status = "限流"
    else:
        status = "正常"
    return {
        "access_token": remote_account_id(server_id, cred_id),
        "type": "g2a-remote",
        "source_type": "g2a",
        "status": status,
        "quota": 0,
        "email": email,
        "user_id": cred_id or None,
        "success": 0,
        "fail": 0,
        "provider": "g2a",
        "account_id": cred_id or None,
        "base_url": _clean(server.get("base_url")) or None,
        "last_error": None if status == "正常" else (credential.get("status") or None),
        "last_refresh": _clean(server.get("last_ok_at")) or None,
        "created_at": None,
        "g2a_server_id": server_id,
        "g2a_server_name": _clean(server.get("name")) or _clean(server.get("base_url")),
        "g2a_credential_id": cred_id,
        "readonly": True,
        "remote": True,
    }


class G2AConfig:
    def __init__(self, store_file: Path | None = None):
        self._store_file = store_file or G2A_CONFIG_FILE
        self._lock = Lock()
        self._servers: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not self._store_file.exists():
            return []
        try:
            raw = json.loads(self._store_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [_normalize_server(item) for item in raw if isinstance(item, dict)]
        except Exception:
            pass
        return []

    def _save(self) -> None:
        self._store_file.parent.mkdir(parents=True, exist_ok=True)
        self._store_file.write_text(
            json.dumps(self._servers, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def list_servers(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._servers]

    def get_server(self, server_id: str) -> dict | None:
        sid = _clean(server_id)
        with self._lock:
            for item in self._servers:
                if item["id"] == sid:
                    return dict(item)
        return None

    def add_server(
        self,
        *,
        name: str,
        base_url: str,
        admin_key: str,
        note: str = "",
        proxy: str = "",
        api_key: str = "",
        prefer_for_image: bool = True,
    ) -> dict:
        server = _normalize_server(
            {
                "id": _new_id(),
                "name": name,
                "base_url": base_url,
                "admin_key": admin_key,
                "api_key": api_key,
                "prefer_for_image": prefer_for_image,
                "note": note,
                "proxy": proxy,
                "enabled": True,
            }
        )
        if not server["base_url"]:
            raise ValueError("base_url is required")
        if not server["admin_key"]:
            raise ValueError("admin_key is required")
        with self._lock:
            self._servers.append(server)
            self._save()
        return dict(server)

    def update_server(self, server_id: str, updates: dict) -> dict | None:
        sid = _clean(server_id)
        with self._lock:
            for index, item in enumerate(self._servers):
                if item["id"] != sid:
                    continue
                merged = dict(item)
                for key in (
                    "name",
                    "base_url",
                    "admin_key",
                    "api_key",
                    "note",
                    "enabled",
                    "proxy",
                    "prefer_for_image",
                ):
                    if key in updates and updates[key] is not None:
                        if key in {"admin_key", "api_key"} and not _clean(updates[key]):
                            continue  # empty means keep
                        merged[key] = updates[key]
                merged["id"] = sid
                merged["updated_at"] = _now_iso()
                self._servers[index] = _normalize_server(merged)
                self._save()
                return dict(self._servers[index])
        return None

    def list_image_proxy_servers(self) -> list[dict]:
        """Enabled servers that can proxy OpenAI-compatible image calls."""
        with self._lock:
            out: list[dict] = []
            for item in self._servers:
                if not item.get("enabled", True):
                    continue
                if not item.get("prefer_for_image", True):
                    continue
                if not (_clean(item.get("api_key")) or _clean(item.get("admin_key"))):
                    continue
                if not _clean(item.get("base_url")):
                    continue
                out.append(dict(item))
            return out

    def delete_server(self, server_id: str) -> bool:
        sid = _clean(server_id)
        with self._lock:
            before = len(self._servers)
            self._servers = [item for item in self._servers if item["id"] != sid]
            if len(self._servers) < before:
                self._save()
                return True
        return False

    def mark_status(self, server_id: str, *, ok: bool, error: str | None = None) -> None:
        sid = _clean(server_id)
        with self._lock:
            for index, item in enumerate(self._servers):
                if item["id"] != sid:
                    continue
                next_item = dict(item)
                if ok:
                    next_item["last_ok_at"] = _now_iso()
                    next_item["last_error"] = None
                else:
                    next_item["last_error"] = _clean(error) or "unknown"
                next_item["updated_at"] = _now_iso()
                self._servers[index] = next_item
                self._save()
                return


class G2AClientError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class G2AClient:
    """HTTP client for one grokcli2api-go admin + OpenAI-compatible endpoint.

    Traffic defaults to **direct** connection (``trust_env=False`` and
    ``proxies`` disabled). Process-level ``HTTP_PROXY``/``HTTPS_PROXY`` often
    point at CONNECT-only forwarders; sending GET/POST through them yields:

        HTTP 405 ... only CONNECT supported

    which is unrelated to grokcli2api-go itself. Optional per-server ``proxy``
    can re-enable an explicit outbound proxy when the host is remote.
    """

    def __init__(self, server: dict, *, timeout: float = 45.0):
        self.server_id = _clean(server.get("id"))
        self.server_name = _clean(server.get("name")) or _clean(server.get("base_url"))
        self.base_url = _normalize_base_url(server.get("base_url"))
        self.admin_key = _clean(server.get("admin_key"))
        self.api_key = _clean(server.get("api_key")) or self.admin_key
        self.proxy = _clean(server.get("proxy"))
        self.timeout = timeout
        if not self.base_url:
            raise G2AClientError("server base_url is empty")
        if not self.admin_key and not self.api_key:
            raise G2AClientError("server admin_key/api_key is empty")
        self._session = requests.Session()
        # Never inherit ambient HTTP(S)_PROXY for admin/API by default.
        self._session.trust_env = False
        if self.proxy:
            self._session.proxies = {"http": self.proxy, "https": self.proxy}
        else:
            self._session.proxies = {"http": None, "https": None}

    def _headers(
        self,
        *,
        content_type: str | None = "application/json",
        use_api_key: bool = False,
    ) -> dict[str, str]:
        key = self.api_key if use_api_key else (self.admin_key or self.api_key)
        if not key:
            raise G2AClientError("missing auth key for request")
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }
        if not use_api_key and self.admin_key:
            headers["X-Admin-Key"] = self.admin_key
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    def _format_http_error(self, status: int, text: str, url: str) -> str:
        snippet = (text or "").strip().replace("\n", " ")[:280]
        lower = snippet.lower()
        if status == 405 and "only connect supported" in lower:
            return (
                f"HTTP 405 from {url}: request hit a CONNECT-only proxy "
                f"(not grokcli2api-go). G2A admin calls now bypass env proxies; "
                f"check base_url is the service root (e.g. http://host:8088), "
                f"not a local proxy port. raw={snippet[:160]}"
            )
        if status == 404 and ("not found" in lower or "<!doctype" in lower or "<html" in lower):
            if "/v1/images/generations" in url:
                return (
                    f"HTTP 404 from {url}: grokcli2api-go 0.4.x has no Images API; "
                    f"image proxy uses POST /v1/responses + tools=[image_generation] instead. "
                    f"raw={snippet[:120]}"
                )
            return (
                f"HTTP 404 from {url}: path missing — use service root as base_url "
                f"(http://host:8088), admin path is /v1/admin/credentials, "
                f"image path is /v1/responses (not /v1/images/generations on 0.4.x). "
                f"raw={snippet[:160]}"
            )
        return f"HTTP {status}: {snippet or '(empty body)'}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        url = self._url(path)
        hdrs = headers if headers is not None else self._headers(
            content_type=None if files is not None else ("application/json" if json_body is not None else None)
        )
        try:
            resp = self._session.request(
                method.upper(),
                url,
                headers=hdrs,
                json=json_body,
                data=data,
                files=files,
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise G2AClientError(f"network error talking to {url}: {exc}") from exc
        text = resp.text or ""
        if resp.status_code >= 400:
            raise G2AClientError(
                self._format_http_error(resp.status_code, text, url),
                status=resp.status_code,
                body=text[:800],
            )
        if not text.strip():
            return {}
        try:
            return resp.json()
        except Exception:
            return {"raw": text[:500]}

    def ping(self) -> dict[str, Any]:
        """List credentials as connectivity probe."""
        if not self.admin_key:
            raise G2AClientError("server admin_key is empty")
        result = self.list_credentials()
        return {
            "ok": True,
            "count": len(result.get("items") or []),
            "base_url": self.base_url,
            "can_proxy_image": bool(self.api_key or self.admin_key),
            "raw_keys": sorted(result.keys()),
        }

    def list_credentials(self) -> dict[str, Any]:
        if not self.admin_key:
            raise G2AClientError("server admin_key is empty")
        payload = self._request("GET", "/v1/admin/credentials")
        items = _extract_credential_list(payload)
        return {"items": items, "raw": payload if isinstance(payload, dict) else {"data": payload}}

    def upload_credential(self, account: dict[str, Any]) -> dict[str, Any]:
        """POST cliproxy-compatible JSON body (same as remote --data-binary @auth.json)."""
        if not self.admin_key:
            raise G2AClientError("server admin_key is empty")
        body = _account_to_cliproxy_payload(account)
        if not body.get("access_token"):
            raise G2AClientError("account missing access_token")
        # Prefer raw JSON bytes (matches README --data-binary @auth.json).
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = self._headers(content_type="application/json")
        try:
            return self._request("POST", "/v1/admin/credentials", data=raw, headers=headers)
        except G2AClientError as first_exc:
            # Fallback: multipart file field used by some deploy docs.
            if first_exc.status not in {400, 415, 422}:
                raise
            try:
                return self.upload_credential_file(raw, filename="auth.json")
            except G2AClientError:
                raise first_exc from None

    def upload_credential_file(self, content: bytes, filename: str = "auth.json") -> dict[str, Any]:
        if not self.admin_key:
            raise G2AClientError("server admin_key is empty")
        headers = self._headers(content_type=None)
        files = {"file": (filename or "auth.json", content, "application/json")}
        return self._request("POST", "/v1/admin/credentials", files=files, headers=headers)

    def delete_credential(self, credential_id: str) -> dict[str, Any]:
        if not self.admin_key:
            raise G2AClientError("server admin_key is empty")
        cid = _clean(credential_id)
        if not cid:
            raise G2AClientError("credential id is required")
        return self._request("DELETE", f"/v1/admin/credentials/{cid}")

    def generate_image(
        self,
        *,
        prompt: str,
        model: str = "grok-2-image",
        n: int = 1,
        size: str | None = None,
        response_format: str = "b64_json",
        extra: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Proxy image generation to remote grokcli2api-go.

        grokcli2api-go 0.4.x does **not** expose ``POST /v1/images/generations``.
        Free Grok Build images go through the OpenAI Responses surface:

            POST /v1/responses
            model=grok-4.5 (text / build-free)
            tools=[{"type":"image_generation"}]

        Account rotation stays on the remote. We only forward the request and
        normalize the result into OpenAI Images ``{created, data:[{b64_json|url}]}``.

        Fallback order:
        1. ``/v1/responses`` + image_generation tool (primary, matches 0.4.x)
        2. ``/v1/images/generations`` (if a future remote adds it)
        """
        if not (self.api_key or self.admin_key):
            raise G2AClientError("server api_key/admin_key is empty")
        prompt_text = _clean(prompt)
        if not prompt_text:
            raise G2AClientError("prompt is required", status=400)
        want_n = max(1, min(int(n or 1), 4))
        prev_timeout = self.timeout
        if timeout is not None:
            self.timeout = float(timeout)
        attempts: list[dict[str, Any]] = []
        try:
            # 1) Primary: Responses + image_generation tool (grokcli2api-go 0.4.x)
            try:
                out_items, used_model, path = self._generate_image_via_responses(
                    prompt=prompt_text,
                    model=model,
                    n=want_n,
                    extra=extra,
                )
                return self._image_result(
                    out_items,
                    model=used_model,
                    path=path,
                    response_format=response_format,
                    attempts=attempts
                    + [{"path": path, "model": used_model, "status": 200, "images": len(out_items)}],
                )
            except G2AClientError as exc:
                attempts.append(
                    {
                        "path": "responses+image_generation",
                        "error": str(exc)[:220],
                        "status": exc.status,
                    }
                )
                # Auth errors: don't bother with images/generations
                if exc.status in {401, 403}:
                    raise

            # 2) Optional OpenAI Images endpoint (not present on 0.4.x)
            try:
                out_items, used_model, path = self._generate_image_via_images_api(
                    prompt=prompt_text,
                    model=model,
                    n=want_n,
                    size=size,
                    response_format=response_format,
                    extra=extra,
                )
                return self._image_result(
                    out_items,
                    model=used_model,
                    path=path,
                    response_format=response_format,
                    attempts=attempts
                    + [{"path": path, "model": used_model, "status": 200, "images": len(out_items)}],
                )
            except G2AClientError as exc:
                attempts.append(
                    {
                        "path": "images/generations",
                        "error": str(exc)[:220],
                        "status": exc.status,
                    }
                )
                raise G2AClientError(
                    f"g2a image proxy failed on {self.base_url}: "
                    f"{'; '.join(a.get('error') or '' for a in attempts if a.get('error'))[:500]}",
                    status=exc.status or 502,
                    body={"attempts": attempts},
                ) from exc
        finally:
            self.timeout = prev_timeout

    def _response_image_models(self, requested: str) -> list[str]:
        """Text models accepted by free Build for the image_generation tool.

        Image model ids like ``grok-2-image`` 400 on /responses ("Model not found").
        """
        ordered: list[str] = []
        for candidate in (
            "grok-4.5",
            "grok-4",
            "grok-3",
            "grok",
        ):
            if candidate not in ordered:
                ordered.append(candidate)
        # Keep requested only if it already looks like a text/build model.
        name = _clean(requested).lower()
        if name and "image" not in name and "imagine" not in name and name not in ordered:
            ordered.insert(0, _clean(requested))
        return ordered

    def _generate_image_via_responses(
        self,
        *,
        prompt: str,
        model: str,
        n: int,
        extra: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], str, str]:
        headers = self._headers(content_type="application/json", use_api_key=True)
        last_exc: G2AClientError | None = None
        for free_model in self._response_image_models(model):
            body: dict[str, Any] = {
                "model": free_model,
                "input": prompt,
                # Free Build accepts tools=[{type:image_generation}] but rejects
                # OpenAI-style tool_choice object forms (422 ModelToolChoice).
                "tools": [{"type": "image_generation"}],
            }
            if isinstance(extra, dict):
                for key, value in extra.items():
                    if key in body or value is None:
                        continue
                    body[key] = value
            try:
                payload = self._request(
                    "POST",
                    "/v1/responses",
                    json_body=body,
                    headers=headers,
                )
            except G2AClientError as exc:
                last_exc = exc
                if exc.status in {401, 403}:
                    raise
                # model not found / bad request → try next free model
                if exc.status in {400, 404, 422}:
                    continue
                raise
            if not isinstance(payload, dict):
                last_exc = G2AClientError("remote responses body is not a JSON object")
                continue
            extracted = _extract_g2a_images_from_responses(payload)
            if not extracted:
                last_exc = G2AClientError(
                    f"remote /v1/responses returned no image payload "
                    f"(model={free_model}, status={payload.get('status')}, "
                    f"keys={sorted(payload.keys())[:12]})"
                )
                continue
            items = list(extracted[:n])
            while len(items) < n:
                try:
                    more = self._request(
                        "POST",
                        "/v1/responses",
                        json_body=body,
                        headers=headers,
                    )
                except G2AClientError:
                    break
                if not isinstance(more, dict):
                    break
                more_imgs = _extract_g2a_images_from_responses(more)
                if not more_imgs:
                    break
                items.extend(more_imgs)
            return items[:n], free_model, "responses+image_generation"
        if last_exc is not None:
            raise last_exc
        raise G2AClientError("remote /v1/responses image_generation produced no images")

    def _generate_image_via_images_api(
        self,
        *,
        prompt: str,
        model: str,
        n: int,
        size: str | None,
        response_format: str,
        extra: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], str, str]:
        body: dict[str, Any] = {
            "prompt": prompt,
            "model": model,
            "n": n,
            "response_format": response_format or "b64_json",
        }
        if size:
            body["size"] = size
        if isinstance(extra, dict):
            for key, value in extra.items():
                if key in body or value is None:
                    continue
                body[key] = value
        payload = self._request(
            "POST",
            "/v1/images/generations",
            json_body=body,
            headers=self._headers(content_type="application/json", use_api_key=True),
        )
        if not isinstance(payload, dict):
            raise G2AClientError("remote image response is not a JSON object")
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            for key in ("images", "result", "output"):
                alt = payload.get(key)
                if isinstance(alt, list) and alt:
                    data = alt
                    break
            if not isinstance(data, list) or not data:
                raise G2AClientError(
                    f"remote image response missing data[]: keys={sorted(payload.keys())[:20]}"
                )
        out_items: list[dict[str, Any]] = []
        for item in data:
            if isinstance(item, str) and item.strip():
                out_items.append({"b64_json": item.strip()})
                continue
            if not isinstance(item, dict):
                continue
            entry: dict[str, Any] = {}
            b64 = item.get("b64_json") or item.get("b64") or item.get("base64") or item.get("result")
            url = item.get("url")
            if isinstance(b64, str) and b64.strip():
                entry["b64_json"] = b64.strip()
            if isinstance(url, str) and url.strip():
                entry["url"] = url.strip()
            if entry:
                out_items.append(entry)
        if not out_items:
            raise G2AClientError("remote image response had no usable b64/url items")
        return out_items, model, "images/generations"

    def _image_result(
        self,
        out_items: list[dict[str, Any]],
        *,
        model: str,
        path: str,
        response_format: str,
        attempts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if response_format == "url":
            normalized: list[dict[str, Any]] = []
            for item in out_items:
                if item.get("url") and item.get("b64_json"):
                    normalized.append({k: v for k, v in item.items() if k != "b64_json"})
                else:
                    normalized.append(item)
            out_items = normalized
        return {
            "created": int(time.time()),
            "data": out_items,
            "_meta": {
                "upstream": "g2a",
                "upstream_path": path,
                "server_id": self.server_id,
                "server_name": self.server_name,
                "base_url": self.base_url,
                "model": model,
                "attempts": attempts or [],
            },
        }


def _extract_credential_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        for key in ("credentials", "items", "data", "accounts"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
        else:
            # single credential response
            if payload.get("id") or payload.get("credential"):
                cred = payload.get("credential") if isinstance(payload.get("credential"), dict) else payload
                candidates = [cred]
            else:
                candidates = []
    else:
        candidates = []

    items: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        # nested credential object
        if isinstance(item.get("credential"), dict):
            base = dict(item["credential"])
            for k in ("model_discovery", "created", "status"):
                if k in item and k not in base:
                    base[k] = item[k]
            item = base
        cid = _clean(item.get("id") or item.get("credential_id"))
        items.append(
            {
                "id": cid,
                "email": _clean(item.get("email") or item.get("account") or item.get("principal")) or None,
                "disabled": bool(item.get("disabled")),
                "status": _clean(item.get("status")) or None,
                "type": _clean(item.get("type") or item.get("auth_kind") or "xai") or "xai",
                "scopes": item.get("scopes") if isinstance(item.get("scopes"), list) else None,
                "model_discovery": item.get("model_discovery"),
                "raw": {k: v for k, v in item.items() if k not in {"access_token", "refresh_token", "id_token"}},
            }
        )
    return items


def _account_to_cliproxy_payload(account: dict[str, Any]) -> dict[str, Any]:
    """Build a CLIProxyAPI / grokcli2api-go compatible auth JSON object."""
    access = _clean(account.get("access_token"))
    headers = account.get("headers") if isinstance(account.get("headers"), dict) else {}
    payload: dict[str, Any] = {
        "type": "xai",
        "auth_kind": _clean(account.get("auth_kind")) or "oauth",
        "email": _clean(account.get("email")),
        "sub": _clean(account.get("sub") or account.get("account_id")),
        "access_token": access,
        "refresh_token": _clean(account.get("refresh_token")),
        "id_token": _clean(account.get("id_token")),
        "token_type": "Bearer",
        "base_url": _clean(account.get("base_url")) or "https://cli-chat-proxy.grok.com/v1",
        "token_endpoint": _clean(account.get("token_endpoint")) or "https://auth.x.ai/oauth2/token",
        "disabled": bool(account.get("disabled")) or _clean(account.get("status")) == "禁用",
        "headers": {str(k): str(v) for k, v in headers.items() if str(k).strip()},
    }
    if not payload["headers"]:
        payload["headers"] = {
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-grok-client-version": "0.2.93",
            "x-grok-client-identifier": "grok-shell",
        }
    if account.get("expires_in") is not None:
        payload["expires_in"] = account.get("expires_in")
    if account.get("expired"):
        payload["expired"] = account.get("expired")
    if account.get("last_refresh"):
        payload["last_refresh"] = account.get("last_refresh")
    # drop empty strings except required-ish fields
    return {k: v for k, v in payload.items() if v not in ("", None, []) or k in {"disabled", "headers", "type"}}


class G2ABridgeService:
    def __init__(self, config: G2AConfig):
        self.config = config

    def list_remote(self, server_id: str) -> dict[str, Any]:
        server = self.config.get_server(server_id)
        if not server:
            raise G2AClientError("server not found", status=404)
        client = G2AClient(server)
        try:
            result = client.list_credentials()
            self.config.mark_status(server_id, ok=True)
            return result
        except G2AClientError as exc:
            self.config.mark_status(server_id, ok=False, error=str(exc))
            raise

    def list_remote_pool_status(self, server_id: str | None = None) -> dict[str, Any]:
        """Aggregate desensitized remote credentials for the accounts UI.

        Tokens are never available from remote admin list — rows are readonly
        status mirrors only. Synthetic access_token is ``g2a:{server}:{cred}``.
        """
        if server_id:
            servers = []
            one = self.config.get_server(server_id)
            if one:
                servers = [one]
            else:
                raise G2AClientError("server not found", status=404)
        else:
            servers = [s for s in self.config.list_servers() if s.get("enabled", True)]

        items: list[dict[str, Any]] = []
        servers_meta: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []

        for server in servers:
            sid = _clean(server.get("id"))
            meta: dict[str, Any] = {
                "id": sid,
                "name": _clean(server.get("name")) or _clean(server.get("base_url")),
                "base_url": _clean(server.get("base_url")),
                "ok": False,
                "count": 0,
                "error": None,
                "can_proxy_image": bool(
                    _clean(server.get("api_key")) or _clean(server.get("admin_key"))
                ),
                "prefer_for_image": bool(server.get("prefer_for_image", True)),
            }
            try:
                client = G2AClient(server)
                result = client.list_credentials()
                creds = result.get("items") or []
                for cred in creds:
                    if not isinstance(cred, dict):
                        continue
                    items.append(credential_to_account_row(server, cred))
                meta["ok"] = True
                meta["count"] = len(creds)
                self.config.mark_status(sid, ok=True)
            except G2AClientError as exc:
                meta["error"] = str(exc)[:300]
                errors.append({"server_id": sid, "error": str(exc)[:300]})
                self.config.mark_status(sid, ok=False, error=str(exc))
            servers_meta.append(meta)

        return {
            "items": items,
            "servers": servers_meta,
            "errors": errors,
            "total": len(items),
            "readonly": True,
            "note": "remote credentials are desensitized (no tokens); status only",
        }

    def ping(self, server_id: str) -> dict[str, Any]:
        server = self.config.get_server(server_id)
        if not server:
            raise G2AClientError("server not found", status=404)
        client = G2AClient(server)
        try:
            result = client.ping()
            self.config.mark_status(server_id, ok=True)
            return result
        except G2AClientError as exc:
            self.config.mark_status(server_id, ok=False, error=str(exc))
            raise

    def generate_image(
        self,
        *,
        prompt: str,
        model: str = "grok-2-image",
        n: int = 1,
        size: str | None = None,
        response_format: str = "b64_json",
        server_id: str | None = None,
        timeout: float = 180.0,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call remote OpenAI-compatible image API on configured G2A servers.

        Tries preferred/enabled servers in order until one returns images.
        Does not touch local Grok tokens.
        """
        if server_id:
            server = self.config.get_server(server_id)
            if not server:
                raise G2AClientError("server not found", status=404)
            candidates = [server]
        else:
            candidates = self.config.list_image_proxy_servers()
        if not candidates:
            raise G2AClientError(
                "no G2A server configured for image proxy "
                "(add connection with admin_key/api_key and prefer_for_image=true)",
                status=404,
            )

        errors: list[str] = []
        last_exc: G2AClientError | None = None
        for server in candidates:
            sid = _clean(server.get("id"))
            try:
                client = G2AClient(server, timeout=timeout)
                result = client.generate_image(
                    prompt=prompt,
                    model=model,
                    n=n,
                    size=size,
                    response_format=response_format,
                    extra=extra,
                    timeout=timeout,
                )
                self.config.mark_status(sid, ok=True)
                return result
            except G2AClientError as exc:
                last_exc = exc
                errors.append(f"{sid or server.get('base_url')}: {exc}")
                self.config.mark_status(sid, ok=False, error=str(exc))
                continue
        detail = "; ".join(errors[:5]) or (str(last_exc) if last_exc else "unknown")
        raise G2AClientError(f"all G2A image proxy attempts failed: {detail}", status=502)

    def has_image_proxy(self) -> bool:
        return bool(self.config.list_image_proxy_servers())

    def push_local_accounts(
        self,
        server_id: str,
        *,
        access_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        """Push local Grok pool accounts to remote grokcli2api-go."""
        server = self.config.get_server(server_id)
        if not server:
            raise G2AClientError("server not found", status=404)
        client = G2AClient(server)

        local = grok_account_service.list_accounts()
        if access_tokens:
            wanted = {_clean(t) for t in access_tokens if _clean(t)}
            local = [a for a in local if _clean(a.get("access_token")) in wanted]

        pushed = 0
        failed = 0
        errors: list[dict[str, str]] = []
        results: list[dict[str, Any]] = []

        for account in local:
            email = _clean(account.get("email")) or "?"
            token_hint = _clean(account.get("access_token"))[:12]
            try:
                resp = client.upload_credential(account)
                pushed += 1
                results.append({"email": email, "ok": True, "response_keys": sorted(resp.keys()) if isinstance(resp, dict) else []})
            except G2AClientError as exc:
                failed += 1
                errors.append({"email": email, "token_prefix": token_hint, "error": str(exc)[:240]})
                results.append({"email": email, "ok": False, "error": str(exc)[:240]})

        if failed and not pushed:
            self.config.mark_status(server_id, ok=False, error=errors[0]["error"] if errors else "push failed")
        else:
            self.config.mark_status(server_id, ok=True)

        return {
            "total": len(local),
            "pushed": pushed,
            "failed": failed,
            "errors": errors,
            "results": results,
        }

    def delete_remote(self, server_id: str, credential_id: str) -> dict[str, Any]:
        server = self.config.get_server(server_id)
        if not server:
            raise G2AClientError("server not found", status=404)
        client = G2AClient(server)
        try:
            resp = client.delete_credential(credential_id)
            self.config.mark_status(server_id, ok=True)
            return {"ok": True, "response": resp}
        except G2AClientError as exc:
            self.config.mark_status(server_id, ok=False, error=str(exc))
            raise


g2a_config = G2AConfig()
g2a_bridge = G2ABridgeService(g2a_config)
