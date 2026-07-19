"""Integration with Futureppo/grokcli2api-go admin credentials API.

Remote admin surface (requires GROK_ADMIN_KEY on the remote):

  GET    /v1/admin/credentials
  POST   /v1/admin/credentials   (JSON body or multipart file)
  DELETE /v1/admin/credentials/{id}

List responses are intentionally masked (no tokens / paths). Therefore the
supported direction is mainly:

  local Grok pool  ──push──►  grokcli2api-go auths

This never writes into the ChatGPT account pool.
"""
from __future__ import annotations

import json
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


def _normalize_server(raw: dict) -> dict:
    return {
        "id": _clean(raw.get("id")) or _new_id(),
        "name": _clean(raw.get("name")),
        "base_url": _normalize_base_url(raw.get("base_url")),
        "admin_key": _clean(raw.get("admin_key") or raw.get("secret_key")),
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
    item = {key: value for key, value in server.items() if key != "admin_key"}
    item["has_admin_key"] = bool(_clean(server.get("admin_key")))
    return item


def sanitize_g2a_servers(servers: list[dict]) -> list[dict]:
    return [s for server in servers if (s := sanitize_g2a_server(server)) is not None]


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
    ) -> dict:
        server = _normalize_server(
            {
                "id": _new_id(),
                "name": name,
                "base_url": base_url,
                "admin_key": admin_key,
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
                for key in ("name", "base_url", "admin_key", "note", "enabled", "proxy"):
                    if key in updates and updates[key] is not None:
                        if key == "admin_key" and not _clean(updates[key]):
                            continue  # empty means keep
                        merged[key] = updates[key]
                merged["id"] = sid
                merged["updated_at"] = _now_iso()
                self._servers[index] = _normalize_server(merged)
                self._save()
                return dict(self._servers[index])
        return None

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
    """HTTP client for one grokcli2api-go admin endpoint.

    Admin traffic defaults to **direct** connection (``trust_env=False`` and
    ``proxies`` disabled). Process-level ``HTTP_PROXY``/``HTTPS_PROXY`` often
    point at CONNECT-only forwarders; sending GET/POST through them yields:

        HTTP 405 ... only CONNECT supported

    which is unrelated to grokcli2api-go itself. Optional per-server ``proxy``
    can re-enable an explicit outbound proxy when the admin host is remote.
    """

    def __init__(self, server: dict, *, timeout: float = 45.0):
        self.base_url = _normalize_base_url(server.get("base_url"))
        self.admin_key = _clean(server.get("admin_key"))
        self.proxy = _clean(server.get("proxy"))
        self.timeout = timeout
        if not self.base_url:
            raise G2AClientError("server base_url is empty")
        if not self.admin_key:
            raise G2AClientError("server admin_key is empty")
        self._session = requests.Session()
        # Never inherit ambient HTTP(S)_PROXY for admin API by default.
        self._session.trust_env = False
        if self.proxy:
            self._session.proxies = {"http": self.proxy, "https": self.proxy}
        else:
            self._session.proxies = {"http": None, "https": None}

    def _headers(self, *, content_type: str | None = "application/json") -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.admin_key}",
            "X-Admin-Key": self.admin_key,
            "Accept": "application/json",
        }
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
            return (
                f"HTTP 404 from {url}: path missing — use service root as base_url "
                f"(http://host:8088), admin path is /v1/admin/credentials. raw={snippet[:160]}"
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
        result = self.list_credentials()
        return {
            "ok": True,
            "count": len(result.get("items") or []),
            "base_url": self.base_url,
            "raw_keys": sorted(result.keys()),
        }

    def list_credentials(self) -> dict[str, Any]:
        payload = self._request("GET", "/v1/admin/credentials")
        items = _extract_credential_list(payload)
        return {"items": items, "raw": payload if isinstance(payload, dict) else {"data": payload}}

    def upload_credential(self, account: dict[str, Any]) -> dict[str, Any]:
        """POST cliproxy-compatible JSON body (same as remote --data-binary @auth.json)."""
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
        headers = self._headers(content_type=None)
        files = {"file": (filename or "auth.json", content, "application/json")}
        return self._request("POST", "/v1/admin/credentials", files=files, headers=headers)

    def delete_credential(self, credential_id: str) -> dict[str, Any]:
        cid = _clean(credential_id)
        if not cid:
            raise G2AClientError("credential id is required")
        return self._request("DELETE", f"/v1/admin/credentials/{cid}")


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
