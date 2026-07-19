"""注册成功后推送到 chatgpt2api 号池。"""
from __future__ import annotations

from typing import Any, Mapping

from .client import ChatGPT2APIClient, ChatGPT2APIError
from .mapper import map_register_result_to_account


def push_register_result(
    account: Mapping[str, Any] | object,
    *,
    proxy: str | None = None,
    source_type: str | None = None,
    plan_type: str = "free",
    client: ChatGPT2APIClient | None = None,
    base_url: str | None = None,
    auth_key: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """把一次注册结果映射并导入 chatgpt2api。

    返回:
      {
        "payload": <导入体>,
        "import": <API 响应或 dry_run 标记>,
        "ok": bool,
        "error": str | None,
      }
    """
    payload = map_register_result_to_account(
        account,
        proxy=proxy,
        source_type=source_type,
        plan_type=plan_type,
    )
    result: dict[str, Any] = {
        "payload": {
            # 不回显完整 token，仅给日志用摘要
            "email": payload.get("email"),
            "account_id": payload.get("account_id"),
            "source_type": payload.get("source_type"),
            "export_type": payload.get("export_type"),
            "has_refresh_token": bool(payload.get("refresh_token")),
            "has_id_token": bool(payload.get("id_token")),
            "has_session_token": bool(payload.get("session_token")),
            "has_password": bool(payload.get("password")),
            "proxy": payload.get("proxy") or "",
            "access_token_len": len(str(payload.get("access_token") or "")),
        },
        "import": None,
        "ok": False,
        "error": None,
    }

    if dry_run:
        result["import"] = {"dry_run": True, "would_post": True}
        result["ok"] = True
        return result

    api = client or ChatGPT2APIClient(base_url=base_url, auth_key=auth_key)
    try:
        imported = api.add_account(payload)
        result["import"] = {
            "added": imported.get("added"),
            "skipped": imported.get("skipped"),
            "refreshed": imported.get("refreshed"),
            "errors": imported.get("errors") or [],
            "item_count": len(imported.get("items") or []),
        }
        result["ok"] = True
        return result
    except (ChatGPT2APIError, ValueError) as exc:
        result["error"] = str(exc)
        return result
