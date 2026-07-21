"""
注册流程引擎
从 main.py 中提取并重构的注册流程
"""

import os
import re
import json
import time
import uuid
import base64
import random
import logging
import secrets
import string
from typing import Optional, Dict, Any, Tuple, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from curl_cffi import requests as cffi_requests

from .oauth import OAuthManager, OAuthStart, generate_oauth_url, submit_callback_url
from .http_client import OpenAIHTTPClient, HTTPClientError
# from ..services import EmailServiceFactory, BaseEmailService, EmailServiceType  # removed: external dep
# from ..database import crud  # removed: external dep
# from ..database.session import get_db  # removed: external dep
from .constants import (
    OPENAI_API_ENDPOINTS,
    OPENAI_PAGE_TYPES,
    generate_random_user_info,
    OTP_CODE_PATTERN,
    DEFAULT_PASSWORD_LENGTH,
    PASSWORD_CHARSET,
    AccountStatus,
    TaskStatus,
    SENTINEL_SDK_URL,
    OAUTH_REDIRECT_URI,
    OAUTH_CLIENT_ID,
)
# from ..config.settings import get_settings  # removed: external dep


logger = logging.getLogger(__name__)


@dataclass
class RegistrationResult:
    """注册结果"""
    success: bool
    email: str = ""
    password: str = ""  # 注册密码
    account_id: str = ""
    workspace_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""  # 会话令牌
    error_message: str = ""
    logs: list = None
    metadata: dict = None
    source: str = "register"  # 'register' 或 'login'，区分账号来源

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "email": self.email,
            "password": self.password,
            "account_id": self.account_id,
            "workspace_id": self.workspace_id,
            "access_token": self.access_token[:20] + "..." if self.access_token else "",
            "refresh_token": self.refresh_token[:20] + "..." if self.refresh_token else "",
            "id_token": self.id_token[:20] + "..." if self.id_token else "",
            "session_token": self.session_token[:20] + "..." if self.session_token else "",
            "error_message": self.error_message,
            "logs": self.logs or [],
            "metadata": self.metadata or {},
            "source": self.source,
        }


@dataclass
class SignupFormResult:
    """提交注册表单的结果"""
    success: bool
    page_type: str = ""  # 响应中的 page.type 字段
    is_existing_account: bool = False  # 是否为已注册账号
    response_data: Dict[str, Any] = None  # 完整的响应数据
    error_message: str = ""


@dataclass
class SentinelPayload:
    """Sentinel 请求结果。"""
    p: str
    c: str
    flow: str
    t: str = ""
    # openai-sentinel-so-token (create_account / oauth_create_account).
    # Empty when SDK/VM did not produce a dedicated so field.
    so: str = ""


# ─── Sentinel helpers (ported from browser_register.py) ──────────

