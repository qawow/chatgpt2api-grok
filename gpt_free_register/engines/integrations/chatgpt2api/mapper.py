"""把 any-register-engines 的 ChatGPT 注册结果映射为 chatgpt2api 号池条目。"""
from __future__ import annotations

from typing import Any, Mapping


def _text(value: object) -> str:
    return str(value or "").strip()


def _extra(account: Mapping[str, Any] | object) -> dict[str, Any]:
    if isinstance(account, Mapping):
        raw = account.get("extra")
    else:
        raw = getattr(account, "extra", None)
    return dict(raw or {}) if isinstance(raw, Mapping) else {}


def _field(account: Mapping[str, Any] | object, key: str, *extra_keys: str) -> str:
    """优先读 account.extra，再读顶层字段。"""
    extra = _extra(account)
    for candidate in (key, *extra_keys):
        if isinstance(account, Mapping):
            value = account.get(candidate)
        else:
            value = getattr(account, candidate, None)
        text = _text(value)
        if text:
            return text
        text = _text(extra.get(candidate))
        if text:
            return text
    return ""


def map_register_result_to_account(
    account: Mapping[str, Any] | object,
    *,
    proxy: str | None = None,
    source_type: str | None = None,
    plan_type: str = "free",
) -> dict[str, Any]:
    """组装 chatgpt2api `POST /api/accounts` 的 accounts[] 项。

    必填: access_token
    尽量带上: refresh_token / id_token / email / password / account_id / proxy
    有完整 Codex 三件套时标记 export_type=codex，便于后续导出与 refresh。
    """
    access_token = _field(account, "access_token", "token")
    if not access_token:
        raise ValueError("注册结果缺少 access_token，无法导入 chatgpt2api 号池")

    refresh_token = _field(account, "refresh_token")
    id_token = _field(account, "id_token")
    session_token = _field(account, "session_token")
    email = _field(account, "email")
    password = _field(account, "password")
    account_id = _field(account, "account_id", "user_id")
    workspace_id = _field(account, "workspace_id")

    # 有 refresh+id 时按 codex 源处理，chatgpt2api 可走 refresh_token 保活
    has_codex = bool(refresh_token and id_token)
    resolved_source = _text(source_type) or ("codex" if has_codex else "register")

    payload: dict[str, Any] = {
        "access_token": access_token,
        "source_type": resolved_source,
        "type": _text(plan_type) or "free",
        "status": "正常",
    }
    if email:
        payload["email"] = email
    if password:
        payload["password"] = password
    if account_id:
        payload["account_id"] = account_id
        payload["user_id"] = account_id
    if refresh_token:
        payload["refresh_token"] = refresh_token
    if id_token:
        payload["id_token"] = id_token
    if session_token:
        payload["session_token"] = session_token
    if workspace_id:
        payload["workspace_id"] = workspace_id
    if has_codex:
        payload["export_type"] = "codex"
    if proxy:
        payload["proxy"] = _text(proxy)

    return payload
