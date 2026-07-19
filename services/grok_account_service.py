"""Isolated Grok/xAI account pool — never shares storage with ChatGPT accounts."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.grok_backend_api import (
    GrokBackendError,
    normalize_base_url,
    probe_responses,
    refresh_access_token,
)

# Keep path independent of config import side-effects (storage backends etc.)
_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
GROK_ACCOUNTS_FILE = _DATA_DIR / "grok_accounts.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(value: object) -> str:
    return str(value or "").strip()


class GrokAccountService:
    """Minimal round-robin pool for cliproxy-style xAI auth records."""

    def __init__(self, path: Path | None = None):
        self.path = path or GROK_ACCOUNTS_FILE
        self._lock = threading.RLock()
        self._accounts: dict[str, dict[str, Any]] = {}
        self._index = 0
        self._load()

    # ── persistence ─────────────────────────────────────────────
    def _load(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._accounts = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._accounts = {}
            return
        items = raw if isinstance(raw, list) else raw.get("items") if isinstance(raw, dict) else []
        accounts: dict[str, dict[str, Any]] = {}
        for item in items or []:
            normalized = self.normalize_account(item)
            if normalized:
                accounts[normalized["access_token"]] = normalized
        self._accounts = accounts

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        items = [dict(item) for item in self._accounts.values()]
        self.path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # ── normalize / import ──────────────────────────────────────
    @classmethod
    def normalize_account(cls, item: object) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        access_token = _clean(item.get("access_token") or item.get("accessToken") or item.get("token"))
        if not access_token:
            return None

        # Reject accidental OpenAI-only imports that lack xai markers when type is set to openai-ish
        type_hint = _clean(item.get("type")).lower()
        if type_hint in {"openai", "chatgpt", "codex"} and "x.ai" not in _clean(item.get("token_endpoint")).lower():
            # Still allow if base_url points at grok/cli-chat-proxy
            base_guess = _clean(item.get("base_url")).lower()
            if "grok" not in base_guess and "x.ai" not in base_guess and "cli-chat-proxy" not in base_guess:
                return None

        headers = item.get("headers") if isinstance(item.get("headers"), dict) else {}
        status = _clean(item.get("status")) or "正常"
        if item.get("disabled") is True:
            status = "禁用"

        account = {
            "access_token": access_token,
            "refresh_token": _clean(item.get("refresh_token")),
            "id_token": _clean(item.get("id_token")),
            "email": _clean(item.get("email")) or None,
            "sub": _clean(item.get("sub") or item.get("account_id") or item.get("user_id")) or None,
            "account_id": _clean(item.get("account_id") or item.get("sub") or item.get("user_id")) or None,
            "type": "xai",
            "auth_kind": _clean(item.get("auth_kind")) or "oauth",
            "base_url": normalize_base_url(_clean(item.get("base_url"))),
            "token_endpoint": _clean(item.get("token_endpoint")) or "https://auth.x.ai/oauth2/token",
            "headers": {str(k): str(v) for k, v in headers.items() if str(k).strip()},
            "status": status,
            "disabled": bool(item.get("disabled")) or status == "禁用",
            "proxy": _clean(item.get("proxy")),
            "expired": _clean(item.get("expired")) or None,
            "last_refresh": _clean(item.get("last_refresh")) or None,
            "expires_in": item.get("expires_in"),
            "remaining_tokens": item.get("remaining_tokens"),
            "limit_tokens": item.get("limit_tokens"),
            "success": int(item.get("success") or 0),
            "fail": int(item.get("fail") or 0),
            "created_at": _clean(item.get("created_at")) or _now_iso(),
            "last_used_at": item.get("last_used_at"),
            "last_error": _clean(item.get("last_error")) or None,
            "last_error_at": item.get("last_error_at"),
            "provider": "grok",
        }
        return account

    # ── CRUD ────────────────────────────────────────────────────
    def list_accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._accounts.values()]

    def count(self) -> int:
        with self._lock:
            return len(self._accounts)

    def add_account_items(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        added = 0
        skipped = 0
        with self._lock:
            for item in items:
                normalized = self.normalize_account(item)
                if not normalized:
                    skipped += 1
                    continue
                token = normalized["access_token"]
                if token in self._accounts:
                    # merge non-empty fields
                    current = dict(self._accounts[token])
                    for key, value in normalized.items():
                        if value in (None, "", [], {}):
                            continue
                        if key == "created_at" and current.get("created_at"):
                            continue
                        current[key] = value
                    self._accounts[token] = current
                    skipped += 1
                else:
                    self._accounts[token] = normalized
                    added += 1
            self._save()
            items_out = [dict(item) for item in self._accounts.values()]
        return {"added": added, "skipped": skipped, "items": items_out}

    def delete_accounts(self, tokens: list[str]) -> dict[str, Any]:
        target = {_clean(t) for t in tokens if _clean(t)}
        with self._lock:
            removed = 0
            for token in list(target):
                if self._accounts.pop(token, None) is not None:
                    removed += 1
            if removed:
                if self._accounts:
                    self._index %= len(self._accounts)
                else:
                    self._index = 0
                self._save()
            items = [dict(item) for item in self._accounts.values()]
        return {"removed": removed, "items": items}

    def update_account(self, access_token: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        token = _clean(access_token)
        if not token:
            return None
        with self._lock:
            current = self._accounts.get(token)
            if current is None:
                return None
            next_item = dict(current)
            if "status" in updates and updates["status"] is not None:
                next_item["status"] = _clean(updates["status"]) or next_item.get("status")
                next_item["disabled"] = next_item["status"] == "禁用"
            if "disabled" in updates and updates["disabled"] is not None:
                next_item["disabled"] = bool(updates["disabled"])
                if next_item["disabled"]:
                    next_item["status"] = "禁用"
                elif next_item.get("status") == "禁用":
                    next_item["status"] = "正常"
            if "proxy" in updates and updates["proxy"] is not None:
                next_item["proxy"] = _clean(updates["proxy"])
            if "base_url" in updates and updates["base_url"] is not None:
                next_item["base_url"] = normalize_base_url(_clean(updates["base_url"]))
            self._accounts[token] = next_item
            self._save()
            return dict(next_item)

    # ── selection ───────────────────────────────────────────────
    def _is_available(self, account: dict[str, Any]) -> bool:
        if bool(account.get("disabled")):
            return False
        status = _clean(account.get("status")) or "正常"
        return status not in {"禁用", "异常"}

    def get_next_account(self, *, exclude_tokens: set[str] | None = None) -> dict[str, Any] | None:
        exclude = exclude_tokens or set()
        with self._lock:
            tokens = [t for t, a in self._accounts.items() if t not in exclude and self._is_available(a)]
            if not tokens:
                return None
            if self._index >= len(tokens):
                self._index = 0
            # map index into available list via full ordered keys
            ordered = list(self._accounts.keys())
            if not ordered:
                return None
            start = self._index % len(ordered)
            for offset in range(len(ordered)):
                token = ordered[(start + offset) % len(ordered)]
                if token in exclude:
                    continue
                account = self._accounts.get(token)
                if account and self._is_available(account):
                    self._index = (start + offset + 1) % len(ordered)
                    next_item = dict(account)
                    next_item["last_used_at"] = _now_iso()
                    self._accounts[token] = {**account, "last_used_at": next_item["last_used_at"]}
                    # persist last_used lazily only on mark/save paths to avoid write storms
                    return next_item
            return None

    def mark_result(self, access_token: str, success: bool, *, error: str | None = None) -> None:
        token = _clean(access_token)
        if not token:
            return
        with self._lock:
            account = self._accounts.get(token)
            if account is None:
                return
            next_item = dict(account)
            if success:
                next_item["success"] = int(next_item.get("success") or 0) + 1
                if next_item.get("status") == "异常":
                    next_item["status"] = "正常"
                next_item["last_error"] = None
            else:
                next_item["fail"] = int(next_item.get("fail") or 0) + 1
                next_item["last_error"] = _clean(error) or next_item.get("last_error")
                next_item["last_error_at"] = _now_iso()
            self._accounts[token] = next_item
            self._save()

    def replace_token(self, old_token: str, new_fields: dict[str, Any]) -> dict[str, Any] | None:
        """After refresh, access_token may rotate — re-key the pool entry."""
        old = _clean(old_token)
        new_token = _clean(new_fields.get("access_token"))
        if not old or not new_token:
            return None
        with self._lock:
            current = self._accounts.get(old)
            if current is None:
                return None
            merged = dict(current)
            for key, value in new_fields.items():
                if value in (None, ""):
                    continue
                merged[key] = value
            merged["access_token"] = new_token
            merged["last_refresh"] = _now_iso()
            if old != new_token:
                self._accounts.pop(old, None)
            self._accounts[new_token] = merged
            self._save()
            return dict(merged)

    # ── refresh + probe ─────────────────────────────────────────
    def refresh_accounts(self, access_tokens: list[str] | None = None) -> dict[str, Any]:
        with self._lock:
            if access_tokens:
                targets = [_clean(t) for t in access_tokens if _clean(t) in self._accounts]
            else:
                targets = list(self._accounts.keys())
            snapshots = [dict(self._accounts[t]) for t in targets if t in self._accounts]

        refreshed = 0
        errors: list[dict[str, str]] = []
        for account in snapshots:
            token = account["access_token"]
            try:
                rt = _clean(account.get("refresh_token"))
                if rt:
                    token_data = refresh_access_token(
                        rt,
                        token_endpoint=_clean(account.get("token_endpoint")) or None,
                        proxy=_clean(account.get("proxy")) or None,
                    )
                    fields = {
                        "access_token": _clean(token_data.get("access_token")) or token,
                        "refresh_token": _clean(token_data.get("refresh_token")) or rt,
                        "id_token": _clean(token_data.get("id_token")) or account.get("id_token"),
                        "expires_in": token_data.get("expires_in"),
                    }
                    exp_at = token_data.get("expires_at")
                    if exp_at:
                        try:
                            fields["expired"] = datetime.fromtimestamp(
                                int(exp_at), tz=timezone.utc
                            ).strftime("%Y-%m-%dT%H:%M:%SZ")
                        except Exception:
                            pass
                    updated = self.replace_token(token, fields)
                    if updated:
                        account = updated
                        token = updated["access_token"]
                        refreshed += 1

                probe = probe_responses(account)
                status_code = int(probe.get("status") or 0)
                updates: dict[str, Any] = {
                    "remaining_tokens": probe.get("remaining_tokens"),
                    "limit_tokens": probe.get("limit_tokens"),
                }
                if status_code == 200:
                    updates["status"] = "正常"
                    updates["disabled"] = False
                    updates["last_error"] = None
                elif status_code == 429:
                    updates["status"] = "限流"
                elif status_code in {401, 403}:
                    updates["status"] = "异常"
                    updates["last_error"] = _clean(probe.get("error")) or f"auth_{status_code}"
                    updates["last_error_at"] = _now_iso()
                else:
                    updates["last_error"] = _clean(probe.get("error")) or f"http_{status_code}"
                    updates["last_error_at"] = _now_iso()

                with self._lock:
                    current = self._accounts.get(token)
                    if current:
                        current = dict(current)
                        current.update({k: v for k, v in updates.items() if v is not None or k in {"last_error"}})
                        self._accounts[token] = current
                        self._save()
            except GrokBackendError as exc:
                errors.append({"access_token": token[:16] + "...", "error": str(exc)[:200]})
                with self._lock:
                    current = self._accounts.get(token)
                    if current:
                        current = dict(current)
                        current["last_error"] = str(exc)[:300]
                        current["last_error_at"] = _now_iso()
                        self._accounts[token] = current
                        self._save()
            except Exception as exc:
                errors.append({"access_token": token[:16] + "...", "error": str(exc)[:200]})

        return {
            "refreshed": refreshed,
            "errors": errors,
            "items": self.list_accounts(),
        }


grok_account_service = GrokAccountService()
