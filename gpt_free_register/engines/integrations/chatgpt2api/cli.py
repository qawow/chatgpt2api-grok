"""CLI: 注册结果 / JSON 导入到 chatgpt2api 号池。

用法:
  # 从 register_cli 输出的 JSON 文件导入
  python -m integrations.chatgpt2api.cli import --from-json result.json

  # 直接用 access_token 导入
  python -m integrations.chatgpt2api.cli import \\
      --access-token eyJ... --email a@b.com --refresh-token ...

  # 探测服务
  python -m integrations.chatgpt2api.cli ping

环境变量:
  CHATGPT2API_BASE_URL   默认 http://127.0.0.1:8000
  CHATGPT2API_AUTH_KEY   默认 chatgpt2api
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.proxy_env import load_dotenv  # noqa: E402
from integrations.chatgpt2api.client import ChatGPT2APIClient, ChatGPT2APIError  # noqa: E402
from integrations.chatgpt2api.mapper import map_register_result_to_account  # noqa: E402
from integrations.chatgpt2api.push import push_register_result  # noqa: E402


def _load_json_account(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("--from-json 必须是 JSON object（register_cli 输出）")
    return data


def cmd_ping(args) -> int:
    load_dotenv()
    client = ChatGPT2APIClient(base_url=args.base_url, auth_key=args.auth_key)
    try:
        result = client.list_accounts()
    except ChatGPT2APIError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    items = result.get("items") or []
    print(
        json.dumps(
            {
                "ok": True,
                "base_url": client.base_url,
                "account_count": len(items),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_import(args) -> int:
    load_dotenv()
    if args.from_json:
        account = _load_json_account(args.from_json)
    else:
        if not args.access_token:
            raise SystemExit("需要 --from-json 或 --access-token")
        account = {
            "email": args.email or "",
            "password": args.password or "",
            "user_id": args.account_id or "",
            "token": args.access_token,
            "extra": {
                "access_token": args.access_token,
                "refresh_token": args.refresh_token or "",
                "id_token": args.id_token or "",
                "session_token": args.session_token or "",
            },
        }

    result = push_register_result(
        account,
        proxy=args.proxy,
        source_type=args.source_type,
        plan_type=args.plan_type,
        base_url=args.base_url,
        auth_key=args.auth_key,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="chatgpt2api 号池桥接 CLI")
    parser.add_argument("--base-url", default=None, help="chatgpt2api 地址")
    parser.add_argument("--auth-key", default=None, help="管理端 auth-key")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ping = sub.add_parser("ping", help="探测服务与鉴权")
    p_ping.set_defaults(func=cmd_ping)

    p_imp = sub.add_parser("import", help="导入账号到号池")
    p_imp.add_argument("--from-json", default=None, help="register_cli 输出的 JSON 文件")
    p_imp.add_argument("--access-token", default=None)
    p_imp.add_argument("--refresh-token", default=None)
    p_imp.add_argument("--id-token", default=None)
    p_imp.add_argument("--session-token", default=None)
    p_imp.add_argument("--email", default=None)
    p_imp.add_argument("--password", default=None)
    p_imp.add_argument("--account-id", default=None)
    p_imp.add_argument("--proxy", default=None, help="账号绑定代理（号池侧）")
    p_imp.add_argument("--source-type", default=None, help="默认 codex/register 自动判断")
    p_imp.add_argument("--plan-type", default="free")
    p_imp.add_argument("--dry-run", action="store_true", help="只映射不 POST")
    p_imp.set_defaults(func=cmd_import)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
