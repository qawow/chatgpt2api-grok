"""出站代理环境解析。

优先顺序:
1. 显式传入的 proxy 参数
2. REGISTER_PROXY / AAR_PROXY
3. ALL_PROXY / all_proxy / HTTPS_PROXY / HTTP_PROXY
4. REGISTER_PROXY_DEFAULT（可选默认代理，推荐写在 .env）

不会在源码里硬编码账号密码。

粘性出口（sticky）:
轮换代理默认每条 TCP 连接换一次出口 IP。一次注册要跨 chatgpt.com /
auth.openai.com 打 30+ 个请求，出口中途变化会被 Cloudflare 判成会话劫持
（403 challenge），OAuth state 也会 409 invalid_state。把会话 ID 拼进
代理用户名即可把出口钉死（Resin 用 `用户名.会话ID`，实测 8 条并发连接 +
6 次跨主机请求同 IP）。用 REGISTER_PROXY_STICKY 开启。
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Optional


# Do not leak these into the FastAPI process from gpt_register.env / engines/.env.
# tiktoken, Grok stdlib requests, urllib, and D1 would otherwise inherit a dead SOCKS.
_PROCESS_WIDE_PROXY_KEYS = frozenset({
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
})


def load_dotenv(path: str | Path | None = None) -> Path | None:
    """轻量加载 .env 到 os.environ（不覆盖已有环境变量）。"""
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    else:
        here = Path(__file__).resolve().parent.parent
        candidates.extend([
            here / ".env",
            here / "data" / ".env",
            Path.cwd() / ".env",
        ])
    for env_path in candidates:
        if not env_path.is_file():
            continue
        try:
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'").strip('"')
                if key in _PROCESS_WIDE_PROXY_KEYS:
                    continue
                if key and key not in os.environ:
                    os.environ[key] = value
            return env_path
        except Exception:
            continue
    return None


def _first_env(*keys: str) -> str:
    for key in keys:
        value = str(os.getenv(key, "") or "").strip()
        if value:
            return value
    return ""


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}


# `socks5h://user.oa1:pass@host:port` — the session id rides on the username.
_PROXY_URL_RE = re.compile(
    r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)?"
    r"(?:(?P<user>[^:@/]*)(?::(?P<password>[^@/]*))?@)?"
    r"(?P<host>[^:/?#]+)"
    r"(?::(?P<port>\d+))?$"
)


def sticky_session_id() -> str:
    """Session id for a sticky exit. Override with REGISTER_PROXY_SESSION_ID."""
    explicit = _first_env("REGISTER_PROXY_SESSION_ID")
    if explicit:
        return explicit
    return f"oa{uuid.uuid4().hex[:12]}"


def _split_proxy_url(value: str):
    """(user, password, hostport) or None when the URL carries no credentials.

    urlsplit is used when possible (handles IPv6 brackets and passwords that
    contain ``@``); the regex is the fallback for shapes urlsplit rejects.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(value)
        if parts.username and parts.hostname:
            host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
            if parts.port:
                host = f"{host}:{parts.port}"
            return parts.username, parts.password or "", host
    except Exception:
        pass

    match = _PROXY_URL_RE.match(value)
    if not match or not match.group("user"):
        return None
    host = match.group("host") or ""
    if match.group("port"):
        host = f"{host}:{match.group('port')}"
    return match.group("user"), match.group("password") or "", host


