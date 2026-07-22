"""Protocol Codex CLI OTP upgrade for existing free/session accounts.

Re-runs the register step-15 path (Codex client_id + mailbox OTP) against an
already-registered email so we can recover refresh_token / id_token without a
browser OAuth paste flow.

Soft-fails on add_phone and other expected free-account gates — caller keeps
the session_only row.
"""
from __future__ import annotations

import json
import time
import traceback
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from gpt_free_register.runner import (
    _bootstrap,
    _clean,
    _create_mailbox,
    _ensure_runtime_deps,
    default_engines_dir,
)


def _log(log: Callable[[str], None] | None, msg: str) -> None:
    (log or print)(msg)


def _bind_existing_mailbox_account(mailbox: Any, email: str):
    """Bind CFD1 (or other) mailbox to a known address without minting a new local-part."""
    from core.base_mailbox import MailboxAccount

    addr = _clean(email).lower()
    if not addr or "@" not in addr:
        raise ValueError("email is required for Codex upgrade")
    domain = addr.split("@", 1)[-1]
    return MailboxAccount(
        email=addr,
        account_id=addr,
        extra={
            "provider_resource": {
                "provider_type": "mailbox",
                "provider_name": "cloudflare_d1",
                "resource_type": "mailbox",
                "resource_identifier": addr,
                "handle": addr,
                "display_name": addr,
                "metadata": {"email": addr, "domain": domain},
            },
            "fixed_email": True,
        },
    )


def _apply_env_overrides(cfg: dict[str, Any]) -> None:
    if _clean(cfg.get("cfd1_domain")):
        import os

        os.environ["CFD1_DOMAIN"] = _clean(cfg.get("cfd1_domain"))
    import os

    for env_key, cfg_key in (
        ("CFD1_API_TOKEN", "cfd1_api_token"),
        ("CFD1_ACCOUNT_ID", "cfd1_account_id"),
        ("CFD1_DATABASE_ID", "cfd1_database_id"),
        ("CFD1_LOCAL_PART_PREFIX", "cfd1_local_part_prefix"),
        ("CFD1_LOCAL_PART_LENGTH", "cfd1_local_part_length"),
        ("REGISTER_PROXY", "proxy"),
    ):
        val = _clean(cfg.get(cfg_key))
        if val:
            os.environ[env_key] = val


