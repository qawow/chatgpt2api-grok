from __future__ import annotations

import asyncio
import io
import json
import re
import uuid
import zipfile
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field

from services.auth_service import auth_service

from api.support import require_admin
from services.account_service import account_service
from services.oauth_login_service import OAuthLoginError, oauth_login_service



class UserKeyCreateRequest(BaseModel):
    name: str = ""


class UserKeyUpdateRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    key: str | None = None


class AccountCreateRequest(BaseModel):
    tokens: list[str] = Field(default_factory=list)
    accounts: list[dict[str, Any]] = Field(default_factory=list)


class AccountDeleteRequest(BaseModel):
    tokens: list[str] = Field(default_factory=list)


class AccountRefreshRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)


class AccountExportRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)
    format: Literal["json", "zip"] = "json"


class AccountUpdateRequest(BaseModel):
    access_token: str = ""
    type: str | None = None
    status: str | None = None
    quota: int | None = None
    proxy: str | None = None


class OAuthLoginStartRequest(BaseModel):
    """起始 OAuth 桥。email_hint 可选，仅用于让 OpenAI 登录页预填邮箱。"""
    email_hint: str = ""


class OAuthLoginFinishRequest(BaseModel):
    """提交 callback。callback 既可以是完整 URL 也可以只填 code。

    replace_access_token：可选。成功写入新 token 后删除该旧 access_token
    （浏览器 OAuth 备用路径：把 session_only 号升级成完整凭证；主路径见 POST /api/accounts/codex-upgrade）。
    """
    session_id: str = ""
    callback: str = ""
    replace_access_token: str = ""


class CodexUpgradeRequest(BaseModel):
    """协议 Codex OTP 补 refresh：对已有 session_only 邮箱再跑一次 Codex client_id + 邮箱 OTP。

    成功写入 refresh/id 后删除旧 access_token 行；add_phone 等失败软返回，不删 session 行。
    """
    email: str = ""
    access_token: str = ""
    password: str = ""


def _account_payload_token(item: dict[str, Any]) -> str:
    return str(item.get("access_token") or item.get("accessToken") or "").strip()


def _unique_tokens(tokens: list[str]) -> list[str]:
    return list(dict.fromkeys(str(token or "").strip() for token in tokens if str(token or "").strip()))


