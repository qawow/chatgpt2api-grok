#!/usr/bin/env python3
"""Import CLIProxyAPI xAI auth JSON files into chatgpt2api Grok pool.

Example:
  python scripts/import_grok_cliproxy_auth.py \\
    --dir /root/work/grok-build-auth/cliproxyapi_auth \\
    --base-url http://127.0.0.1:8000 \\
    --auth-key chatgpt2api
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_accounts(directory: Path) -> tuple[list[dict], list[str]]:
    accounts: list[dict] = []
    skipped: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            skipped.append(f"{path.name}: load_error {exc}")
            continue
        if not isinstance(data, dict):
            skipped.append(f"{path.name}: root_not_object")
            continue
        type_hint = str(data.get("type") or "").strip().lower()
        token = str(data.get("access_token") or data.get("token") or "").strip()
        if not token:
            skipped.append(f"{path.name}: missing_access_token")
            continue
        if type_hint and type_hint not in {"xai", "grok", ""}:
            # allow empty type; skip explicit non-xai
            if type_hint not in {"xai", "grok"}:
                skipped.append(f"{path.name}: skip_type={type_hint}")
                continue
        accounts.append(data)
    return accounts, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import cliproxy xAI auth into Grok pool")
    parser.add_argument("--dir", required=True, help="Directory of cliproxyapi_auth JSON files")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--auth-key", default="chatgpt2api")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    directory = Path(args.dir).expanduser().resolve()
    if not directory.is_dir():
        print(f"not a directory: {directory}", file=sys.stderr)
        return 2

    accounts, skipped = load_accounts(directory)
    print(f"found={len(accounts)} skipped={len(skipped)} dir={directory}")
    for line in skipped[:20]:
        print(f"  skip: {line}")
    if not accounts:
        return 1
    if args.dry_run:
        emails = [str(a.get("email") or "?") for a in accounts]
        print("dry-run emails:", ", ".join(emails[:10]), ("..." if len(emails) > 10 else ""))
        return 0

    url = args.base_url.rstrip("/") + "/api/grok/accounts"
    body = json.dumps({"accounts": accounts}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {args.auth_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"connect failed: {exc.reason}", file=sys.stderr)
        return 1

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(raw[:500])
        return 0
    print(
        json.dumps(
            {
                "added": data.get("added"),
                "skipped": data.get("skipped"),
                "item_count": len(data.get("items") or []),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