def obtain_codex_tokens_for_email(
    *,
    email: str,
    settings: dict[str, Any] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run Codex CLI OAuth + OTP for an existing email.

    Returns::

        {
          "ok": bool,
          "email": str,
          "access_token": str,
          "refresh_token": str,
          "id_token": str,
          "account_id": str,
          "error": str | None,
          "reason": str | None,  # add_phone | otp_failed | no_callback | ...
          "logs": list[str],
        }
    """
    cfg = dict(settings or {})
    logs: list[str] = []

    def _append(msg: str) -> None:
        text = str(msg or "").strip()
        if not text:
            return
        logs.append(text[:500])
        _log(log, text)

    addr = _clean(email).lower()
    if not addr or "@" not in addr:
        return {
            "ok": False,
            "email": addr,
            "access_token": "",
            "refresh_token": "",
            "id_token": "",
            "account_id": "",
            "error": "email is required",
            "reason": "invalid_email",
            "logs": logs,
        }

    engines_dir = Path(_clean(cfg.get("engines_dir")) or default_engines_dir())
    if not engines_dir.is_dir():
        return {
            "ok": False,
            "email": addr,
            "access_token": "",
            "refresh_token": "",
            "id_token": "",
            "account_id": "",
            "error": f"注册机目录不存在: {engines_dir}",
            "reason": "engines_missing",
            "logs": logs,
        }

    try:
        _bootstrap(engines_dir)
        _apply_env_overrides(cfg)

        from core.proxy_env import mask_proxy, resolve_proxy

        proxy = resolve_proxy(cfg.get("proxy") if _clean(cfg.get("proxy")) else None)
        _ensure_runtime_deps(proxy)
        _append(f"[codex-upgrade] email={addr} proxy={mask_proxy(proxy) if proxy else '(none)'}")

        mail_provider = _clean(cfg.get("mail_provider")) or "cloudflare_d1_api"
        extra: dict[str, Any] = {
            "mail_provider": mail_provider,
            "identity_provider": "mailbox",
        }
        for key in (
            "cfd1_api_token",
            "cfd1_account_id",
            "cfd1_database_id",
            "cfd1_domain",
            "cfd1_local_part_prefix",
            "cfd1_local_part_length",
            "cfd1_api_base",
            "cfd1_table",
        ):
            if _clean(cfg.get(key)):
                extra[key] = _clean(cfg.get(key))

        # CFD1 reads are independent of OpenAI egress; avoid SOCKS timeouts on mail poll.
        mailbox_proxy = None
        if mail_provider not in {"cloudflare_d1_api", "cloudflare_d1", "cfd1"}:
            mailbox_proxy = proxy
        mailbox = _create_mailbox(mail_provider, extra, mailbox_proxy)
        mailbox_account = _bind_existing_mailbox_account(mailbox, addr)

        from platforms.chatgpt.protocol_mailbox import _MailboxEmailService
        from platforms.chatgpt.register import (
            RegistrationEngine,
            SentinelPayload,
            _SentinelTokenGenerator,
        )
        from platforms.chatgpt.oauth import generate_oauth_url, submit_callback_url
        from platforms.chatgpt.constants import (
            CODEX_CLIENT_ID,
            CODEX_REDIRECT_URI,
            CODEX_SCOPE,
            OPENAI_API_ENDPOINTS,
            OPENAI_AUTH,
            SENTINEL_FRAME_URL,
        )
        from platforms.chatgpt.http_client import OpenAIHTTPClient

        email_service = _MailboxEmailService(
            mailbox=mailbox,
            mailbox_account=mailbox_account,
            provider=mail_provider,
        )
        # Snapshot baseline mail ids for this fixed address before OTP send.
        email_service.create_email()

        engine = RegistrationEngine(
            email_service=email_service,
            proxy_url=proxy,
            callback_logger=_append,
        )
        engine.email = addr
        engine.email_info = {
            "email": addr,
            "service_id": getattr(mailbox_account, "account_id", "") or addr,
            "token": getattr(mailbox_account, "account_id", "") or addr,
        }

        _append("Codex CLI OTP 补齐开始…")
        codex_oauth = generate_oauth_url(
            redirect_uri=CODEX_REDIRECT_URI,
            scope=CODEX_SCOPE,
            client_id=CODEX_CLIENT_ID,
        )
        login_client = OpenAIHTTPClient(
            proxy_url=proxy,
            profile=getattr(engine, "_browser_profile", None),
        )
        login_session = login_client.session
        login_session.get(codex_oauth.auth_url, timeout=15)
        did2 = login_session.cookies.get("oai-did", "")
        _append(f"Codex login did: {str(did2)[:20]}...")

        sen2 = None
        try:
            ua2 = getattr(login_client, "user_agent", None) or login_client.default_headers.get(
                "User-Agent", ""
            )
            gen2 = _SentinelTokenGenerator(
                did2, ua2, profile=getattr(engine, "_browser_profile", None)
            )
            sp2 = gen2.generate_requirements_token()
            sr2 = json.dumps(
                {"p": sp2, "id": did2, "flow": "authorize_continue"},
                separators=(",", ":"),
            )
            sr2_resp = login_client.post(
                OPENAI_API_ENDPOINTS["sentinel"],
                headers={
                    "origin": "https://sentinel.openai.com",
                    "referer": SENTINEL_FRAME_URL,
                    "content-type": "text/plain;charset=UTF-8",
                },
                data=sr2,
            )
            if sr2_resp.status_code == 200:
                d2 = sr2_resp.json()
                pm2 = d2.get("proofofwork") or {}
                if pm2.get("required") and pm2.get("seed"):
                    sp2 = gen2.generate_token(
                        str(pm2.get("seed") or ""),
                        str(pm2.get("difficulty") or "0"),
                    )
                tr2 = (d2.get("turnstile") or {}).get("dx", "")
                tv2 = ""
                if tr2:
                    try:
                        tv2 = gen2.decrypt_turnstile(tr2, sp2)
                    except Exception:
                        tv2 = ""
                sen2 = SentinelPayload(
                    p=sp2, t=tv2, c=str(d2.get("token") or ""), flow="authorize_continue"
                )
                _append("Codex sentinel 获取成功")
        except Exception as exc:
            _append(f"Codex sentinel 失败: {exc}")

        signup_headers = {
            "referer": f"{OPENAI_AUTH}/log-in",
            "accept": "application/json",
            "content-type": "application/json",
        }
        if sen2 and did2:
            signup_headers["openai-sentinel-token"] = json.dumps(
                {
                    "p": sen2.p,
                    "t": sen2.t,
                    "c": sen2.c,
                    "id": did2,
                    "flow": sen2.flow,
                },
                separators=(",", ":"),
            )

        signup_body = json.dumps(
            {"username": {"value": addr, "kind": "email"}, "screen_hint": "signup"}
        )
        signup_resp = login_session.post(
            OPENAI_API_ENDPOINTS["signup"], headers=signup_headers, data=signup_body
        )
        _append(f"Codex authorize/continue: {signup_resp.status_code}")
        if signup_resp.status_code != 200:
            return {
                "ok": False,
                "email": addr,
                "access_token": "",
                "refresh_token": "",
                "id_token": "",
                "account_id": "",
                "error": f"authorize/continue 失败: {signup_resp.text[:200]}",
                "reason": "authorize_continue_failed",
                "logs": logs[-80:],
            }

        page_type = (signup_resp.json() or {}).get("page", {}).get("type", "")
        _append(f"Codex page_type: {page_type}")

        if page_type not in ("email_otp_send", "email_otp_verification"):
            return {
                "ok": False,
                "email": addr,
                "access_token": "",
                "refresh_token": "",
                "id_token": "",
                "account_id": "",
                "error": f"Codex 非 OTP 流程 ({page_type})",
                "reason": f"page_{page_type or 'unknown'}",
                "logs": logs[-80:],
            }

        if page_type == "email_otp_send":
            login_session.get(
                OPENAI_API_ENDPOINTS["send_otp"],
                headers={"referer": f"{OPENAI_AUTH}/email-verification"},
                timeout=15,
            )
            _append("Codex OTP 已发送")

        engine._otp_sent_at = time.time()
        # Prefer mailbox poll with baseline; avoid register-session resend side effects.
        code = None
        try:
            code = email_service.get_verification_code(
                email=addr,
                email_id=engine.email_info.get("service_id"),
                timeout=120,
                otp_sent_at=engine._otp_sent_at,
            )
        except Exception as exc:
            _append(f"OTP 收信失败: {exc}")
        if not code:
            # Fallback to engine helper (may attempt resend on unset session — soft).
            try:
                code = engine._get_verification_code()
            except Exception as exc:
                _append(f"OTP 回退失败: {exc}")
        if not code:
            return {
                "ok": False,
                "email": addr,
                "access_token": "",
                "refresh_token": "",
                "id_token": "",
                "account_id": "",
                "error": "Codex OTP 获取失败",
                "reason": "otp_failed",
                "logs": logs[-80:],
            }
        _append("Codex OTP 已获取")

        otp_resp = login_session.post(
            OPENAI_API_ENDPOINTS["validate_otp"],
            headers={
                "referer": f"{OPENAI_AUTH}/email-verification",
                "accept": "application/json",
                "content-type": "application/json",
            },
            data=json.dumps({"code": code}),
        )
        _append(f"Codex OTP validate: {otp_resp.status_code}")
        if otp_resp.status_code != 200:
            return {
                "ok": False,
                "email": addr,
                "access_token": "",
                "refresh_token": "",
                "id_token": "",
                "account_id": "",
                "error": f"Codex OTP 验证失败: {otp_resp.text[:200]}",
                "reason": "otp_validate_failed",
                "logs": logs[-80:],
            }

        otp_data = otp_resp.json() or {}
        otp_page = (otp_data.get("page") or {}).get("type", "")
        _append(f"Codex OTP -> page_type={otp_page}")
        if otp_page == "add_phone":
            return {
                "ok": False,
                "email": addr,
                "access_token": "",
                "refresh_token": "",
                "id_token": "",
                "account_id": "",
                "error": "add_phone required",
                "reason": "add_phone",
                "logs": logs[-80:],
            }

        _append("Codex: 重新访问 OAuth URL…")
        resp = login_session.get(codex_oauth.auth_url, allow_redirects=False, timeout=15)
        codex_callback = None
        current_url = codex_oauth.auth_url
        for i in range(15):
            if resp.status_code not in (301, 302, 303, 307, 308):
                break
            location = resp.headers.get("Location", "")
            if not location:
                break
            next_url = urllib.parse.urljoin(current_url, location)
            _append(f"Codex 重定向 {i + 1}: {next_url[:80]}...")
            if "code=" in next_url and "state=" in next_url:
                codex_callback = next_url
                break
            current_url = next_url
            resp = login_session.get(current_url, allow_redirects=False, timeout=15)

        if not codex_callback:
            return {
                "ok": False,
                "email": addr,
                "access_token": "",
                "refresh_token": "",
                "id_token": "",
                "account_id": "",
                "error": f"Codex callback 未获取 (status={getattr(resp, 'status_code', '?')})",
                "reason": "no_callback",
                "logs": logs[-80:],
            }

        token_json = submit_callback_url(
            callback_url=codex_callback,
            expected_state=codex_oauth.state,
            code_verifier=codex_oauth.code_verifier,
            redirect_uri=CODEX_REDIRECT_URI,
            client_id=CODEX_CLIENT_ID,
            proxy_url=proxy,
        )
        token_info = json.loads(token_json)
        access = _clean(token_info.get("access_token"))
        refresh = _clean(token_info.get("refresh_token"))
        id_token = _clean(token_info.get("id_token"))
        account_id = _clean(token_info.get("account_id"))
        if not access or not refresh:
            return {
                "ok": False,
                "email": addr,
                "access_token": access,
                "refresh_token": refresh,
                "id_token": id_token,
                "account_id": account_id,
                "error": "Codex token 响应缺少 access/refresh",
                "reason": "incomplete_tokens",
                "logs": logs[-80:],
            }

        _append("Codex token 成功（含 refresh_token）")
        return {
            "ok": True,
            "email": addr,
            "access_token": access,
            "refresh_token": refresh,
            "id_token": id_token,
            "account_id": account_id,
            "error": None,
            "reason": None,
            "logs": logs[-80:],
        }
    except Exception as exc:
        _append(f"Codex 补齐异常: {exc}")
        return {
            "ok": False,
            "email": addr,
            "access_token": "",
            "refresh_token": "",
            "id_token": "",
            "account_id": "",
            "error": str(exc)[:400],
            "reason": "exception",
            "logs": logs[-80:] + [traceback.format_exc()[-600:]],
        }