def _generate_datadog_trace_headers() -> dict:
    trace_hex = secrets.token_hex(8).rjust(16, "0")
    parent_hex = secrets.token_hex(8).rjust(16, "0")
    trace_id = str(int(trace_hex, 16))
    parent_id = str(int(parent_hex, 16))
    return {
        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def _random_delay(low: float = 0.25, high: float = 0.9) -> None:
    """Human-ish inter-step jitter. Disable with OPENAI_REGISTER_NO_DELAY=1."""
    if str(os.environ.get("OPENAI_REGISTER_NO_DELAY") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        return
    try:
        lo = float(low)
        hi = float(high)
    except Exception:
        lo, hi = 0.25, 0.9
    if hi < lo:
        lo, hi = hi, lo
    time.sleep(random.uniform(lo, hi))


def _env_truthy(name: str, default: str = "0") -> bool:
    return str(os.environ.get(name, default) or default).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _so_collect_seconds(flow: str) -> float:
    """yukkcat aligns create_account Turnstile SO with official SDK 5000ms collect.

    OPENAI_SO_COLLECT_MS overrides (milliseconds). 0 disables.
    Default: 5000 for oauth_create_account / create_account, 0 otherwise.
    """
    raw = str(os.environ.get("OPENAI_SO_COLLECT_MS") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw) / 1000.0)
        except Exception:
            pass
    if flow in {"oauth_create_account", "create_account"}:
        return 5.0
    return 0.0


def _apply_oai_sc_cookie(session: Any, challenge_token: str) -> None:
    """Mirror yukkcat: oai-sc cookie = '0' + sentinel challenge token c."""
    token = str(challenge_token or "").strip()
    if not token or session is None:
        return
    value = token if token.startswith("0") else f"0{token}"
    for domain in (".auth.openai.com", "auth.openai.com", ".openai.com"):
        try:
            session.cookies.set("oai-sc", value, domain=domain)
        except Exception:
            try:
                session.cookies.set("oai-sc", value)
            except Exception:
                pass


def _create_account_so_max() -> int:
    """Max bytes for openai-sentinel-so-token (free proxies curl(55) on huge blobs)."""
    try:
        return max(256, int(os.environ.get("OPENAI_CREATE_ACCOUNT_SO_MAX", "4096") or 4096))
    except Exception:
        return 4096


def _compact_so_token(
    so_full: str,
    *,
    device_id: str,
    user_agent: str,
    profile: Optional[Dict[str, Any]] = None,
    max_so: Optional[int] = None,
) -> tuple[str, str]:
    """Return (so_value, source) with size guard for free proxies.

    Prefer full VM/server so when under max. When oversized, synthesize a compact
    requirements token (keeps create_account alive on weak egress).
    """
    so = str(so_full or "").strip()
    limit = max_so if max_so is not None else _create_account_so_max()
    if not so:
        return "", "empty"
    if len(so) <= limit:
        return so, "full"
    try:
        gen = _SentinelTokenGenerator(device_id, user_agent, profile=profile)
        compact = gen.generate_requirements_token()
        return compact, f"compacted:{len(so)}->{len(compact)}"
    except Exception:
        return so[:limit], f"truncated:{len(so)}->{limit}"


def _build_sentinel_header_bundle(
    payload: "SentinelPayload",
    device_id: str,
    *,
    include_so_header: bool = True,
    so_override: str = "",
    mirror_so_into_t: bool = False,
) -> tuple[str, str, dict]:
    """Build (openai-sentinel-token, openai-sentinel-so-token, sen_obj).

    yukkcat puts Turnstile VM result into both JSON `t` and the so-token header.
    Local keeps discrete `t`/`so` but can mirror so→t for create_account when
    OPENAI_SO_MIRROR_INTO_T=1 (default on for create_account callers).
    """
    so_val = str(so_override if so_override is not None and so_override != "" else (payload.so or "")).strip()
    t_val = str(payload.t or "").strip()
    if mirror_so_into_t and so_val and not t_val:
        t_val = so_val
    elif mirror_so_into_t and so_val and _env_truthy("OPENAI_SO_MIRROR_INTO_T", "1"):
        # Prefer so (VM) for t when both exist and so is the VM-derived token.
        if so_val == t_val or not t_val:
            t_val = so_val
        elif so_val.startswith("0") or len(so_val) >= len(t_val):
            # VM turnstile tokens are typically longer opaque strings.
            t_val = so_val
    sen_obj = {
        "p": payload.p,
        "t": t_val,
        "c": payload.c,
        "id": device_id,
        "flow": payload.flow,
    }
    if so_val:
        sen_obj["so"] = so_val
    token_header = json.dumps(sen_obj, separators=(",", ":"))
    so_header = so_val if (include_so_header and so_val) else ""
    return token_header, so_header, sen_obj


class _SentinelTokenGenerator:
    """Dynamic sentinel token generator – mirrors browser_register._SentinelTokenGenerator."""

    def __init__(self, device_id: str, user_agent: str, profile: Optional[Dict[str, Any]] = None):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent
        self.profile = profile or {}
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= (h >> 16)
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= (h >> 13)
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= (h >> 16)
        return f"{h & 0xFFFFFFFF:08x}"

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":")).encode("utf-8")).decode("ascii")

    def _config(self) -> list:
        perf_now = 1000 + random.random() * 49000
        p = self.profile or {}
        sw = int(p.get("screen_width") or 1920)
        sh = int(p.get("screen_height") or 1080)
        cores = int(p.get("hardware_concurrency") or random.choice([4, 8, 12, 16]))
        lang = str((p.get("languages") or ["en-US", "en"])[0] if p.get("languages") else "en-US")
        langs = ",".join(p.get("languages") or ["en-US", "en"])
        # Approximate heap from device memory when available.
        try:
            mem_gb = float(p.get("device_memory") or 8)
        except Exception:
            mem_gb = 8.0
        heap = int(min(4294705152, max(2147483648, mem_gb * 268435456)))
        return [
            f"{sw}x{sh}",
            time.strftime("%a, %d %b %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            heap,
            random.random(),
            self.user_agent,
            SENTINEL_SDK_URL,
            None,
            None,
            lang,
            langs,
            random.random(),
            "webkitTemporaryStorage\u2212undefined",
            "location",
            "Object",
            perf_now,
            self.sid,
            "",
            cores,
            int(time.time() * 1000 - perf_now),
        ]

    def generate_requirements_token(self) -> str:
        cfg = self._config()
        cfg[3] = 1
        cfg[9] = round(5 + random.random() * 45)
        return "gAAAAAC" + self._b64(cfg)

    def generate_token(self, seed: str, difficulty: str) -> str:
        max_attempts = 500000
        cfg = self._config()
        start_ms = int(time.time() * 1000)
        diff = str(difficulty or "0")
        for nonce in range(max_attempts):
            cfg[3] = nonce
            cfg[9] = round(int(time.time() * 1000) - start_ms)
            encoded = self._b64(cfg)
            digest = self._fnv1a32((seed or "") + encoded)
            if digest[: len(diff)] <= diff:
                return "gAAAAAB" + encoded + "~S"
        return "gAAAAAB" + self._b64(None)

    def decrypt_turnstile(self, dx_b64: str, p_token: str) -> str:
        """Compatibility wrapper used by Codex login paths.

        Older code called decrypt_turnstile; the real solver lives in sentinel_vm.
        """
        if not dx_b64:
            return ""
        from .sentinel_vm import solve_turnstile_dx
        from .constants import SENTINEL_SDK_URL
        return solve_turnstile_dx(
            dx_b64,
            p_token,
            user_agent=self.user_agent,
            sdk_url=SENTINEL_SDK_URL,
        )


class RegistrationEngine:
    """
    注册引擎
    负责协调邮箱服务、OAuth 流程和 OpenAI API 调用
    """

    def __init__(
        self,
        email_service: Any,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None
    ):
        """
        初始化注册引擎

        Args:
            email_service: 邮箱服务实例
            proxy_url: 代理 URL
            callback_logger: 日志回调函数
            task_uuid: 任务 UUID（用于数据库记录）
        """
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid

        # One account => one independent coherent browser profile.
        from .browser_profile import random_browser_profile, profile_summary
        self._browser_profile = random_browser_profile()
        self.http_client = OpenAIHTTPClient(
            proxy_url=proxy_url,
            profile=self._browser_profile,
        )
        # Keep profile object identity from client after chrome_major normalization.
        self._browser_profile = getattr(self.http_client, "browser", self._browser_profile) or self._browser_profile
        self._profile_summary = profile_summary(self._browser_profile)

        # 创建 OAuth 管理器
        from .constants import OAUTH_CLIENT_ID, OAUTH_AUTH_URL, OAUTH_TOKEN_URL, OAUTH_REDIRECT_URI, OAUTH_SCOPE
        self.oauth_manager = OAuthManager(
            client_id=OAUTH_CLIENT_ID,
            auth_url=OAUTH_AUTH_URL,
            token_url=OAUTH_TOKEN_URL,
            redirect_uri=OAUTH_REDIRECT_URI,
            scope=OAUTH_SCOPE,
            proxy_url=proxy_url  # 传递代理配置
        )

        # 状态变量
        self.email: Optional[str] = None
        self.password: Optional[str] = None  # 注册密码
        self.email_info: Optional[Dict[str, Any]] = None
        self.oauth_start: Optional[OAuthStart] = None
        self.session: Optional[cffi_requests.Session] = None
        self.session_token: Optional[str] = None  # 会话令牌
        self.logs: list = []
        self._otp_sent_at: Optional[float] = None  # OTP 发送时间戳
        self._is_existing_account: bool = False  # 是否为已注册账号（用于自动登录）
        self._is_passwordless_signup: bool = False
        self._force_password_path: bool = False
        self._email_verification_mode: str = ""
        self._signup_mode: str = ""
        # When send_otp returns login_challenge on a fresh catch-all, OpenAI often
        # never delivers mail. Track for short-circuit wait.
        self._otp_login_challenge: bool = False
        # authorize follow / login_or_signup may auto-trigger OTP before explicit send.
        self._otp_auto_sent: bool = False
        self._oauth_screen_hint: str = ""
        self._device_id: Optional[str] = None
        self._sentinel_token: Optional[str] = None
        self._signup_sentinel: Optional[SentinelPayload] = None
        self._password_sentinel: Optional[SentinelPayload] = None
        self._create_account_continue_url: Optional[str] = None
        self._otp_continue_url: Optional[str] = None
        self._otp_page_type: Optional[str] = None

    def _log(self, message: str, level: str = "info"):
        """记录日志"""
        timestamp = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}"

        # 添加到日志列表
        self.logs.append(log_message)

        # 调用回调函数
        if self.callback_logger:
            self.callback_logger(message)

        # 记录到数据库（如果有关联任务）
        if self.task_uuid:
            try:
                with get_db() as db:
                    crud.append_task_log(db, self.task_uuid, message)
            except Exception as e:
                logger.warning(f"记录任务日志失败: {e}")

        # 根据级别记录到日志系统
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    def _generate_password(self, length: int = DEFAULT_PASSWORD_LENGTH) -> str:
        """生成更稳的注册密码（大小写+数字+符号）。"""
        # 浏览器流 plugin 已验证：至少含小写/大写/数字/符号时通过率更稳。
        specials = ",._!@#"
        size = max(int(length or 12), 12)
        required = [
            secrets.choice("abcdefghijklmnopqrstuvwxyz"),
            secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
            secrets.choice("0123456789"),
            secrets.choice(specials),
        ]
        pool = PASSWORD_CHARSET + specials
        required.extend(secrets.choice(pool) for _ in range(size - len(required)))
        secrets.SystemRandom().shuffle(required)
        return "".join(required)

    def _load_create_account_password_page(self) -> bool:
        """预加载 create-account/password 页面，拿到页面阶段 cookie。"""
        try:
            response = self.session.get(
                "https://auth.openai.com/create-account/password",
                headers={
                    "referer": "https://chatgpt.com/",
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                timeout=20,
            )
            self._log(f"加载密码页状态: {response.status_code}")
            return response.status_code == 200
        except Exception as e:
            self._log(f"加载密码页失败: {e}", "warning")
            return False

    def _check_ip_location(self) -> Tuple[bool, Optional[str]]:
        """检查 IP 地理位置"""
        try:
            return self.http_client.check_ip_location()
        except Exception as e:
            self._log(f"检查 IP 地理位置失败: {e}", "error")
            return False, None

    def _create_email(self) -> bool:
        """创建邮箱"""
        try:
            self._log(f"正在创建 {self.email_service.service_type.value} 邮箱...")
            self.email_info = self.email_service.create_email()

            if not self.email_info or "email" not in self.email_info:
                self._log("创建邮箱失败: 返回信息不完整", "error")
                return False

            self.email = self.email_info["email"]
            self._log(f"成功创建邮箱: {self.email}")
            return True

        except Exception as e:
            self._log(f"创建邮箱失败: {e}", "error")
            return False

    @staticmethod
    def _resolve_screen_hint() -> str:
        """OAuth/authorize screen_hint. Default login_or_signup (gpt-free-register path)."""
        raw = str(os.environ.get("OPENAI_SCREEN_HINT") or "login_or_signup").strip().lower()
        allowed = {
            "login_or_signup",
            "signup",
            "login",
            "create_account",
        }
        return raw if raw in allowed else "login_or_signup"

    @staticmethod
    def _response_body_snip(resp: Any, limit: int = 180) -> str:
        try:
            text = getattr(resp, "text", None) or ""
        except Exception:
            text = ""
        text = str(text).replace("\n", " ").strip()
        if not text:
            return "<empty>"
        return text[:limit]

    def _clear_nextauth_cookies(self) -> None:
        """Drop chatgpt.com NextAuth/session crumbs so a retry can mint a fresh state."""
        if not getattr(self, "session", None):
            return
        names = (
            "__Host-next-auth.csrf-token",
            "__Secure-next-auth.callback-url",
            "__Secure-next-auth.session-token",
            "next-auth.csrf-token",
            "next-auth.callback-url",
            "next-auth.session-token",
            "oai-client-auth-session",
            "oai-sc",
        )
        jar = getattr(self.session, "cookies", None)
        if jar is None:
            return
        for name in names:
            try:
                jar.set(name, "", domain="chatgpt.com", path="/")
            except Exception:
                pass
            try:
                jar.set(name, "", domain=".chatgpt.com", path="/")
            except Exception:
                pass
            try:
                jar.set(name, "", domain="auth.openai.com", path="/")
            except Exception:
                pass
            try:
                # curl_cffi CookieJar may support delete-like set with expired
                if hasattr(jar, "delete"):
                    jar.delete(name)
            except Exception:
                pass

    def _parse_json_response(self, resp: Any, label: str) -> Optional[Dict[str, Any]]:
        """Parse JSON body; log status/content-type/body snip instead of bare JSONDecodeError."""
        status = getattr(resp, "status_code", "?")
        headers = getattr(resp, "headers", None) or {}
        ctype = ""
        try:
            ctype = str(headers.get("content-type") or headers.get("Content-Type") or "")
        except Exception:
            ctype = ""
        try:
            data = resp.json()
        except Exception as exc:
            self._log(
                f"{label} 非 JSON: status={status} content-type={ctype or '-'} "
                f"err={exc} body={self._response_body_snip(resp)}",
                "error",
            )
            return None
        if not isinstance(data, dict):
            self._log(
                f"{label} JSON 非对象: status={status} type={type(data).__name__} "
                f"body={self._response_body_snip(resp)}",
                "error",
            )
            return None
        return data

    def _start_oauth(self, *, attempts: int = 3) -> bool:
        """通过 chatgpt.com NextAuth 发起 OAuth 流程。

        Aligns with public protocol registrars / browser_register:
        signin carries prompt=login + screen_hint=login_or_signup + login_hint=email
        so authorize follow is more likely to land on email-verification and auto-send OTP.

        Local robustness (not risk-control bypass):
        - empty/HTML CSRF or signin responses get one-body diagnostics + retry
        - each retry clears stale NextAuth cookies and re-seeds chatgpt.com session
        """
        from .constants import CHATGPT_APP
        import urllib.parse

        attempts = max(1, int(attempts or 1))
        last_err = ""
        for attempt in range(1, attempts + 1):
            try:
                self._log(
                    f"通过 chatgpt.com NextAuth 发起 OAuth..."
                    + (f" (retry {attempt}/{attempts})" if attempt > 1 else "")
                )
                if attempt > 1:
                    self._clear_nextauth_cookies()
                    time.sleep(0.4 + random.random() * 0.8)

                # 1. 访问 chatgpt.com 获取基础 cookie
                # WARP/IN exits are often slow; keep timeouts generous for first hop.
                home = self.session.get(f"{CHATGPT_APP}/", timeout=30)
                home_status = getattr(home, "status_code", "?")
                if home_status not in (200, 301, 302, 303, 307, 308):
                    self._log(
                        f"chatgpt.com 首页异常: status={home_status} "
                        f"body={self._response_body_snip(home)}",
                        "warning",
                    )
                oai_did = self.session.cookies.get("oai-did", "") or ""
                self._log(f"chatgpt.com oai-did: {(oai_did[:20] + '...') if oai_did else '(empty)'}")

                # 2. 获取 CSRF token
                csrf_resp = self.session.get(
                    f"{CHATGPT_APP}/api/auth/csrf",
                    headers={
                        "accept": "application/json",
                        "referer": f"{CHATGPT_APP}/",
                    },
                    timeout=30,
                )
                csrf_data = self._parse_json_response(csrf_resp, "csrf")
                csrf_token = ""
                if csrf_data:
                    csrf_token = str(csrf_data.get("csrfToken") or "").strip()
                if not csrf_token:
                    # 从 cookie 中提取
                    csrf_cookie = self.session.cookies.get("__Host-next-auth.csrf-token", "") or ""
                    csrf_token = (
                        csrf_cookie.split("%7C")[0]
                        if "%7C" in csrf_cookie
                        else csrf_cookie.split("|")[0]
                    )
                    csrf_token = str(csrf_token or "").strip()
                if not csrf_token:
                    last_err = "csrf_token_empty"
                    self._log(
                        f"CSRF token 为空 status={getattr(csrf_resp, 'status_code', '?')} "
                        f"body={self._response_body_snip(csrf_resp)}",
                        "error",
                    )
                    continue
                self._log(f"CSRF token: {csrf_token[:20]}...")

                # 3. 调用 signin/openai 获取 authorize URL
                # Public registrars (gpt-free-register / browser_register) pass:
                #   prompt=login & screen_hint=login_or_signup & login_hint=<email> & ext-oai-did
                screen_hint = self._resolve_screen_hint()
                self._oauth_screen_hint = screen_hint
                q: Dict[str, str] = {"prompt": "login"}
                if oai_did:
                    q["ext-oai-did"] = oai_did
                q["screen_hint"] = screen_hint
                if self.email:
                    q["login_hint"] = str(self.email)
                q["auth_session_logging_id"] = str(uuid.uuid4())
                signin_url = f"{CHATGPT_APP}/api/auth/signin/openai?{urllib.parse.urlencode(q)}"
                self._log(
                    f"signin mode: screen_hint={screen_hint} login_hint={'yes' if self.email else 'no'}"
                )

                signin_resp = self.session.post(
                    signin_url,
                    headers={
                        "content-type": "application/x-www-form-urlencoded",
                        "origin": CHATGPT_APP,
                        "referer": f"{CHATGPT_APP}/",
                        "accept": "application/json",
                    },
                    data=f"callbackUrl={CHATGPT_APP}%2F&csrfToken={csrf_token}&json=true",
                    timeout=30,
                )
                self._log(f"signin/openai 状态: {signin_resp.status_code}")

                if signin_resp.status_code != 200:
                    last_err = f"signin_http_{signin_resp.status_code}"
                    self._log(
                        f"signin/openai 失败: {self._response_body_snip(signin_resp, 220)}",
                        "error",
                    )
                    continue

                signin_data = self._parse_json_response(signin_resp, "signin/openai")
                if not signin_data:
                    last_err = "signin_non_json"
                    continue

                auth_url = str(signin_data.get("url") or "").strip()
                if not auth_url:
                    last_err = "signin_no_url"
                    self._log(
                        f"signin/openai 未返回 authorize URL body={self._response_body_snip(signin_resp)}",
                        "error",
                    )
                    continue

                self._log(f"OAuth URL: {auth_url[:80]}...")

                # 存储为 OAuthStart (不需要 code_verifier，由 chatgpt.com 后端处理)
                self.oauth_start = OAuthStart(
                    auth_url=auth_url,
                    state="",  # state 由 NextAuth 管理
                    code_verifier="",  # 不需要
                    redirect_uri="",  # 不需要
                )
                # reset flags that belong to a previous oauth attempt
                self._otp_auto_sent = False
                self._otp_sent_at = None
                return True

            except Exception as e:
                last_err = str(e)
                self._log(f"NextAuth OAuth 流程失败: {e}", "error")
                continue

        self._log(f"NextAuth OAuth 放弃: last_err={last_err}", "error")
        return False

    def _init_session(self) -> bool:
        """初始化会话"""
        try:
            self.session = self.http_client.session
            # Ensure session-level headers stay coherent even if Session was created early.
            try:
                from .browser_profile import apply_profile_to_session
                apply_profile_to_session(self.session, getattr(self.http_client, "browser", None))
            except Exception:
                pass
            ua = getattr(self.http_client, "user_agent", "") or ""
            plat = (getattr(self.http_client, "browser", {}) or {}).get("platform")
            self._log(f"浏览器画像: {getattr(self, '_profile_summary', '')} ua={ua[:56]}...")
            return True
        except Exception as e:
            self._log(f"初始化会话失败: {e}", "error")
            return False

    def _get_device_id(self) -> Optional[str]:
        """获取 Device ID，并探测 authorize 跟随是否已落到 email-verification。"""
        try:
            if not self.oauth_start:
                return None

            response = self.session.get(
                self.oauth_start.auth_url,
                timeout=30,
                allow_redirects=True,
            )
            did = self.session.cookies.get("oai-did")
            self._log(f"Device ID: {did}")
            # login_or_signup path may auto-land on email-verification and trigger OTP.
            try:
                final_url = str(getattr(response, "url", "") or "")
                if final_url:
                    self._log(f"authorize final_url: {final_url[:120]}")
                low = final_url.lower()
                if any(x in low for x in (
                    "email-verification",
                    "email-otp",
                    "email_otp",
                    "/about-you",
                )):
                    self._otp_auto_sent = True
                    self._otp_sent_at = time.time()
                    self._log("authorize 跟随疑似已触发 OTP / 验证页（login_or_signup 路径）")
                # also sniff page type hints from HTML/json body when available
                body_snip = (getattr(response, "text", None) or "")[:800].lower()
                if "email_otp_verification" in body_snip or "email-verification" in body_snip:
                    self._otp_auto_sent = True
                    if not self._otp_sent_at:
                        self._otp_sent_at = time.time()
            except Exception:
                pass
            return did

        except Exception as e:
            self._log(f"获取 Device ID 失败: {e}", "error")
            return None

    def _check_sentinel(self, did: str, *, flow: str = "authorize_continue", attempts: int = 3) -> Optional[SentinelPayload]:
        """检查 Sentinel 拦截（动态生成 token + 处理 PoW），失败轻量重试。

        Fused with yukkcat/chatgpt2api behaviour:
        - create_account waits ~5s before Turnstile VM (official SDK collect)
        - Turnstile solve is preferred as openai-sentinel-so-token (not server so blob)
        - oai-sc cookie set to '0' + challenge token c
        """
        last_err = ""
        for attempt in range(1, max(1, attempts) + 1):
            try:
                ua = getattr(self.http_client, "user_agent", None) or self.http_client.default_headers.get("User-Agent", "")
                generator = _SentinelTokenGenerator(did, ua, profile=getattr(self, "_browser_profile", None))
                sent_p = generator.generate_requirements_token()
                sen_req_body = json.dumps({"p": sent_p, "id": did, "flow": flow}, separators=(",", ":"))

                from .constants import SENTINEL_FRAME_URL
                response = self.http_client.post(
                    OPENAI_API_ENDPOINTS["sentinel"],
                    headers={
                        "origin": "https://sentinel.openai.com",
                        "referer": SENTINEL_FRAME_URL,
                        "content-type": "text/plain;charset=UTF-8",
                    },
                    data=sen_req_body,
                )

                if response.status_code == 200:
                    data = response.json()
                    sen_token = str(data.get("token") or "")
                    turnstile = data.get("turnstile") or {}

                    # Handle proofofwork challenge if required
                    initial_p = sent_p  # keep for dx decryption
                    pow_meta = data.get("proofofwork") or {}
                    if pow_meta.get("required") and pow_meta.get("seed"):
                        sent_p = generator.generate_token(
                            str(pow_meta.get("seed") or ""),
                            str(pow_meta.get("difficulty") or "0"),
                        )
                        self._log(f"Sentinel PoW solved: flow={flow}")

                    # Solve turnstile dx with VM
                    t_value = ""
                    dx_b64 = str(turnstile.get("dx") or "")
                    turnstile_required = bool(turnstile.get("required") and dx_b64)
                    if dx_b64:
                        # yukkcat: sleep 5000ms before SO collect on create_account
                        collect_s = _so_collect_seconds(flow)
                        if collect_s > 0 and not _env_truthy("OPENAI_REGISTER_NO_DELAY", "0"):
                            self._log(
                                f"Sentinel SO collect wait: {collect_s:.1f}s flow={flow}"
                            )
                            time.sleep(collect_s)
                        try:
                            from .sentinel_vm import solve_turnstile_dx
                            from .constants import SENTINEL_SDK_URL
                            t_value = solve_turnstile_dx(
                                dx_b64, initial_p, user_agent=ua, sdk_url=SENTINEL_SDK_URL
                            )
                            self._log(f"Sentinel VM solved: t_len={len(t_value)} flow={flow}")
                        except Exception as vm_err:
                            self._log(f"Sentinel VM failed: {vm_err}", "warning")
                            # turnstile required but unsolved → retry whole sentinel
                            if turnstile_required and attempt < attempts:
                                time.sleep(0.4 * attempt)
                                continue

                    if not sen_token:
                        last_err = "empty token"
                        if attempt < attempts:
                            time.sleep(0.4 * attempt)
                            continue

                    # Server-side so blobs (often 30KB+) are not what the browser sends.
                    # yukkcat puts the Turnstile VM result into both t and so-token header.
                    server_so = str(
                        data.get("so")
                        or data.get("so_token")
                        or (data.get("token_so") if isinstance(data.get("token_so"), str) else "")
                        or ""
                    ).strip()
                    if not server_so:
                        for bag_key in ("proofofwork", "turnstile", "result", "payload"):
                            bag = data.get(bag_key) or {}
                            if isinstance(bag, dict):
                                cand = bag.get("so") or bag.get("so_token")
                                if cand:
                                    server_so = str(cand).strip()
                                    break

                    prefer_vm_so = _env_truthy("OPENAI_SO_PREFER_VM", "1")
                    so_value = ""
                    so_source = ""
                    if prefer_vm_so and t_value:
                        so_value = t_value
                        so_source = "vm_t"
                    elif server_so:
                        so_value = server_so
                        so_source = "server"
                    elif t_value:
                        so_value = t_value
                        so_source = "vm_t"
                    elif flow in {
                        "oauth_create_account",
                        "create_account",
                        "email_otp_validate",
                        "email_otp_send",
                    }:
                        # Compact requirements token when SDK so/VM absent
                        # (email_otp_validate benefits; bare p/t/c often login_failed).
                        try:
                            so_value = generator.generate_requirements_token()
                            so_source = "requirements"
                        except Exception:
                            if sen_token:
                                so_value = sen_token
                                so_source = "c"

                    payload = SentinelPayload(
                        p=sent_p,
                        c=sen_token,
                        flow=flow,
                        t=t_value,
                        so=so_value,
                    )
                    if payload.so:
                        self._log(
                            f"Sentinel so-token ready: flow={flow} so_len={len(payload.so)} "
                            f"src={so_source or '-'} server_so_len={len(server_so) if server_so else 0}"
                        )
                    # yukkcat: oai-sc = "0" + challenge token
                    try:
                        _apply_oai_sc_cookie(self.session, sen_token)
                    except Exception:
                        pass
                    self._log(f"Sentinel token 获取成功: flow={flow} attempt={attempt}")
                    return payload

                last_err = f"status={response.status_code}"
                self._log(
                    f"Sentinel 检查失败: flow={flow} {last_err} attempt={attempt}/{attempts}",
                    "warning",
                )
            except Exception as e:
                last_err = str(e)
                self._log(
                    f"Sentinel 检查异常: flow={flow} attempt={attempt}/{attempts} {e}",
                    "warning",
                )
            if attempt < attempts:
                time.sleep(0.5 * attempt + random.random() * 0.2)
        self._log(f"Sentinel 最终失败: flow={flow} {last_err}", "warning")
        return None

    def _attach_create_account_sentinel(
        self,
        headers: dict,
        ca_sentinel: SentinelPayload,
    ) -> None:
        """Attach dual sentinel headers for create_account (yukkcat-aligned).

        - openai-sentinel-token JSON with p/t/c/id/flow[/so]
        - openai-sentinel-so-token = VM Turnstile result (size-capped for free proxies)
        - oai-sc cookie already set inside _check_sentinel
        """
        if not ca_sentinel or not self._device_id:
            return
        ua = (
            getattr(self.http_client, "user_agent", None)
            or self.http_client.default_headers.get("User-Agent", "")
        )
        so_full = (ca_sentinel.so or ca_sentinel.t or "").strip()
        so_compact, so_src = _compact_so_token(
            so_full,
            device_id=self._device_id,
            user_agent=ua,
            profile=getattr(self, "_browser_profile", None),
        )
        if so_src.startswith("compacted") or so_src.startswith("truncated"):
            self._log(
                f"create_account so-token {so_src} (max={_create_account_so_max()})"
            )
        token_h, so_h, _sen = _build_sentinel_header_bundle(
            ca_sentinel,
            self._device_id,
            include_so_header=True,
            so_override=so_compact,
            mirror_so_into_t=True,
        )
        headers["openai-sentinel-token"] = token_h
        if so_h:
            headers["openai-sentinel-so-token"] = so_h
            self._log(
                f"create_account so-token attached: len={len(so_h)} src={so_src}"
            )
        else:
            # Fallback: some public paths mirror the full JSON as so-token.
            headers["openai-sentinel-so-token"] = token_h
            self._log("create_account so-token mirrored from sentinel-token JSON")
        try:
            _apply_oai_sc_cookie(self.session, ca_sentinel.c)
        except Exception:
            pass
        self._log(f"create_account Sentinel 已获取: flow={ca_sentinel.flow}")

    @staticmethod
    def _is_invalid_state_response(status_code: Any, body: str = "") -> bool:
        """True for authorize/continue 409 invalid_state (stale NextAuth session)."""
        try:
            code = int(status_code or 0)
        except Exception:
            code = 0
        text = str(body or "").lower()
        if code == 409 and "invalid_state" in text:
            return True
        if "invalid_state" in text and ("sign-in session is no longer valid" in text or code in {400, 401, 403, 409}):
            return True
        return False

    def _rebuild_oauth_session(self) -> Tuple[bool, Optional[str], Optional[SentinelPayload], str]:
        """Rebuild NextAuth → authorize → device id → sentinel after invalid_state.

        Local robustness only: clear stale cookies, re-mint OAuth, re-follow authorize.
        Does not change fingerprints, proxies, or risk-control behavior.
        Returns (ok, did, sen_payload, error_message).
        """
        self._log("OAuth 会话失效，重建 NextAuth/authorize...", "warning")
        self._clear_nextauth_cookies()
        self.oauth_start = None
        self._otp_auto_sent = False
        self._otp_sent_at = None
        self._device_id = None
        self._signup_sentinel = None
        self._sentinel_token = None
        self._is_passwordless_signup = False
        self._force_password_path = False
        self._is_existing_account = False
        self._email_verification_mode = ""
        self._signup_mode = ""
        time.sleep(0.5 + random.random() * 0.7)
        if not self._start_oauth(attempts=3):
            return False, None, None, "重建 OAuth 失败"
        did = self._get_device_id()
        if not did:
            return False, None, None, "重建后获取 Device ID 失败"
        sen_payload = self._check_sentinel(did)
        if sen_payload:
            self._log("重建后 Sentinel 检查通过")
        else:
            self._log("重建后 Sentinel 检查失败或未启用", "warning")
        return True, did, sen_payload, ""

    def _submit_signup_form(
        self,
        did: str,
        sen_payload: Optional[SentinelPayload],
        *,
        allow_oauth_rebuild: bool = True,
    ) -> SignupFormResult:
        """
        提交注册表单（通过 authorize/continue 建立 session）

        Returns:
            SignupFormResult: 提交结果，包含账号状态判断

        Local robustness: on authorize/continue 409 invalid_state, optionally rebuild
        OAuth once (clear cookies → _start_oauth → re-follow authorize → resubmit).
        """
        try:
            self._device_id = did
            self._signup_sentinel = sen_payload
            self._sentinel_token = sen_payload.c if sen_payload else None

            # If authorize already landed on email-verification and auto-sent OTP,
            # posting authorize/continue again starts a *new* passwordless session and
            # invalidates the code that just arrived. Stay on the current session.
            #
            # yukkcat treats /email-verification landing as passwordless and never
            # invents a password here. Forcing password create on an auto-OTP session
            # yields account_creation_failed, then invalid_auth_step on OTP validate.
            # Only force password when OPENAI_FORCE_PASSWORD_ON_AUTO_OTP=1.
            if getattr(self, "_otp_auto_sent", False) and str(
                os.environ.get("OPENAI_SKIP_CONTINUE_ON_AUTO_OTP", "1")
            ).strip().lower() in {"1", "true", "yes", "on"}:
                self._log(
                    "authorize 已到 email-verification，跳过 authorize/continue，"
                    "直接使用 auto-OTP 会话"
                )
                force_password_on_auto = str(
                    os.environ.get("OPENAI_FORCE_PASSWORD_ON_AUTO_OTP", "0")
                ).strip().lower() in {"1", "true", "yes", "on"}
                self._email_verification_mode = "passwordless_signup"
                self._signup_mode = "email_signup"
                self._is_passwordless_signup = not force_password_on_auto
                self._force_password_path = force_password_on_auto
                self._is_existing_account = False
                if self._is_passwordless_signup:
                    self._log(
                        "passwordless 注册 OTP 流程: mode=passwordless_signup "
                        "(auto-otp skip continue; yukkcat-aligned)"
                    )
                else:
                    self._log(
                        "auto-OTP 存在但仍按 OPENAI_FORCE_PASSWORD_ON_AUTO_OTP 走密码路径"
                    )
                try:
                    names = sorted({c.name for c in self.session.cookies})
                    auth_sess = bool(self.session.cookies.get("oai-client-auth-session"))
                    self._log(f"skip-continue cookies={len(names)} auth_session={auth_sess}")
                except Exception:
                    pass
                return SignupFormResult(
                    success=True,
                    page_type="email_otp_verification",
                    is_existing_account=False,
                    response_data={
                        "page": {"type": "email_otp_verification"},
                        "skipped_continue": True,
                    },
                )

            # authorize/continue body: login_or_signup is a NextAuth signin hint.
            # Body still uses signup for new accounts unless forced to login.
            screen_hint = self._resolve_screen_hint()
            body_hint = "login" if screen_hint == "login" else "signup"
            signup_body = json.dumps(
                {
                    "username": {"value": self.email, "kind": "email"},
                    "screen_hint": body_hint,
                },
                separators=(",", ":"),
            )
            self._log(f"authorize/continue body screen_hint={body_hint} (oauth={screen_hint})")

            headers = {
                "referer": "https://auth.openai.com/create-account",
                "accept": "application/json",
                "content-type": "application/json",
            }

            if sen_payload:
                token_h, so_h, _ = _build_sentinel_header_bundle(
                    sen_payload,
                    did,
                    include_so_header=bool(getattr(sen_payload, "so", None)),
                    mirror_so_into_t=False,
                )
                headers["openai-sentinel-token"] = token_h
                if so_h:
                    headers["openai-sentinel-so-token"] = so_h
                try:
                    _apply_oai_sc_cookie(self.session, sen_payload.c)
                except Exception:
                    pass

            response = self.session.post(
                OPENAI_API_ENDPOINTS["signup"],
                headers=headers,
                data=signup_body,
            )

            status = getattr(response, "status_code", "?")
            body_snip = self._response_body_snip(response, 220)
            self._log(f"提交注册表单状态: {status}")

            if status != 200:
                err_msg = f"HTTP {status}: {body_snip}"
                if allow_oauth_rebuild and self._is_invalid_state_response(status, body_snip):
                    self._log(
                        f"authorize/continue invalid_state，尝试重建 OAuth 后重提: {body_snip}",
                        "warning",
                    )
                    ok, new_did, new_sen, rebuild_err = self._rebuild_oauth_session()
                    if not ok:
                        return SignupFormResult(
                            success=False,
                            error_message=f"{err_msg}; rebuild_failed={rebuild_err}",
                        )
                    # One rebuild only — prevent infinite loop on persistent 409.
                    return self._submit_signup_form(
                        new_did or did,
                        new_sen,
                        allow_oauth_rebuild=False,
                    )
                return SignupFormResult(success=False, error_message=err_msg)

            try:
                response_data = self._parse_json_response(response, "authorize/continue")
                if not response_data:
                    return SignupFormResult(
                        success=False,
                        error_message=f"authorize/continue 非 JSON: {body_snip}",
                    )
                page_type = response_data.get("page", {}).get("type", "")
                self._log(f"响应页面类型: {page_type}")

                page_payload = (response_data.get("page") or {}).get("payload") or {}
                sess = response_data.get("oai-client-auth-session") or {}
                verification_mode = str(
                    page_payload.get("email_verification_mode")
                    or sess.get("email_verification_mode")
                    or ""
                ).strip().lower()
                signup_mode = str(sess.get("signup_mode") or "").strip().lower()
                screen_hint = str(
                    sess.get("original_screen_hint")
                    or page_payload.get("screen_hint")
                    or ""
                ).strip().lower()
                self._email_verification_mode = verification_mode
                self._signup_mode = signup_mode
                self._log(
                    f"signup session: verification_mode={verification_mode or '-'} "
                    f"signup_mode={signup_mode or '-'} screen_hint={screen_hint or '-'} "
                    f"page={page_type or '-'}"
                )
                # Prefer explicit verification_mode. login_challenge means OpenAI wants a
                # login OTP (often silent / no delivery for brand-new catch-all addresses);
                # do NOT treat that as passwordless signup solely because signup_mode=email_signup.
                if verification_mode == "passwordless_signup":
                    # Observed on WARP/IN: passwordless_signup → email-otp/send flips to
                    # login_challenge and never delivers. Prefer password create path first.
                    prefer_password = str(
                        os.environ.get("OPENAI_PREFER_PASSWORD_SIGNUP", "1")
                    ).strip().lower() in {"1", "true", "yes", "on"}
                    is_passwordless_signup = not prefer_password
                elif verification_mode in {"login_challenge", "login", "login_otp"}:
                    is_passwordless_signup = False
                else:
                    # Empty / unknown: email_signup + otp page still means passwordless signup.
                    is_passwordless_signup = (
                        page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]
                        and signup_mode in {"email_signup", "passwordless_signup"}
                    )
                is_existing = (
                    page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]
                    and not is_passwordless_signup
                    and verification_mode in {"login_challenge", "login", "login_otp"}
                    and screen_hint not in {"signup", ""}
                )
                # login_challenge after screen_hint=signup on a fresh address is ambiguous:
                # treat as "needs password create path" rather than existing-account login.
                force_password_path = (
                    (
                        verification_mode in {"login_challenge", "login", "login_otp"}
                        and screen_hint in {"signup", ""}
                    )
                    or (
                        verification_mode == "passwordless_signup"
                        and str(os.environ.get("OPENAI_PREFER_PASSWORD_SIGNUP", "1")).strip().lower()
                        in {"1", "true", "yes", "on"}
                    )
                )
                self._is_passwordless_signup = is_passwordless_signup and not force_password_path
                self._force_password_path = force_password_path
                if self._is_passwordless_signup:
                    self._log(
                        f"passwordless 注册 OTP 流程: mode={verification_mode or signup_mode or page_type}"
                    )
                    self._is_existing_account = False
                    continue_url = str(response_data.get("continue_url") or "").strip()
                    # If authorize already auto-triggered OTP, keep that mark and avoid
                    # extra HTML navigation / email-otp/send (both observed to flip the
                    # passwordless session into login_challenge → invalid_state).
                    auto = bool(getattr(self, "_otp_auto_sent", False))
                    if auto:
                        self._log(
                            "保留 authorize auto-OTP，跳过 continue_url HTML 跟随与显式 send"
                        )
                    elif continue_url.startswith("https://auth.openai.com/"):
                        # Only follow same-origin auth page hops when we still need send.
                        try:
                            cont = self.session.get(continue_url, timeout=15)
                            self._log(f"跟随 signup continue_url: {cont.status_code}")
                        except Exception as e:
                            self._log(f"跟随 signup continue_url 失败: {e}", "warning")
                    elif continue_url:
                        self._log(f"跳过非 auth continue_url: {continue_url[:80]}", "warning")
                    # cookie presence is critical for OTP validate
                    try:
                        names = sorted({c.name for c in self.session.cookies})
                        auth_sess = bool(self.session.cookies.get("oai-client-auth-session"))
                        self._log(f"signup 后 cookies={len(names)} auth_session={auth_sess}")
                    except Exception:
                        pass
                elif force_password_path:
                    self._log(
                        f"改走密码注册路径 (mode={verification_mode or '-'} "
                        f"signup_mode={signup_mode or '-'} screen_hint={screen_hint or '-'})"
                    )
                    self._is_existing_account = False
                elif is_existing:
                    self._log(f"检测到已注册账号，将自动切换到登录流程")
                    self._is_existing_account = True

                return SignupFormResult(
                    success=True,
                    page_type=page_type,
                    is_existing_account=bool(getattr(self, "_is_existing_account", False)),
                    response_data=response_data
                )

            except Exception as parse_error:
                self._log(f"解析响应失败: {parse_error}", "warning")
                return SignupFormResult(success=True)

        except Exception as e:
            self._log(f"提交注册表单失败: {e}", "error")
            return SignupFormResult(success=False, error_message=str(e))

    def _register_password(self) -> Tuple[bool, Optional[str]]:
        """注册密码"""
        try:
            ua = (
                getattr(self.http_client, "user_agent", None)
                or self.http_client.default_headers.get("User-Agent", "")
            )
            profile = getattr(self, "_browser_profile", None) or getattr(self.http_client, "browser", {}) or {}
            chrome_major = str(profile.get("chrome_major") or "")
            if not chrome_major:
                chrome_match = re.search(r"Chrome/(\d+)", ua)
                chrome_major = str(chrome_match.group(1) if chrome_match else "142")
            sec_ch_ua = str(profile.get("sec_ch_ua") or (
                f'"Chromium";v="{chrome_major}", "Google Chrome";v="{chrome_major}", "Not_A Brand";v="99"'
            ))
            sec_ch_platform = str(profile.get("sec_ch_ua_platform") or '"macOS"')

            candidates = []
            while len(candidates) < 3:
                pwd = self._generate_password()
                if pwd not in candidates:
                    candidates.append(pwd)

            for index, password in enumerate(candidates, start=1):
                self.password = password

                # Reload page + refresh sentinel for each attempt (tokens are single-use)
                self._load_create_account_password_page()
                if self._device_id:
                    self._password_sentinel = self._check_sentinel(self._device_id, flow="username_password_create")
                    if self._password_sentinel:
                        self._log(
                            f"密码阶段 Sentinel 已刷新: flow={self._password_sentinel.flow} "
                            f"turnstile={'yes' if self._password_sentinel.t else 'no'}"
                        )

                self._log(f"生成密码[{index}/{len(candidates)}]: {password}")

                register_body = json.dumps({
                    "password": password,
                    "username": self.email
                })

                register_headers = {
                    "origin": "https://auth.openai.com",
                    "referer": "https://auth.openai.com/create-account/password",
                    "accept": "application/json",
                    "content-type": "application/json",
                    "accept-language": "en-US,en;q=0.9",
                    "sec-ch-ua": sec_ch_ua,
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": sec_ch_platform,
                    "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-site": "same-origin",
                    **_generate_datadog_trace_headers(),
                }
                if self._device_id:
                    register_headers["oai-device-id"] = self._device_id
                if self._password_sentinel and self._device_id:
                    register_headers["openai-sentinel-token"] = json.dumps({
                        "p": self._password_sentinel.p,
                        "t": self._password_sentinel.t,
                        "c": self._password_sentinel.c,
                        "id": self._device_id,
                        "flow": self._password_sentinel.flow,
                    }, separators=(",", ":"))

                response = self.session.post(
                    OPENAI_API_ENDPOINTS["register"],
                    headers=register_headers,
                    data=register_body,
                )

                self._log(f"提交密码状态[{index}/{len(candidates)}]: {response.status_code}")

                if response.status_code == 200:
                    # 解析响应，检测已注册账号
                    try:
                        resp_data = response.json()
                        page_type = resp_data.get("page", {}).get("type", "")
                        self._log(f"注册响应页面类型: {page_type}")
                        if page_type == OPENAI_PAGE_TYPES.get("EMAIL_OTP_VERIFICATION", "email_otp_verification"):
                            self._log("检测到已注册账号，自动切换到登录流程")
                            self._is_existing_account = True
                    except Exception:
                        pass
                    return True, password

                error_text = response.text[:500]
                self._log(f"密码注册失败[{index}/{len(candidates)}]: {error_text}", "warning")

                try:
                    error_json = response.json()
                    error_msg = error_json.get("error", {}).get("message", "")
                    error_code = error_json.get("error", {}).get("code", "")

                    if "already" in error_msg.lower() or "exists" in error_msg.lower() or error_code == "user_exists":
                        self._log(f"邮箱 {self.email} 可能已在 OpenAI 注册过", "error")
                        self._mark_email_as_registered()
                        return False, None
                except Exception:
                    pass

            return False, None

        except Exception as e:
            self._log(f"密码注册失败: {e}", "error")
            return False, None

    def _mark_email_as_registered(self):
        """标记邮箱为已注册状态（用于防止重复尝试）"""
        try:
            with get_db() as db:
                # 检查是否已存在该邮箱的记录
                existing = crud.get_account_by_email(db, self.email)
                if not existing:
                    # 创建一个失败记录，标记该邮箱已注册过
                    crud.create_account(
                        db,
                        email=self.email,
                        password="",  # 空密码表示未成功注册
                        email_service=self.email_service.service_type.value,
                        email_service_id=self.email_info.get("service_id") if self.email_info else None,
                        status="failed",
                        extra_data={"register_failed_reason": "email_already_registered_on_openai"}
                    )
                    self._log(f"已在数据库中标记邮箱 {self.email} 为已注册状态")
        except Exception as e:
            logger.warning(f"标记邮箱状态失败: {e}")

    def _send_verification_code(self) -> bool:
        """发送验证码。

        passwordless_signup 场景下，authorize/continue 后页面虽是 email_otp_verification，
        但邮件常需显式 POST /api/accounts/email-otp/send 才会投递（已用 zc.233159.xyz 探针验证）。
        """
        headers = {
            "origin": "https://auth.openai.com",
            "referer": "https://auth.openai.com/email-verification",
            "accept": "application/json",
            "content-type": "application/json",
        }
        # Light session touch only. Full HTML navigation of email-verification
        # after passwordless authorize/continue has been observed to flip the
        # server state to login_challenge and later invalid_state on validate.
        try:
            dump_resp = self.session.get(
                "https://auth.openai.com/api/accounts/client_auth_session_dump",
                headers={
                    "accept": "application/json",
                    "referer": "https://auth.openai.com/email-verification",
                },
                timeout=10,
            )
            self._log(f"预热 auth session dump: {dump_resp.status_code}")
            try:
                dump = dump_resp.json() if dump_resp.status_code == 200 else {}
                mode = str(
                    ((dump.get("page") or {}).get("payload") or {}).get("email_verification_mode")
                    or (dump.get("oai-client-auth-session") or {}).get("email_verification_mode")
                    or dump.get("email_verification_mode")
                    or ""
                ).strip().lower()
                if mode:
                    self._log(f"send 前 session verification_mode={mode}")
            except Exception:
                pass
        except Exception as e:
            self._log(f"预热 auth session dump 失败: {e}", "warning")
        last_err = ""
        for attempt in range(1, 4):
            try:
                # stamp send time at the successful attempt; on retries refresh it
                self._otp_sent_at = time.time()
                # Prefer POST (works for passwordless). Fall back to GET for older flows.
                response = self.session.post(
                    OPENAI_API_ENDPOINTS["send_otp"],
                    headers=headers,
                    data="{}",
                )
                self._log(f"验证码发送状态(POST): {response.status_code} attempt={attempt}")
                if response.status_code == 200:
                    body = (response.text or "")[:180].replace("\n", " ")
                    if body:
                        self._log(f"验证码发送响应: {body}")
                    try:
                        data = response.json()
                        page_payload = ((data.get("page") or {}).get("payload") or {})
                        sess = data.get("oai-client-auth-session") or {}
                        mode = str(
                            page_payload.get("email_verification_mode")
                            or sess.get("email_verification_mode")
                            or ""
                        ).strip().lower()
                        if mode:
                            self._email_verification_mode = mode
                            self._log(f"send_otp verification_mode={mode}")
                            if mode in {"login_challenge", "login", "login_otp"}:
                                self._otp_login_challenge = True
                            elif mode in {"passwordless_signup", "signup", "email_signup"}:
                                # real signup OTP — clear sticky challenge flag
                                self._otp_login_challenge = False
                    except Exception:
                        pass
                    # ensure page cookies still present
                    if not self.session.cookies.get("oai-client-auth-session"):
                        self._log("警告: send_otp 后缺少 oai-client-auth-session", "warning")
                    return True

                # 429 / 5xx retry
                if response.status_code in {408, 425, 429} or response.status_code >= 500:
                    last_err = f"POST {response.status_code}"
                    time.sleep(0.8 * attempt + random.random() * 0.3)
                    continue

                response = self.session.get(
                    OPENAI_API_ENDPOINTS["send_otp"],
                    headers={
                        "referer": "https://auth.openai.com/email-verification",
                        "accept": "application/json",
                    },
                )
                self._log(f"验证码发送状态(GET fallback): {response.status_code} attempt={attempt}")
                if response.status_code == 200:
                    self._otp_sent_at = time.time()
                    return True
                last_err = f"GET {response.status_code} body={response.text[:180]}"
                self._log(f"发送验证码响应: {response.text[:300]}", "warning")
                if response.status_code in {408, 425, 429} or response.status_code >= 500:
                    time.sleep(0.8 * attempt + random.random() * 0.3)
                    continue
                # non-retriable client error
                break
            except Exception as e:
                last_err = str(e)
                self._log(f"发送验证码失败 attempt={attempt}: {e}", "error")
                time.sleep(0.6 * attempt)
        self._log(f"发送验证码最终失败: {last_err}", "error")
        return False

    def _keep_auth_session_alive(self) -> None:
        """Light touch on auth.openai.com to reduce invalid_state during OTP wait."""
        try:
            # Prefer API dump over full HTML navigation (less chance of state reset).
            self.session.get(
                "https://auth.openai.com/api/accounts/client_auth_session_dump",
                headers={
                    "accept": "application/json",
                    "referer": "https://auth.openai.com/email-verification",
                },
                timeout=10,
            )
        except Exception as e:
            self._log(f"会话保活失败: {e}", "warning")

    def _login_challenge_fast_fail_enabled(self) -> bool:
        """Whether to short-circuit OTP wait on login_challenge (default on)."""
        return str(os.environ.get("OPENAI_OTP_LOGIN_CHALLENGE_FAST_FAIL", "1")).strip().lower() in {
            "1", "true", "yes", "on",
        }

    def _login_challenge_probe_secs(self) -> float:
        """How long to wait for mail before declaring login_challenge undeliverable."""
        try:
            return max(5.0, float(os.environ.get("OPENAI_OTP_LOGIN_CHALLENGE_PROBE_SECS", "35") or 35))
        except Exception:
            return 35.0

    def _get_verification_code(self) -> Optional[str]:
        """获取验证码（分段轮询 + 会话保活 + 中途重发，降低 invalid_state / 投递丢信）。

        Fast-fail: if send_otp parked the session on login_challenge and no mail
        arrives within a short probe window, abort instead of waiting full 300s.
        Public registrars treat this as "switch email/proxy strategy", not a hang.
        """
        try:
            self._log(f"正在等待邮箱 {self.email} 的验证码...")
            email_id = self.email_info.get("service_id") if self.email_info else None
            total_timeout = 300
            slice_secs = 20
            resend_every = 45
            max_resends = 4
            # login_challenge rarely delivers to catch-all; use short budget.
            login_challenge = bool(getattr(self, "_otp_login_challenge", False))
            if login_challenge and self._login_challenge_fast_fail_enabled():
                total_timeout = int(self._login_challenge_probe_secs())
                slice_secs = min(12, total_timeout)
                resend_every = max(12, total_timeout // 2)
                max_resends = 1
                self._log(
                    f"login_challenge 快速失败模式: probe={total_timeout}s "
                    f"(set OPENAI_OTP_LOGIN_CHALLENGE_FAST_FAIL=0 to disable)",
                    "warning",
                )
            deadline = time.time() + total_timeout
            last_err = ""
            resends = 0
            next_resend_at = time.time() + resend_every
            while time.time() < deadline:
                remaining = max(1, int(deadline - time.time()))
                slice_timeout = min(slice_secs, remaining)
                try:
                    code = self.email_service.get_verification_code(
                        email=self.email,
                        email_id=email_id,
                        timeout=slice_timeout,
                        pattern=OTP_CODE_PATTERN,
                        otp_sent_at=self._otp_sent_at,
                    )
                except Exception as e:
                    last_err = str(e)
                    code = None
                    # TimeoutError from mailbox is expected for a slice
                    if "超时" not in last_err and "timeout" not in last_err.lower():
                        self._log(f"收信切片异常: {e}", "warning")
                if code:
                    self._log(f"成功获取验证码: {code}")
                    # refresh page right before validate
                    self._keep_auth_session_alive()
                    return code
                self._keep_auth_session_alive()
                # OpenAI sometimes returns send_otp 200 without actually delivering.
                # Resend mid-wait while keeping the same auth session.
                if resends < max_resends and time.time() >= next_resend_at and time.time() < deadline - 8:
                    resends += 1
                    self._log(f"验证码未到，重发 OTP attempt={resends}/{max_resends}")
                    if self._send_verification_code():
                        # If resend flipped into/kept login_challenge, keep short budget.
                        if getattr(self, "_otp_login_challenge", False) and self._login_challenge_fast_fail_enabled():
                            # do not extend beyond original short deadline
                            next_resend_at = deadline  # no further resends
                        else:
                            next_resend_at = time.time() + resend_every
                    else:
                        next_resend_at = time.time() + max(10, resend_every // 2)
            if login_challenge and self._login_challenge_fast_fail_enabled():
                self._log(
                    "login_challenge 快速失败: 短窗口内无 OTP 投递，建议换邮箱域/出口后重试",
                    "error",
                )
                return None
            self._log(f"等待验证码超时{(': ' + last_err) if last_err else ''}", "error")
            return None

        except Exception as e:
            self._log(f"获取验证码失败: {e}", "error")
            return None

    def _validate_verification_code(self, code: str) -> bool:
        """验证验证码（网络抖动时短重试一次）。

        Browser path attaches openai-sentinel-token with flow=email_otp_validate.
        Protocol path historically omitted it; OpenAI now often returns login_failed
        401 without a fresh sentinel on validate.
        """
        code = str(code or "").strip()
        if not code:
            return False
        headers = {
            "referer": "https://auth.openai.com/email-verification",
            "accept": "application/json",
            "content-type": "application/json",
            "origin": "https://auth.openai.com",
            "sec-fetch-site": "same-origin",
        }
        if self._device_id:
            headers["oai-device-id"] = self._device_id
        code_body = json.dumps({"code": code}, separators=(",", ":"))
        for attempt in range(1, 3):
            try:
                # re-touch page before each attempt
                self._keep_auth_session_alive()
                # Fresh sentinel per attempt (tokens are single-use).
                if self._device_id:
                    try:
                        otp_sen = self._check_sentinel(
                            self._device_id, flow="email_otp_validate", attempts=2
                        )
                        if otp_sen:
                            token_h, so_h, _ = _build_sentinel_header_bundle(
                                otp_sen,
                                self._device_id,
                                include_so_header=True,
                                mirror_so_into_t=False,
                            )
                            headers["openai-sentinel-token"] = token_h
                            if so_h:
                                headers["openai-sentinel-so-token"] = so_h
                            else:
                                headers.pop("openai-sentinel-so-token", None)
                            try:
                                _apply_oai_sc_cookie(self.session, otp_sen.c)
                            except Exception:
                                pass
                            self._log(
                                f"OTP validate sentinel ready: attempt={attempt} "
                                f"t_len={len(otp_sen.t or '')} so={'yes' if so_h else 'no'}"
                            )
                        else:
                            headers.pop("openai-sentinel-token", None)
                            headers.pop("openai-sentinel-so-token", None)
                            self._log("OTP validate sentinel missing", "warning")
                    except Exception as se:
                        headers.pop("openai-sentinel-token", None)
                        headers.pop("openai-sentinel-so-token", None)
                        self._log(f"OTP validate sentinel error: {se}", "warning")
                response = self.session.post(
                    OPENAI_API_ENDPOINTS["validate_otp"],
                    headers=headers,
                    data=code_body,
                )
                self._log(f"验证码校验状态: {response.status_code} attempt={attempt}")
                if response.status_code == 200:
                    try:
                        resp_data = response.json()
                        self._otp_continue_url = resp_data.get("continue_url", "")
                        self._otp_page_type = resp_data.get("page", {}).get("type", "")
                        self._log(f"验证码校验 -> page_type={self._otp_page_type}")
                    except Exception:
                        self._otp_continue_url = ""
                        self._otp_page_type = ""
                    return True

                body = response.text[:300]
                self._log(f"验证码校验响应: {body}", "warning")
                # invalid_state = session lost; retrying same code rarely helps
                if "invalid_state" in body or response.status_code == 409:
                    self._log("会话 invalid_state，停止重试同一验证码", "error")
                    return False
                # wrong code / expired usually 400 — do not spin forever
                if response.status_code in {408, 425, 429} or response.status_code >= 500:
                    time.sleep(0.6 * attempt)
                    continue
                return False
            except Exception as e:
                self._log(f"验证验证码失败 attempt={attempt}: {e}", "error")
                time.sleep(0.5 * attempt)
        return False

    def _create_user_account(self) -> bool:
        """创建用户账户"""
        try:
            user_info = generate_random_user_info()
            self._log(f"生成用户信息: {user_info['name']}, 生日: {user_info['birthdate']}")
            create_account_body = json.dumps(user_info)

            # 调 client_auth_session_dump 推进服务器 auth 状态机
            try:
                dump_resp = self.session.get(
                    "https://auth.openai.com/api/accounts/client_auth_session_dump",
                    headers={
                        "referer": "https://auth.openai.com/email-verification",
                        "accept": "application/json",
                    },
                    timeout=20,
                )
                self._log(f"client_auth_session_dump 状态: {dump_resp.status_code}")
            except Exception as e:
                self._log(f"client_auth_session_dump 异常: {e}", "warning")

            create_headers = {
                "referer": "https://auth.openai.com/about-you",
                "accept": "application/json",
                "content-type": "application/json",
                "origin": "https://auth.openai.com",
                "sec-fetch-site": "same-origin",
                **_generate_datadog_trace_headers(),
            }
            if self._device_id:
                create_headers["oai-device-id"] = self._device_id

            # create_account 也需要 sentinel token (flow=oauth_create_account)
            # yukkcat dual headers: openai-sentinel-token + openai-sentinel-so-token
            # Free proxies: compact oversized so (OPENAI_CREATE_ACCOUNT_SO_MAX, default 4096).
            ca_sentinel: Optional[SentinelPayload] = None
            if self._device_id:
                ca_sentinel = self._check_sentinel(self._device_id, flow="oauth_create_account")
                if ca_sentinel:
                    self._attach_create_account_sentinel(create_headers, ca_sentinel)

            response = None
            last_transport_err = ""
            for attempt in range(1, 5):
                # refresh sentinel on retry (single-use)
                if attempt > 1 and self._device_id:
                    ca_sentinel = self._check_sentinel(self._device_id, flow="oauth_create_account")
                    if ca_sentinel:
                        self._attach_create_account_sentinel(create_headers, ca_sentinel)
                        create_headers.update(_generate_datadog_trace_headers())
                try:
                    response = self.session.post(
                        OPENAI_API_ENDPOINTS["create_account"],
                        headers=create_headers,
                        data=create_account_body,
                        timeout=45,
                    )
                except Exception as te:
                    last_transport_err = str(te)
                    self._log(
                        f"账户创建传输失败 attempt={attempt}: {te}",
                        "warning",
                    )
                    time.sleep(0.8 * attempt + random.random() * 0.4)
                    continue
                self._log(f"账户创建状态: {response.status_code} attempt={attempt}")
                if response.status_code == 200:
                    break
                if response.status_code in {408, 425, 429} or response.status_code >= 500:
                    time.sleep(0.7 * attempt)
                    continue
                break

            if response is None or response.status_code != 200:
                body = response.text[:200] if response is not None else (last_transport_err or "no response")
                self._log(f"账户创建失败: {body}", "warning")
                return False

            # 提取 continue_url（ChatGPT Web 流程直接返回 OAuth callback URL）
            try:
                resp_data = response.json()
                self._create_account_continue_url = resp_data.get("continue_url", "")
                if self._create_account_continue_url:
                    self._log(f"create_account continue_url: {self._create_account_continue_url[:100]}...")
            except Exception:
                pass

            return True

        except Exception as e:
            self._log(f"创建账户失败: {e}", "error")
            return False

    def _acquire_codex_callback(self) -> Optional[str]:
        """
        注册完成后，通过 Codex CLI OAuth 完整登录流程获取 callback URL。
        使用新 session，走 authorize → authorize/continue → OTP → callback 流程。
        """
        try:
            from .constants import (
                CODEX_CLIENT_ID, CODEX_REDIRECT_URI, CODEX_SCOPE,
                OPENAI_AUTH, OPENAI_API_ENDPOINTS,
            )
            import urllib.parse

            self._log("开始 Codex CLI 登录流程...")

            # 1. 创建新 HTTP client + session
            login_client = OpenAIHTTPClient(proxy_url=self.proxy_url, profile=getattr(self, "_browser_profile", None))
            login_session = login_client.session

            # 2. 生成 Codex CLI OAuth URL (Hydra)
            codex_oauth = generate_oauth_url(
                redirect_uri=CODEX_REDIRECT_URI,
                scope=CODEX_SCOPE,
                client_id=CODEX_CLIENT_ID,
            )
            self._codex_oauth = codex_oauth

            # 3. 访问 authorize URL 获取 device_id + session cookies
            response = login_session.get(codex_oauth.auth_url, timeout=15)
            did = login_session.cookies.get("oai-did")
            self._log(f"Codex login device_id: {did}")
            if not did:
                self._log("Codex login 获取 device_id 失败", "error")
                return None

            # 4. 获取 Sentinel token
            sen_payload = None
            try:
                ua = getattr(login_client, "user_agent", None) or login_client.default_headers.get("User-Agent", "")
                generator = _SentinelTokenGenerator(did, ua, profile=getattr(self, "_browser_profile", None))
                sent_p = generator.generate_requirements_token()
                sen_req_body = json.dumps({"p": sent_p, "id": did, "flow": "authorize_continue"}, separators=(",", ":"))

                from .constants import SENTINEL_FRAME_URL
                sen_resp = login_client.post(
                    OPENAI_API_ENDPOINTS["sentinel"],
                    headers={
                        "origin": "https://sentinel.openai.com",
                        "referer": SENTINEL_FRAME_URL,
                        "content-type": "text/plain;charset=UTF-8",
                    },
                    data=sen_req_body,
                )
                if sen_resp.status_code == 200:
                    data = sen_resp.json()
                    turnstile = data.get("turnstile") or {}
                    pow_meta = data.get("proofofwork") or {}
                    if pow_meta.get("required") and pow_meta.get("seed"):
                        sent_p = generator.generate_token(
                            str(pow_meta.get("seed") or ""),
                            str(pow_meta.get("difficulty") or "0"),
                        )
                    t_raw = turnstile.get("dx", "")
                    t_val = ""
                    if t_raw:
                        try:
                            t_val = generator.decrypt_turnstile(t_raw, sent_p)
                        except Exception:
                            pass
                    sen_payload = SentinelPayload(p=sent_p, t=t_val, c=str(data.get("token") or ""), flow="authorize_continue")
                    self._log("Codex login Sentinel 已获取")
            except Exception as e:
                self._log(f"Codex login Sentinel 失败: {e}", "warning")

            # 5. authorize/continue 提交邮箱（登录已有账号）
            signup_body = f'{{"username":{{"value":"{self.email}","kind":"email"}},"screen_hint":"login"}}'
            headers = {
                "referer": "https://auth.openai.com/log-in",
                "accept": "application/json",
                "content-type": "application/json",
            }
            if sen_payload:
                headers["openai-sentinel-token"] = json.dumps({
                    "p": sen_payload.p, "t": sen_payload.t, "c": sen_payload.c,
                    "id": did, "flow": sen_payload.flow,
                }, separators=(",", ":"))

            resp = login_session.post(OPENAI_API_ENDPOINTS["signup"], headers=headers, data=signup_body)
            self._log(f"Codex login authorize/continue: {resp.status_code}")
            if resp.status_code != 200:
                self._log(f"Codex login authorize/continue 失败: {resp.text[:200]}", "error")
                return None

            resp_data = resp.json()
            page_type = resp_data.get("page", {}).get("type", "")
            self._log(f"Codex login page_type: {page_type}")

            # 6. 如果需要 OTP，等待第二次验证码
            if page_type == "email_otp_verification":
                self._log("等待第二次验证码...")
                self._otp_sent_at = time.time()
                code = self._get_verification_code()
                if not code:
                    self._log("Codex login 获取验证码失败", "error")
                    return None

                # 验证 OTP
                code_body = f'{{"code":"{code}"}}'
                otp_resp = login_session.post(
                    OPENAI_API_ENDPOINTS["validate_otp"],
                    headers={
                        "referer": "https://auth.openai.com/email-verification",
                        "accept": "application/json",
                        "content-type": "application/json",
                    },
                    data=code_body,
                )
                self._log(f"Codex login OTP 校验: {otp_resp.status_code}")
                if otp_resp.status_code != 200:
                    self._log(f"Codex login OTP 失败: {otp_resp.text[:200]}", "error")
                    return None

                otp_data = otp_resp.json()
                otp_page = otp_data.get("page", {}).get("type", "")
                self._log(f"Codex login OTP -> page_type={otp_page}")

                if otp_page == "add_phone":
                    self._log("Codex CLI 登录仍需 add_phone，无法跳过", "error")
                    return None

            # 7. 需要密码登录
            elif page_type in ("login_password", "create_account_password"):
                self._log(f"Codex login 提交密码...")
                if not self.password:
                    self._log("无密码可用", "error")
                    return None

                # 加载密码页获取 sentinel
                login_session.get(f"{OPENAI_AUTH}/log-in/password", timeout=15)
                pwd_sentinel = None
                try:
                    ua2 = getattr(login_client, "user_agent", None) or login_client.default_headers.get("User-Agent", "")
                    gen2 = _SentinelTokenGenerator(did, ua2, profile=getattr(self, "_browser_profile", None))
                    sp2 = gen2.generate_requirements_token()
                    sr2 = json.dumps({"p": sp2, "id": did, "flow": "login_password"}, separators=(",", ":"))
                    from .constants import SENTINEL_FRAME_URL as SF2
                    sr2_resp = login_client.post(
                        OPENAI_API_ENDPOINTS["sentinel"],
                        headers={"origin": "https://sentinel.openai.com", "referer": SF2, "content-type": "text/plain;charset=UTF-8"},
                        data=sr2,
                    )
                    if sr2_resp.status_code == 200:
                        d2 = sr2_resp.json()
                        pm2 = d2.get("proofofwork") or {}
                        if pm2.get("required") and pm2.get("seed"):
                            sp2 = gen2.generate_token(str(pm2.get("seed") or ""), str(pm2.get("difficulty") or "0"))
                        tr2 = (d2.get("turnstile") or {}).get("dx", "")
                        tv2 = ""
                        if tr2:
                            try: tv2 = gen2.decrypt_turnstile(tr2, sp2)
                            except: pass
                        pwd_sentinel = SentinelPayload(p=sp2, t=tv2, c=str(d2.get("token") or ""), flow="login_password")
                        self._log("Codex login 密码 Sentinel 已获取")
                except Exception as e:
                    self._log(f"Codex login 密码 Sentinel 失败: {e}", "warning")

                pwd_headers = {
                    "origin": OPENAI_AUTH,
                    "referer": f"{OPENAI_AUTH}/log-in/password",
                    "accept": "application/json",
                    "content-type": "application/json",
                }
                if did:
                    pwd_headers["oai-device-id"] = did
                if pwd_sentinel:
                    pwd_headers["openai-sentinel-token"] = json.dumps({
                        "p": pwd_sentinel.p, "t": pwd_sentinel.t, "c": pwd_sentinel.c,
                        "id": did, "flow": pwd_sentinel.flow,
                    }, separators=(",", ":"))

                pwd_body = json.dumps({"password": self.password, "username": self.email})
                pwd_resp = login_session.post(OPENAI_API_ENDPOINTS["register"], headers=pwd_headers, data=pwd_body)
                self._log(f"Codex login 密码提交: {pwd_resp.status_code}")
                if pwd_resp.status_code != 200:
                    self._log(f"Codex login 密码失败: {pwd_resp.text[:200]}", "error")
                    return None

                pwd_data = pwd_resp.json()
                pwd_page = pwd_data.get("page", {}).get("type", "")
                self._log(f"Codex login 密码 -> page_type={pwd_page}")

                # 密码后可能需要 OTP
                if pwd_page == "email_otp_verification" or pwd_page == "email_otp_send":
                    if pwd_page == "email_otp_send":
                        login_session.get(OPENAI_API_ENDPOINTS["send_otp"], headers={
                            "referer": f"{OPENAI_AUTH}/email-verification",
                        }, timeout=15)
                    self._log("Codex login: 等待验证码...")
                    self._otp_sent_at = time.time()
                    code = self._get_verification_code()
                    if not code:
                        self._log("Codex login 获取验证码失败", "error")
                        return None
                    code_body = f'{{"code":"{code}"}}'
                    otp_resp = login_session.post(
                        OPENAI_API_ENDPOINTS["validate_otp"],
                        headers={"referer": f"{OPENAI_AUTH}/email-verification", "accept": "application/json", "content-type": "application/json"},
                        data=code_body,
                    )
                    self._log(f"Codex login OTP: {otp_resp.status_code}")
                    if otp_resp.status_code != 200:
                        self._log(f"Codex login OTP 失败: {otp_resp.text[:200]}", "error")
                        return None
                    otp_data = otp_resp.json()
                    otp_page = otp_data.get("page", {}).get("type", "")
                    self._log(f"Codex login OTP -> page_type={otp_page}")
                    if otp_page == "add_phone":
                        self._log("Codex CLI 登录仍需 add_phone", "error")
                        return None

            # 8. 重新访问 authorize URL 获取回调
            self._log("Codex login: 重新访问 OAuth URL 获取回调...")
            response = login_session.get(codex_oauth.auth_url, allow_redirects=False, timeout=15)
            max_redirects = 10
            current_url = codex_oauth.auth_url
            for i in range(max_redirects):
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                location = response.headers.get("Location", "")
                if not location:
                    break
                next_url = urllib.parse.urljoin(current_url, location)
                self._log(f"Codex login 重定向 {i+1}: {next_url[:80]}...")
                if "code=" in next_url and "state=" in next_url:
                    self._log("找到 Codex CLI 回调 URL")
                    return next_url
                current_url = next_url
                response = login_session.get(current_url, allow_redirects=False, timeout=15)

            self._log(f"Codex login 最终: status={response.status_code}, url={current_url[:100]}", "warning")
            return None

        except Exception as e:
            self._log(f"Codex CLI 登录流程失败: {e}", "error")
            return None

    def _get_workspace_id(self) -> Optional[str]:
        """获取 Workspace ID"""
        try:
            auth_cookie = self.session.cookies.get("oai-client-auth-session")
            if not auth_cookie:
                self._log("未能获取到授权 Cookie", "error")
                return None

            # 解码 JWT
            import base64
            import json as json_module

            try:
                segments = auth_cookie.split(".")
                if len(segments) < 1:
                    self._log("授权 Cookie 格式错误", "error")
                    return None

                # 解码第一个 segment
                payload = segments[0]
                pad = "=" * ((4 - (len(payload) % 4)) % 4)
                decoded = base64.urlsafe_b64decode((payload + pad).encode("ascii"))
                auth_json = json_module.loads(decoded.decode("utf-8"))

                workspaces = auth_json.get("workspaces") or []
                if not workspaces:
                    self._log("授权 Cookie 里没有 workspace 信息", "error")
                    return None

                workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
                if not workspace_id:
                    self._log("无法解析 workspace_id", "error")
                    return None

                self._log(f"Workspace ID: {workspace_id}")
                return workspace_id

            except Exception as e:
                self._log(f"解析授权 Cookie 失败: {e}", "error")
                return None

        except Exception as e:
            self._log(f"获取 Workspace ID 失败: {e}", "error")
            return None

    def _select_workspace(self, workspace_id: str) -> Optional[str]:
        """选择 Workspace"""
        try:
            select_body = f'{{"workspace_id":"{workspace_id}"}}'

            response = self.session.post(
                OPENAI_API_ENDPOINTS["select_workspace"],
                headers={
                    "referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                    "content-type": "application/json",
                },
                data=select_body,
            )

            if response.status_code != 200:
                self._log(f"选择 workspace 失败: {response.status_code}", "error")
                self._log(f"响应: {response.text[:200]}", "warning")
                return None

            continue_url = str((response.json() or {}).get("continue_url") or "").strip()
            if not continue_url:
                self._log("workspace/select 响应里缺少 continue_url", "error")
                return None

            self._log(f"Continue URL: {continue_url[:100]}...")
            return continue_url

        except Exception as e:
            self._log(f"选择 Workspace 失败: {e}", "error")
            return None

    def _follow_redirects(self, start_url: str) -> Optional[str]:
        """跟随重定向链，寻找回调 URL"""
        try:
            current_url = start_url
            max_redirects = 6

            for i in range(max_redirects):
                self._log(f"重定向 {i+1}/{max_redirects}: {current_url[:100]}...")

                response = self.session.get(
                    current_url,
                    allow_redirects=False,
                    timeout=15
                )

                location = response.headers.get("Location") or ""

                # 如果不是重定向状态码，停止
                if response.status_code not in [301, 302, 303, 307, 308]:
                    self._log(f"非重定向状态码: {response.status_code}")
                    break

                if not location:
                    self._log("重定向响应缺少 Location 头")
                    break

                # 构建下一个 URL
                import urllib.parse
                next_url = urllib.parse.urljoin(current_url, location)

                # 检查是否包含回调参数
                if "code=" in next_url and "state=" in next_url:
                    self._log(f"找到回调 URL: {next_url[:100]}...")
                    return next_url

                current_url = next_url

            self._log("未能在重定向链中找到回调 URL", "error")
            return None

        except Exception as e:
            self._log(f"跟随重定向失败: {e}", "error")
            return None

    def _handle_oauth_callback(self, callback_url: str) -> Optional[Dict[str, Any]]:
        """处理 OAuth 回调"""
        try:
            if not self.oauth_start:
                self._log("OAuth 流程未初始化", "error")
                return None

            self._log("处理 OAuth 回调...")
            token_info = self.oauth_manager.handle_callback(
                callback_url=callback_url,
                expected_state=self.oauth_start.state,
                code_verifier=self.oauth_start.code_verifier
            )

            self._log("OAuth 授权成功")
            return token_info

        except Exception as e:
            self._log(f"处理 OAuth 回调失败: {e}", "error")
            return None

    def run(self) -> RegistrationResult:
        """
        执行完整的注册流程

        支持已注册账号自动登录：
        - 如果检测到邮箱已注册，自动切换到登录流程
        - 已注册账号跳过：设置密码、发送验证码、创建用户账户
        - 共用步骤：获取验证码、验证验证码、Workspace 和 OAuth 回调

        Returns:
            RegistrationResult: 注册结果
        """
        result = RegistrationResult(success=False, logs=self.logs)

        try:
            self._log("=" * 60)
            self._log("开始注册流程")
            self._log("=" * 60)

            # 1. 检查 IP 地理位置
            self._log("1. 检查 IP 地理位置...")
            ip_ok, location = self._check_ip_location()
            if not ip_ok:
                result.error_message = f"IP 地理位置不支持: {location}"
                self._log(f"IP 检查失败: {location}", "error")
                return result

            self._log(f"IP 位置: {location}")
            _random_delay(0.2, 0.6)

            # 2. 创建邮箱
            self._log("2. 创建邮箱...")
            if not self._create_email():
                result.error_message = "创建邮箱失败"
                return result

            result.email = self.email
            _random_delay(0.2, 0.7)

            # 3. 初始化会话
            self._log("3. 初始化会话...")
            if not self._init_session():
                result.error_message = "初始化会话失败"
                return result
            _random_delay(0.3, 0.9)

            # 4. 开始 OAuth 流程
            self._log("4. 开始 OAuth 授权流程...")
            if not self._start_oauth():
                result.error_message = "开始 OAuth 流程失败"
                return result
            _random_delay(0.3, 0.9)

            # 5. 获取 Device ID
            self._log("5. 获取 Device ID...")
            did = self._get_device_id()
            if not did:
                result.error_message = "获取 Device ID 失败"
                return result
            _random_delay(0.25, 0.8)

            # 6. 检查 Sentinel 拦截
            self._log("6. 检查 Sentinel 拦截...")
            sen_payload = self._check_sentinel(did)
            if sen_payload:
                self._log("Sentinel 检查通过")
            else:
                self._log("Sentinel 检查失败或未启用", "warning")
            _random_delay(0.3, 1.0)

            # 7. 提交注册表单 + 解析响应判断账号状态
            self._log("7. 提交注册表单...")
            signup_result = self._submit_signup_form(did, sen_payload)
            if not signup_result.success:
                result.error_message = f"提交注册表单失败: {signup_result.error_message}"
                return result
            _random_delay(0.4, 1.1)

            # 8. 密码 / passwordless
            if self._is_existing_account:
                self._log("8. [已注册账号] 跳过密码设置")
            elif getattr(self, "_is_passwordless_signup", False):
                self._log("8. [passwordless] 跳过密码设置，直接走邮箱 OTP")
            else:
                if getattr(self, "_force_password_path", False):
                    self._log("8. [login_challenge→password] 注册密码...")
                else:
                    self._log("8. 注册密码...")
                password_ok, password = self._register_password()
                if not password_ok:
                    # If OpenAI already parked the session on passwordless OTP, password
                    # create returns account_creation_failed. Fall back to OTP send.
                    if getattr(self, "_force_password_path", False) or getattr(self, "_email_verification_mode", "") in {
                        "passwordless_signup", "login_challenge", "login", "login_otp",
                    }:
                        self._log(
                            "密码注册失败，回退 passwordless OTP 发送路径",
                            "warning",
                        )
                        self._is_passwordless_signup = True
                        self._force_password_path = False
                        self._is_existing_account = False
                        # Password attempts often invalidate the auto-OTP session; do not
                        # trust the original auto-sent code after a failed create.
                        self._otp_auto_sent = False
                        self._otp_sent_at = None
                    else:
                        result.error_message = "注册密码失败"
                        return result
            _random_delay(0.3, 0.9)

            # 9. 发送验证码
            # login_or_signup may already auto-trigger OTP on authorize follow.
            # passwordless / partial new accounts still need explicit email-otp/send.
            # Only trust auto-OTP when we never left the passwordless path.
            skip_explicit_send = (
                bool(getattr(self, "_otp_auto_sent", False))
                and bool(getattr(self, "_is_passwordless_signup", False))
                and not bool(getattr(self, "_force_password_path", False))
                and str(os.environ.get("OPENAI_TRUST_AUTO_OTP", "1")).strip().lower()
                in {"1", "true", "yes", "on"}
            )
            if skip_explicit_send:
                self._log("9. [login_or_signup] authorize 已疑似触发 OTP，跳过显式 send（OPENAI_TRUST_AUTO_OTP=1）")
            else:
                if self._is_existing_account:
                    self._log("9. [已注册账号] 确保 OTP 已发送...")
                elif getattr(self, "_is_passwordless_signup", False):
                    self._log("9. [passwordless] 发送邮箱验证码...")
                else:
                    self._log("9. 发送验证码...")
                if not self._send_verification_code():
                    result.error_message = "发送验证码失败"
                    return result
            # Surface verification mode from last send response if captured.
            mode = getattr(self, "_email_verification_mode", "") or ""
            if mode:
                self._log(f"OTP 发送后 verification_mode={mode}")
                if mode in {"login_challenge", "login", "login_otp"}:
                    self._log(
                        "警告: send_otp 返回 login_challenge，OpenAI 可能静默不投递到 catch-all",
                        "warning",
                    )
            _random_delay(0.4, 1.0)

            # 10. 获取验证码
            self._log("10. 等待验证码...")
            code = self._get_verification_code()
            if not code:
                if getattr(self, "_otp_login_challenge", False) and self._login_challenge_fast_fail_enabled():
                    result.error_message = (
                        "获取验证码失败: login_challenge 静默不投递"
                        "（建议换邮箱域/住宅出口后重试）"
                    )
                else:
                    result.error_message = "获取验证码失败"
                return result
            _random_delay(0.25, 0.7)

            # 11. 验证验证码
            self._log("11. 验证验证码...")
            if not self._validate_verification_code(code):
                result.error_message = "验证验证码失败"
                return result
            _random_delay(0.3, 0.8)

            # 12. 根据 OTP 响应决定下一步
            if self._otp_page_type == "about_you" and not self._is_existing_account:
                # 正常注册流程: about_you → create_account
                self._log("12. 创建用户账户...")
                if not self._create_user_account():
                    result.error_message = "创建用户账户失败"
                    return result
            elif self._is_existing_account:
                self._log("12. [已注册账号] 跳过创建用户账户")
            else:
                self._log(f"12. OTP page_type={self._otp_page_type}，尝试创建账户...")
                if not self._create_user_account():
                    result.error_message = "创建用户账户失败"
                    return result

            # 13. 跟随 callback URL 到 chatgpt.com 获取 session
            callback_url = self._create_account_continue_url
            if not callback_url or "code=" not in str(callback_url):
                result.error_message = "create_account 未返回有效的 callback URL"
                return result

            self._log("13. 跟随 callback URL 到 chatgpt.com...")
            cb_resp = self.session.get(callback_url, timeout=20)
            self._log(f"callback 状态: {cb_resp.status_code}")

            # 提取 session cookie
            session_token = self.session.cookies.get("__Secure-next-auth.session-token")
            account_cookie = self.session.cookies.get("_account", "")
            if session_token:
                self._log(f"获取到 session-token: {session_token[:30]}...")
            if account_cookie:
                self._log(f"获取到 _account: {account_cookie}")

            # 14. 从 chatgpt.com/api/auth/session 获取 access_token
            from .constants import CHATGPT_APP
            self._log("14. 获取 session 信息...")
            session_resp = self.session.get(
                f"{CHATGPT_APP}/api/auth/session",
                headers={"accept": "application/json"},
                timeout=15,
            )
            self._log(f"session API 状态: {session_resp.status_code}")
            self._log(f"session API 响应: {session_resp.text[:500]}")

            session_data = session_resp.json()
            access_token = session_data.get("accessToken", "")
            user_data = session_data.get("user", {})
            self._log(f"session keys: {list(session_data.keys())}")
            self._log(f"accessToken 长度: {len(access_token)}")

            if not access_token:
                result.error_message = "chatgpt.com session 未返回 accessToken"
                return result

            self._log("NextAuth session 获取成功")

            # 15. Codex CLI OTP 登录获取 refresh_token + id_token
            codex_token_info = None
            try:
                self._log("15. Codex CLI OTP 登录...")
                from .constants import (
                    CODEX_CLIENT_ID, CODEX_REDIRECT_URI, CODEX_SCOPE,
                    OPENAI_AUTH, SENTINEL_FRAME_URL,
                )
                import urllib.parse

                codex_oauth = generate_oauth_url(
                    redirect_uri=CODEX_REDIRECT_URI,
                    scope=CODEX_SCOPE,
                    client_id=CODEX_CLIENT_ID,
                )

                # 用全新 session（Hydra 需要干净 session）
                login_client = OpenAIHTTPClient(proxy_url=self.proxy_url, profile=getattr(self, "_browser_profile", None))
                login_session = login_client.session

                # 访问 Codex OAuth URL，跟随重定向到 /log-in
                login_session.get(codex_oauth.auth_url, timeout=15)
                did2 = login_session.cookies.get("oai-did", "")
                self._log(f"Codex login did: {did2[:20]}...")

                # 获取 sentinel（用 login_client）
                sen2 = None
                try:
                    ua2 = getattr(login_client, "user_agent", None) or login_client.default_headers.get("User-Agent", "")
                    gen2 = _SentinelTokenGenerator(did2, ua2, profile=getattr(self, "_browser_profile", None))
                    sp2 = gen2.generate_requirements_token()
                    sr2 = json.dumps({"p": sp2, "id": did2, "flow": "authorize_continue"}, separators=(",", ":"))
                    sr2_resp = login_client.post(
                        OPENAI_API_ENDPOINTS["sentinel"],
                        headers={"origin": "https://sentinel.openai.com", "referer": SENTINEL_FRAME_URL, "content-type": "text/plain;charset=UTF-8"},
                        data=sr2,
                    )
                    if sr2_resp.status_code == 200:
                        d2 = sr2_resp.json()
                        pm2 = d2.get("proofofwork") or {}
                        if pm2.get("required") and pm2.get("seed"):
                            sp2 = gen2.generate_token(str(pm2.get("seed") or ""), str(pm2.get("difficulty") or "0"))
                        tr2 = (d2.get("turnstile") or {}).get("dx", "")
                        tv2 = ""
                        if tr2:
                            try: tv2 = gen2.decrypt_turnstile(tr2, sp2)
                            except: pass
                        sen2 = SentinelPayload(p=sp2, t=tv2, c=str(d2.get("token") or ""), flow="authorize_continue")
                        self._log("Codex sentinel 获取成功")
                except Exception as e:
                    self._log(f"Codex sentinel 失败: {e}", "warning")

                # authorize/continue 提交邮箱（不带 screen_hint，让 codex_cli_simplified_flow 决定）
                signup_headers = {
                    "referer": f"{OPENAI_AUTH}/log-in",
                    "accept": "application/json",
                    "content-type": "application/json",
                }
                if sen2 and did2:
                    signup_headers["openai-sentinel-token"] = json.dumps({
                        "p": sen2.p, "t": sen2.t, "c": sen2.c,
                        "id": did2, "flow": sen2.flow,
                    }, separators=(",", ":"))

                signup_body = json.dumps({"username": {"value": self.email, "kind": "email"}, "screen_hint": "signup"})
                signup_resp = login_session.post(
                    OPENAI_API_ENDPOINTS["signup"], headers=signup_headers, data=signup_body
                )
                self._log(f"Codex authorize/continue: {signup_resp.status_code}")
                if signup_resp.status_code != 200:
                    raise RuntimeError(f"authorize/continue 失败: {signup_resp.text[:200]}")

                page_type = signup_resp.json().get("page", {}).get("type", "")
                self._log(f"Codex page_type: {page_type}")

                # 如果返回 email_otp_send 或 email_otp_verification，走 OTP 流程
                if page_type in ("email_otp_send", "email_otp_verification"):
                    # 发送 OTP
                    if page_type == "email_otp_send":
                        login_session.get(OPENAI_API_ENDPOINTS["send_otp"], headers={
                            "referer": f"{OPENAI_AUTH}/email-verification",
                        }, timeout=15)
                        self._log("Codex OTP 已发送")

                    # 等待 OTP
                    self._otp_sent_at = time.time()
                    code = self._get_verification_code()
                    if not code:
                        raise RuntimeError("Codex OTP 获取失败")
                    self._log(f"Codex OTP: {code}")

                    # 验证 OTP
                    otp_resp = login_session.post(
                        OPENAI_API_ENDPOINTS["validate_otp"],
                        headers={
                            "referer": f"{OPENAI_AUTH}/email-verification",
                            "accept": "application/json",
                            "content-type": "application/json",
                        },
                        data=json.dumps({"code": code}),
                    )
                    self._log(f"Codex OTP validate: {otp_resp.status_code}")
                    if otp_resp.status_code != 200:
                        raise RuntimeError(f"Codex OTP 验证失败: {otp_resp.text[:200]}")

                    otp_data = otp_resp.json()
                    otp_page = otp_data.get("page", {}).get("type", "")
                    self._log(f"Codex OTP -> page_type={otp_page}")

                    if otp_page == "add_phone":
                        self._log("Codex CLI 仍需 add_phone，跳过", "warning")
                        raise RuntimeError("add_phone required")

                    # OTP 成功后，重新访问 OAuth URL 获取 callback
                    self._log("Codex: 重新访问 OAuth URL...")
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
                        self._log(f"Codex 重定向 {i+1}: {next_url[:80]}...")
                        if "code=" in next_url and "state=" in next_url:
                            codex_callback = next_url
                            break
                        current_url = next_url
                        resp = login_session.get(current_url, allow_redirects=False, timeout=15)

                    if codex_callback:
                        self._log("Codex CLI callback 获取成功")
                        token_json = submit_callback_url(
                            callback_url=codex_callback,
                            expected_state=codex_oauth.state,
                            code_verifier=codex_oauth.code_verifier,
                            redirect_uri=CODEX_REDIRECT_URI,
                            client_id=CODEX_CLIENT_ID,
                            proxy_url=self.proxy_url,
                        )
                        codex_token_info = json.loads(token_json)
                        self._log(f"Codex token 成功: keys={list(codex_token_info.keys())}")
                    else:
                        self._log(f"Codex callback 未获取 (status={resp.status_code})", "warning")
                else:
                    self._log(f"Codex 非 OTP 流程 ({page_type})，跳过", "warning")
            except Exception as e:
                self._log(f"Codex CLI 登录失败: {e}", "warning")

            # 提取账户信息（优先 Codex token，fallback 到 NextAuth session）
            if codex_token_info and codex_token_info.get("access_token"):
                self._log("使用 Codex CLI token（完整 refresh_token + id_token）")
                result.account_id = codex_token_info.get("account_id", "") or account_cookie or ""
                result.access_token = codex_token_info.get("access_token", "")
                result.refresh_token = codex_token_info.get("refresh_token", "")
                result.id_token = codex_token_info.get("id_token", "")
            else:
                self._log("使用 NextAuth session token", "warning")
                result.account_id = account_cookie or ""
                result.access_token = access_token
                result.refresh_token = ""
                # access_token JWT 包含 chatgpt_account_id 等同于 id_token 的 claims
                result.id_token = access_token

            result.password = self.password or ""
            result.source = "login" if self._is_existing_account else "register"

            if session_token:
                self.session_token = session_token
                result.session_token = session_token
                self._log(f"获取到 Session Token")

            # 17. 完成
            self._log("=" * 60)
            if self._is_existing_account:
                self._log("登录成功! (已注册账号)")
            else:
                self._log("注册成功!")
            self._log(f"邮箱: {result.email}")
            self._log(f"Account ID: {result.account_id}")
            self._log(f"Workspace ID: {result.workspace_id}")
            self._log("=" * 60)

            result.success = True
            result.metadata = {
                "email_service": self.email_service.service_type.value,
                "proxy_used": self.proxy_url,
                "registered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "is_existing_account": self._is_existing_account,
            }

            return result

        except Exception as e:
            self._log(f"注册过程中发生未预期错误: {e}", "error")
            result.error_message = str(e)
            return result

    def save_to_database(self, result: RegistrationResult) -> bool:
        """
        保存注册结果到数据库

        Args:
            result: 注册结果

        Returns:
            是否保存成功
        """
        if not result.success:
            return False

        return True  # 由 account_manager 统一处理存库
