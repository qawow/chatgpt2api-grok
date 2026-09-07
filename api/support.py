from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

from fastapi import HTTPException, Request

from services.account_service import account_service
from services.auth_service import auth_service
from services.config import config
from services.grok_account_service import grok_account_service

BASE_DIR = Path(__file__).resolve().parents[1]
WEB_DIST_DIR = BASE_DIR / "web_dist"


def extract_bearer_token(authorization: str | None) -> str:
    scheme, _, value = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return ""
    return value.strip()


def _legacy_admin_identity(token: str) -> dict[str, object] | None:
    auth_key = str(config.auth_key or "").strip()
    if auth_key and token == auth_key:
        return {"id": "admin", "name": "管理员", "role": "admin"}
    return None


def require_identity(authorization: str | None) -> dict[str, object]:
    token = extract_bearer_token(authorization)
    identity = _legacy_admin_identity(token) or auth_service.authenticate(token)
    if identity is None:
        raise HTTPException(status_code=401, detail={"error": "密钥无效或已失效，请重新登录"})
    return identity


def require_auth_key(authorization: str | None) -> None:
    require_identity(authorization)


def require_admin(authorization: str | None) -> dict[str, object]:
    identity = require_identity(authorization)
    if identity.get("role") != "admin":
        raise HTTPException(status_code=403, detail={"error": "需要管理员权限才能执行这个操作"})
    return identity


def resolve_image_base_url(request: Request) -> str:
    return config.base_url or f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


def raise_image_quota_error(exc: Exception) -> None:
    message = str(exc)
    if "no available image quota" in message.lower():
        # Keep full diagnostic text (pool empty / revoked free / plan filter).
        raise HTTPException(status_code=429, detail={"error": message}) from exc
    raise HTTPException(status_code=502, detail={"error": message}) from exc


def start_limited_account_watcher(stop_event: Event) -> Thread:
    interval_seconds = config.refresh_account_interval_minute * 60

    def worker() -> None:
        while not stop_event.is_set():
            try:
                # list_* exclude 禁用 / revoked-cooldown / unrecoverable 异常.
                limited_tokens = account_service.list_limited_tokens()
                normal_tokens = account_service.list_normal_tokens()
                abnormal_tokens = account_service.list_abnormal_tokens()
                expiring_tokens = account_service.list_expiring_access_tokens()
                keepalive_tokens = account_service.list_refresh_token_keepalive_tokens()
                tokens = list(dict.fromkeys([*limited_tokens, *normal_tokens, *abnormal_tokens, *expiring_tokens]))
                expiring_token_set = set(expiring_tokens)
                keepalive_tokens = [token for token in keepalive_tokens if token not in expiring_token_set]
                if tokens:
                    print(
                        "[account-watcher] checking "
                        f"{len(limited_tokens)} limited accounts, "
                        f"{len(normal_tokens)} normal accounts, "
                        f"{len(abnormal_tokens)} abnormal accounts, "
                        f"{len(expiring_tokens)} expiring access tokens"
                    )
                    result = account_service.refresh_accounts(tokens)
                    skipped = int((result or {}).get("skipped") or 0)
                    if skipped:
                        print(f"[account-watcher] skipped {skipped} disabled/revoked-cooldown/unrecoverable")
                else:
                    # Quiet idle tick: free session-only pools often have nothing to probe.
                    pass
                if keepalive_tokens:
                    print(f"[account-watcher] keepalive {len(keepalive_tokens)} refresh tokens")
                    result = account_service.keepalive_refresh_tokens(keepalive_tokens)
                    if result.get("errors"):
                        print(f"[account-watcher] keepalive errors: {result['errors']}")
            except Exception as exc:
                print(f"[account-watcher] fail {exc}")
            stop_event.wait(interval_seconds)

    thread = Thread(target=worker, name="account-watcher", daemon=True)
    thread.start()
    return thread


def start_grok_account_watcher(stop_event: Event) -> Thread:
    """Periodic probe for the Grok/xAI pool (mirrors the ChatGPT watcher).

    Grok pool previously had no watcher: tokens stayed in 正常 status forever
    even after remote 401/403, so every request burned on dead accounts until a
    manual /refresh. This watcher re-probes 正常/限流/异常 accounts (those with
    refresh_token) so dead tokens are marked 异常 and recovered tokens are
    restored automatically.
    """
    interval_seconds = config.refresh_account_interval_minute * 60

    def worker() -> None:
        while not stop_event.is_set():
            try:
                tokens = grok_account_service.list_watchable_tokens()
                if not tokens:
                    # Empty pool or only session-only entries — quiet idle tick.
                    pass
                else:
                    print(f"[grok-watcher] checking {len(tokens)} grok accounts")
                    result = grok_account_service.refresh_accounts(tokens)
                    errors = (result or {}).get("errors") or []
                    if errors:
                        print(f"[grok-watcher] errors: {errors}")
            except Exception as exc:
                print(f"[grok-watcher] fail {exc}")
            stop_event.wait(interval_seconds)

    thread = Thread(target=worker, name="grok-account-watcher", daemon=True)
    thread.start()
    return thread


_SPA_FALLBACK_BLOCKLIST = ("_next/", "api/", "v1/", "auth/")


def should_skip_spa_fallback(requested_path: str) -> bool:
    """Unknown API/auth/asset paths must 404, not the dashboard HTML.

    The July settings UI still GETs removed /api/cpa/pools. Serving index.html
    as 200 made axios treat the page as JSON and crash the settings tab.
    """
    return requested_path.strip("/").startswith(_SPA_FALLBACK_BLOCKLIST)


def resolve_web_asset(requested_path: str) -> Path | None:
    if not WEB_DIST_DIR.exists():
        return None
    clean_path = requested_path.strip("/")
    base_dir = WEB_DIST_DIR.resolve()
    candidates = [base_dir / "index.html"] if not clean_path else [
        base_dir / Path(clean_path),
        base_dir / clean_path / "index.html",
        base_dir / f"{clean_path}.html",
    ]
    for candidate in candidates:
        try:
            candidate.resolve().relative_to(base_dir)
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None
