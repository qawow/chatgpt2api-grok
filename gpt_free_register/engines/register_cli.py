"""最小 CLI：按平台调用注册机。

示例:
  python register_cli.py list
  python register_cli.py register cursor \\
      --mail-provider tempmail_lol_api \\
      --executor protocol \\
      --proxy http://127.0.0.1:7890

  # ChatGPT 注册成功后自动导入 chatgpt2api 号池
  python register_cli.py register chatgpt \\
      --executor protocol \\
      --mail-provider cloudflare_d1_api \\
      --push-chatgpt2api

说明:
  - 邮箱 / 验证码 / 接码 provider 配置可通过 --extra-json 或环境变量传入
  - 默认会把 config.extra 直接作为 runtime settings 覆盖，无需先写库
  - --push-chatgpt2api 仅对拿到 access_token 的成功结果生效
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bootstrap import bootstrap
from core.proxy_env import load_dotenv, mask_proxy, resolve_proxy
from core.base_mailbox import create_mailbox
from core.base_platform import RegisterConfig
from core.registry import get as get_platform


def _parse_extra(raw: str | None, pairs: list[str] | None) -> dict:
    extra: dict = {}
    if raw:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise SystemExit("--extra-json 必须是 JSON object")
        extra.update(payload)
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--set 参数格式应为 key=value，收到: {pair}")
        key, value = pair.split("=", 1)
        extra[key.strip()] = value
    return extra


def _should_push_chatgpt2api(args, platform_name: str) -> bool:
    if getattr(args, "push_chatgpt2api", False):
        return True
    flag = str(os.environ.get("CHATGPT2API_AUTO_PUSH") or "").strip().lower()
    if flag in ("1", "true", "yes", "on") and platform_name == "chatgpt":
        return True
    return False


def _push_to_chatgpt2api(account, args, proxy: str | None) -> dict | None:
    """成功注册后推送到 chatgpt2api；失败不抛，写入结果字段。"""
    from integrations.chatgpt2api.push import push_register_result

    bind_proxy = getattr(args, "chatgpt2api_proxy", None)
    if bind_proxy is None and getattr(args, "chatgpt2api_bind_register_proxy", False):
        bind_proxy = proxy

    return push_register_result(
        account,
        proxy=bind_proxy,
        source_type=getattr(args, "chatgpt2api_source_type", None),
        plan_type=getattr(args, "chatgpt2api_plan_type", None) or "free",
        base_url=getattr(args, "chatgpt2api_base_url", None),
        auth_key=getattr(args, "chatgpt2api_auth_key", None),
        dry_run=bool(getattr(args, "chatgpt2api_dry_run", False)),
    )


def cmd_list(_args):
    load_dotenv()
    platforms = bootstrap()
    for item in platforms:
        print(
            f"{item['name']:16s} {item['display_name']:16s} "
            f"executors={item.get('supported_executors')} "
            f"identity={item.get('supported_identity_modes')}"
        )


def cmd_register(args):
    load_dotenv()
    bootstrap()
    extra = _parse_extra(args.extra_json, args.set)
    if args.mail_provider:
        extra.setdefault("mail_provider", args.mail_provider)
    if args.captcha:
        # captcha_solver 走 RegisterConfig.captcha_solver；字段也写进 extra 方便覆盖
        extra.setdefault("captcha_solver", args.captcha)
    if args.sms_provider:
        extra.setdefault("sms_provider", args.sms_provider)
    if args.identity_provider:
        extra.setdefault("identity_provider", args.identity_provider)
    if args.oauth_provider:
        extra.setdefault("oauth_provider", args.oauth_provider)

    proxy = resolve_proxy(args.proxy)
    if proxy:
        print(f"[proxy] {mask_proxy(proxy)}")
    else:
        print("[proxy] (none) — 将使用直连，可能是家宽出口")

    config = RegisterConfig(
        executor_type=args.executor,
        captcha_solver=args.captcha or extra.get("captcha_solver", "auto"),
        proxy=proxy,
        extra=extra,
    )

    platform_cls = get_platform(args.platform)
    mailbox = None
    mail_provider = str(extra.get("mail_provider") or "").strip()
    if mail_provider and (extra.get("identity_provider") or "mailbox") in ("", "mailbox", "email", "mail"):
        mailbox = create_mailbox(mail_provider, extra=extra, proxy=proxy)

    platform = platform_cls(config=config, mailbox=mailbox)
    platform.set_logger(print)

    account = platform.register(email=args.email, password=args.password)
    result = {
        "platform": account.platform,
        "email": account.email,
        "password": account.password,
        "user_id": account.user_id,
        "token": account.token,
        "status": getattr(account.status, "value", account.status),
        "region": account.region,
        "trial_end_time": account.trial_end_time,
        "extra": account.extra,
    }

    # 注册成功（有 token）且开启推送时，导入 chatgpt2api 号池
    has_token = bool(
        str(account.token or "").strip()
        or str((account.extra or {}).get("access_token") or "").strip()
    )
    if has_token and _should_push_chatgpt2api(args, str(args.platform)):
        print("[chatgpt2api] pushing account into pool...")
        push_result = _push_to_chatgpt2api(account, args, proxy)
        result["chatgpt2api"] = push_result
        if push_result and push_result.get("ok"):
            print(
                f"[chatgpt2api] ok added={push_result.get('import', {}).get('added')} "
                f"skipped={push_result.get('import', {}).get('skipped')}"
            )
        else:
            err = (push_result or {}).get("error") or "unknown"
            print(f"[chatgpt2api] failed: {err}", file=sys.stderr)

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Any Register Engines CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出已加载平台注册机")
    p_list.set_defaults(func=cmd_list)

    p_reg = sub.add_parser("register", help="执行某个平台的注册机")
    p_reg.add_argument("platform", help="平台 name，如 cursor / chatgpt / kiro")
    p_reg.add_argument("--executor", default="protocol", choices=["protocol", "headless", "headed"])
    p_reg.add_argument("--mail-provider", default="", help="邮箱 provider_key，如 tempmail_lol_api")
    p_reg.add_argument("--captcha", default="", help="验证码 provider_key，如 yescaptcha_api / manual / auto")
    p_reg.add_argument("--sms-provider", default="", help="接码 provider_key")
    p_reg.add_argument("--identity-provider", default="", help="mailbox | oauth_browser")
    p_reg.add_argument("--oauth-provider", default="", help="google | microsoft | github ...")
    p_reg.add_argument("--proxy", default=None, help="出站代理；默认读 .env 的 REGISTER_PROXY_DEFAULT / REGISTER_PROXY")
    p_reg.add_argument("--email", default=None)
    p_reg.add_argument("--password", default=None)
    p_reg.add_argument("--extra-json", default=None, help="额外 JSON 配置，覆盖 provider settings")
    p_reg.add_argument("--set", action="append", default=[], help="额外 key=value，可重复")
    # chatgpt2api 号池接入
    p_reg.add_argument(
        "--push-chatgpt2api",
        action="store_true",
        help="注册成功后自动 POST 到 chatgpt2api /api/accounts",
    )
    p_reg.add_argument(
        "--chatgpt2api-base-url",
        default=None,
        help="默认 CHATGPT2API_BASE_URL 或 http://127.0.0.1:8000",
    )
    p_reg.add_argument(
        "--chatgpt2api-auth-key",
        default=None,
        help="默认 CHATGPT2API_AUTH_KEY 或 chatgpt2api",
    )
    p_reg.add_argument(
        "--chatgpt2api-proxy",
        default=None,
        help="写入号池的账号级 proxy（可与注册出站代理不同）",
    )
    p_reg.add_argument(
        "--chatgpt2api-bind-register-proxy",
        action="store_true",
        help="把本次注册出站代理写入号池账号 proxy 字段",
    )
    p_reg.add_argument(
        "--chatgpt2api-source-type",
        default=None,
        help="默认有 refresh+id 时为 codex，否则 register",
    )
    p_reg.add_argument("--chatgpt2api-plan-type", default="free")
    p_reg.add_argument(
        "--chatgpt2api-dry-run",
        action="store_true",
        help="只映射 payload，不真正 POST",
    )
    p_reg.set_defaults(func=cmd_register)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