def _download_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_export_name(value: str, fallback: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return (clean or fallback)[:80]


def _account_zip_bytes(items: list[dict[str, str]]) -> bytes:
    buf = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, item in enumerate(items, start=1):
            raw_name = item.get("email") or item.get("account_id") or f"account-{index:03d}"
            base_name = _safe_export_name(raw_name, f"account-{index:03d}")
            name = base_name
            suffix = 2
            while name in used_names:
                name = f"{base_name}-{suffix}"
                suffix += 1
            used_names.add(name)
            archive.writestr(
                f"{name}.json",
                json.dumps(item, ensure_ascii=False, indent=2) + "\n",
            )
    return buf.getvalue()


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/auth/users")
    async def list_user_keys(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": auth_service.list_keys(role="user")}

    @router.post("/api/auth/users")
    async def create_user_key(body: UserKeyCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            item, raw_key = auth_service.create_key(role="user", name=body.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"item": item, "key": raw_key, "items": auth_service.list_keys(role="user")}

    @router.post("/api/auth/users/{key_id}")
    async def update_user_key(
            key_id: str,
            body: UserKeyUpdateRequest,
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        updates = {
            key: value
            for key, value in {
                "name": body.name,
                "enabled": body.enabled,
                "key": body.key,
            }.items()
            if value is not None
        }
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "还没有检测到改动，请修改后再保存"})
        try:
            item = auth_service.update_key(key_id, updates, role="user")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "这条用户密钥不存在，可能已经被删除"})
        return {"item": item, "items": auth_service.list_keys(role="user")}

    @router.delete("/api/auth/users/{key_id}")
    async def delete_user_key(key_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not auth_service.delete_key(key_id, role="user"):
            raise HTTPException(status_code=404, detail={"error": "这条用户密钥不存在，可能已经被删除"})
        return {"items": auth_service.list_keys(role="user")}

    @router.get("/api/accounts")
    async def get_accounts(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": account_service.list_accounts()}

    @router.post("/api/accounts")
    async def create_accounts(body: AccountCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        account_payloads = [item for item in body.accounts if isinstance(item, dict)]
        payload_tokens = [_account_payload_token(item) for item in account_payloads]
        tokens = _unique_tokens([*body.tokens, *payload_tokens])
        if not tokens:
            raise HTTPException(status_code=400, detail={"error": "tokens is required"})
        if account_payloads:
            result = account_service.add_account_items(account_payloads)
            payload_token_set = set(_unique_tokens(payload_tokens))
            extra_tokens = [token for token in tokens if token not in payload_token_set]
            if extra_tokens:
                extra_result = account_service.add_accounts(extra_tokens)
                result["added"] = int(result.get("added") or 0) + int(extra_result.get("added") or 0)
                result["skipped"] = int(result.get("skipped") or 0) + int(extra_result.get("skipped") or 0)
        else:
            result = account_service.add_accounts(tokens)
        refresh_result = account_service.refresh_accounts(tokens)
        return {
            **result,
            "refreshed": refresh_result.get("refreshed", 0),
            "errors": refresh_result.get("errors", []),
            "items": refresh_result.get("items", result.get("items", [])),
        }

    @router.delete("/api/accounts")
    async def delete_accounts(body: AccountDeleteRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        tokens = [str(token or "").strip() for token in body.tokens if str(token or "").strip()]
        if not tokens:
            raise HTTPException(status_code=400, detail={"error": "tokens is required"})
        return account_service.delete_accounts(tokens)

    @router.post("/api/accounts/refresh")
    async def refresh_accounts(body: AccountRefreshRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        access_tokens = [str(token or "").strip() for token in body.access_tokens if str(token or "").strip()]
        if not access_tokens:
            access_tokens = account_service.list_tokens()
        if not access_tokens:
            raise HTTPException(status_code=400, detail={"error": "access_tokens is required"})

        progress_id = str(uuid.uuid4())
        # Init BEFORE create_task so UI poll never races a missing progress_id
        # (also covers free/session_only filtered-to-empty refresh finishing immediately).
        account_service.init_refresh_progress(progress_id, len(access_tokens))

        async def _do_refresh():
            try:
                await run_in_threadpool(account_service.refresh_accounts, access_tokens, progress_id, False)
            except Exception as e:
                account_service.finish_refresh_progress(progress_id, error=str(e))

        asyncio.create_task(_do_refresh())

        return {"progress_id": progress_id}

    @router.get("/api/accounts/refresh/progress/{progress_id}")
    async def get_refresh_progress(progress_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        progress = account_service.get_refresh_progress(progress_id)
        if progress is None:
            raise HTTPException(status_code=404, detail={"error": "progress not found"})
        return progress

    @router.post("/api/accounts/re-login")
    async def re_login_accounts(body: AccountRefreshRequest, authorization: str | None = Header(default=None)):
        """对选中账号执行密码重新登录流程（密码登录→验证码登录→刷新token）。"""
        require_admin(authorization)
        access_tokens = [str(token or "").strip() for token in body.access_tokens if str(token or "").strip()]
        if not access_tokens:
            raise HTTPException(status_code=400, detail={"error": "access_tokens is required"})

        progress_id = str(uuid.uuid4())
        account_service.init_relogin_progress(progress_id, len(access_tokens))

        async def _do_relogin():
            try:
                await run_in_threadpool(account_service.re_login_accounts, access_tokens, progress_id)
            except Exception as e:
                account_service.finish_relogin_progress(progress_id, error=str(e))

        asyncio.create_task(_do_relogin())

        return {"progress_id": progress_id}

    @router.get("/api/accounts/re-login/progress/{progress_id}")
    async def get_relogin_progress(progress_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        progress = account_service.get_relogin_progress(progress_id)
        if progress is None:
            raise HTTPException(status_code=404, detail={"error": "progress not found"})
        return progress

    @router.post("/api/accounts/export")
    async def export_accounts(body: AccountExportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        access_tokens = _unique_tokens(body.access_tokens)
        items = account_service.build_export_items(access_tokens)
        if not items:
            raise HTTPException(
                status_code=400,
                detail={"error": "没有可导出的完整账号，需要同时有 access_token、refresh_token 和 id_token"},
            )

        timestamp = _download_timestamp()
        if body.format == "zip":
            content = _account_zip_bytes(items)
            return Response(
                content,
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="codex-accounts-{timestamp}.zip"'},
            )

        payload: dict[str, str] | list[dict[str, str]] = items[0] if len(items) == 1 else items
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="codex-accounts-{timestamp}.json"'},
        )

    @router.post("/api/accounts/update")
    async def update_account(body: AccountUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        access_token = str(body.access_token or "").strip()
        if not access_token:
            raise HTTPException(status_code=400, detail={"error": "access_token is required"})
        updates = {key: value for key, value in {"type": body.type, "status": body.status, "quota": body.quota, "proxy": body.proxy}.items() if value is not None}
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "还没有检测到改动，请修改后再保存"})
        account = account_service.update_account(access_token, updates)
        if account is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        return {"item": account, "items": account_service.list_accounts()}

    @router.post("/api/accounts/oauth/start")
    async def start_oauth_login(
            body: OAuthLoginStartRequest,
            authorization: str | None = Header(default=None),
    ):
        """登记一次 PKCE 会话，返回可让用户浏览器打开的 authorize URL。"""
        require_admin(authorization)
        try:
            return await run_in_threadpool(oauth_login_service.start, body.email_hint)
        except OAuthLoginError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/accounts/oauth/finish")
    async def finish_oauth_login(
            body: OAuthLoginFinishRequest,
            authorization: str | None = Header(default=None),
    ):
        """收用户从浏览器抓回的 callback URL / code，换出 token 三件套并落盘。"""
        require_admin(authorization)
        # 入参日志：截断敏感字段，仅保留前几位，方便排错而不泄密
        cb_preview = (body.callback or "")[:80]
        sid_preview = (body.session_id or "")[:8]
        print(
            f"[oauth-login] finish called: session_id={sid_preview}..., callback_preview={cb_preview!r}",
            flush=True,
        )
        try:
            tokens = await run_in_threadpool(oauth_login_service.finish, body.session_id, body.callback)
        except OAuthLoginError as exc:
            print(f"[oauth-login] finish rejected: {exc}", flush=True)
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

        payload = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "id_token": tokens["id_token"],
            "source_type": "oauth_login",
            # OAuth 完整凭证：显式清掉 session_only/fragile（normalize 也会按 refresh 重算）
            "session_only": False,
            "fragile": False,
        }
        add_result = await run_in_threadpool(account_service.add_account_items, [payload])

        # 升级路径：用新 OAuth 行替换旧的 session_only access_token，避免号池里残留脆弱重复项
        replaced = 0
        old_token = str(body.replace_access_token or "").strip()
        new_token = str(tokens["access_token"] or "").strip()
        if old_token and new_token and old_token != new_token:
            try:
                del_result = await run_in_threadpool(account_service.delete_accounts, [old_token])
                replaced = int(del_result.get("removed") or 0)
            except Exception as exc:
                print(
                    f"[oauth-login] replace old token failed after oauth add: {exc}",
                    flush=True,
                )

        refresh_result = await run_in_threadpool(
            account_service.refresh_accounts, [tokens["access_token"]]
        )
        return {
            **add_result,
            "replaced": replaced,
            "refreshed": refresh_result.get("refreshed", 0),
            "errors": refresh_result.get("errors", []),
            "items": refresh_result.get("items", add_result.get("items", [])),
        }

    @router.post("/api/accounts/codex-upgrade")
    async def codex_upgrade_account(
            body: CodexUpgradeRequest,
            authorization: str | None = Header(default=None),
    ):
        """对 session_only 账号再跑 Codex CLI OTP，补齐 refresh_token（无需浏览器粘贴 callback）。"""
        require_admin(authorization)
        from services.codex_upgrade_service import upgrade_session_account_via_codex

        email = str(body.email or "").strip().lower()
        access_token = str(body.access_token or "").strip()
        password = str(body.password or "").strip()

        # Resolve email/password from pool if only access_token given
        if access_token and (not email or not password):
            try:
                accounts = account_service.list_accounts() or []
            except Exception:
                accounts = []
            for acc in accounts if isinstance(accounts, list) else []:
                if not isinstance(acc, dict):
                    continue
                if str(acc.get("access_token") or "").strip() != access_token:
                    continue
                if not email:
                    email = str(acc.get("email") or "").strip().lower()
                if not password:
                    password = str(acc.get("password") or "").strip()
                break

        if not email or "@" not in email:
            raise HTTPException(status_code=400, detail={"error": "email is required"})

        print(
            f"[codex-upgrade] start email={email} replace={bool(access_token)}",
            flush=True,
        )
        result = await run_in_threadpool(
            upgrade_session_account_via_codex,
            email=email,
            replace_access_token=access_token,
            password=password,
            log=lambda m: print(f"[codex-upgrade] {m}", flush=True),
        )
        if not result.get("ok"):
            # Soft-fail expected free gates (add_phone etc.) — 422 so UI can toast without 500
            reason = str(result.get("reason") or "failed")
            error = str(result.get("error") or "Codex 补齐失败")
            raise HTTPException(
                status_code=422,
                detail={
                    "error": error,
                    "reason": reason,
                    "email": email,
                    "logs": list(result.get("logs") or [])[-20:],
                },
            )
        items = result.get("items")
        if not items:
            try:
                items = account_service.list_accounts()
            except Exception:
                items = []
        return {
            "ok": True,
            "email": email,
            "added": int(result.get("added") or 0),
            "replaced": int(result.get("replaced") or 0),
            "items": items,
            "logs": list(result.get("logs") or [])[-20:],
        }

    return router