def apply_sticky_session(proxy: Optional[str], session_id: Optional[str] = None) -> Optional[str]:
    """Pin a rotating proxy to one exit IP by suffixing the username.

    Rotating providers (Resin, and most residential pools) hand out a fresh
    exit per TCP connection. A registration spans 30+ requests across
    chatgpt.com and auth.openai.com; when the exit changes mid-flow Cloudflare
    serves a 403 challenge and the OAuth state goes 409 invalid_state.
    `username.<session_id>` keeps one IP for the whole run (measured: 8
    concurrent connections and 6 cross-host requests all returned the same IP).

    No-op when:
      · proxy is empty / not a URL
      · the username already carries a session suffix (contains ".")
      · the proxy has no credentials to suffix
      · REGISTER_PROXY_STICKY is explicitly disabled
    """
    value = normalize_proxy_url(proxy)
    if not value:
        return value

    raw_flag = os.getenv("REGISTER_PROXY_STICKY")
    if raw_flag is None:
        # Default on for socks5h://, which is what rotating SOCKS providers use.
        enabled = value.lower().startswith("socks5h://")
    else:
        enabled = _truthy(raw_flag)
    if not enabled:
        return value

    split = _split_proxy_url(value)
    if not split:
        return value
    user, password, host = split
    if "." in user:
        # Already sticky.
        return value

    sid = str(session_id or "").strip() or sticky_session_id()
    if not sid:
        return value

    scheme = value.split("://", 1)[0] if "://" in value else ""
    prefix = f"{scheme}://" if scheme else ""
    return f"{prefix}{user}.{sid}:{password}@{host}"


def rotate_sticky_session(proxy: Optional[str], session_id: Optional[str] = None) -> Optional[str]:
    """Same proxy, brand-new sticky session id → a different exit IP.

    Rotating pools hand out IPs of mixed reputation; measured on Resin, only
    a fraction of fresh exits clear Cloudflare's challenge on chatgpt.com.
    When the current exit is challenged, re-pinning to a new session id is
    cheaper than rebuilding the mailbox or waiting out the challenge.
    """
    value = normalize_proxy_url(proxy)
    if not value:
        return value
    split = _split_proxy_url(value)
    if not split:
        return value
    user, password, host = split
    # Drop any previous suffix, then pin to a fresh session id.
    base_user = user.split(".", 1)[0]
    scheme = value.split("://", 1)[0] if "://" in value else ""
    prefix = f"{scheme}://" if scheme else ""
    sid = str(session_id or "").strip() or sticky_session_id()
    return f"{prefix}{base_user}.{sid}:{password}@{host}"


def normalize_proxy_url(proxy: Optional[str]) -> Optional[str]:
    """Normalize proxy URLs for curl_cffi.

    `socks5://` uses local DNS; `socks5h://` sends DNS through the proxy, which
    avoids TLS handshake flakes when the local resolver and egress disagree.
    """
    if proxy is None:
        return None
    value = str(proxy).strip()
    if not value:
        return None
    lowered = value.lower()
    if lowered.startswith("socks5://") and not lowered.startswith("socks5h://"):
        return "socks5h://" + value[len("socks5://"):]
    return value


def resolve_proxy(explicit: Optional[str] = None, *, allow_default: bool = True) -> Optional[str]:
    if explicit is not None:
        value = str(explicit).strip()
        return apply_sticky_session(normalize_proxy_url(value or None))

    env_proxy = _first_env(
        "REGISTER_PROXY",
        "AAR_PROXY",
        "ALL_PROXY",
        "all_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
    )
    if env_proxy:
        return apply_sticky_session(normalize_proxy_url(env_proxy))

    if not allow_default:
        return None

    disabled = str(os.getenv("REGISTER_PROXY_DISABLE", "") or "").strip().lower() in {
        "1", "true", "yes", "on", "disable", "disabled",
    }
    if disabled:
        return None

    return apply_sticky_session(normalize_proxy_url(_first_env("REGISTER_PROXY_DEFAULT") or None))


def proxy_dict(proxy: Optional[str]) -> Optional[dict]:
    value = normalize_proxy_url(proxy)
    if not value:
        return None
    return {"http": value, "https": value}


def mask_proxy(proxy: Optional[str]) -> str:
    if not proxy:
        return ""
    text = str(proxy)
    if "@" not in text:
        return text
    scheme, rest = text.split("://", 1) if "://" in text else ("", text)
    creds, host = rest.rsplit("@", 1)
    if ":" in creds:
        user, _password = creds.split(":", 1)
        hidden = f"{user}:***"
    else:
        hidden = "***"
    return f"{scheme}://{hidden}@{host}" if scheme else f"{hidden}@{host}"
