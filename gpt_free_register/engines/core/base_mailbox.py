"""邮箱池基类 - 抽象临时邮箱/收件服务"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
import logging
logger = logging.getLogger(__name__)


@dataclass
class MailboxAccount:
    email: str
    account_id: str = ""
    extra: dict = None  # 平台额外信息


class BaseMailbox(ABC):
    @abstractmethod
    def get_email(self) -> MailboxAccount:
        """获取一个可用邮箱"""
        ...

    @abstractmethod
    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None,
                      code_pattern: str = None) -> str:
        """等待并返回验证码，code_pattern 为自定义正则（默认匹配6位数字）"""
        ...

    @abstractmethod
    def get_current_ids(self, account: MailboxAccount) -> set:
        """返回当前邮件 ID 集合（用于过滤旧邮件）"""
        ...

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        """等待并返回验证链接。默认由具体 provider 自行实现。"""
        raise NotImplementedError(f"{self.__class__.__name__} 暂不支持 wait_for_link()")


def _create_cfd1(extra: dict, proxy: str | None) -> 'BaseMailbox':
    import os
    from core.proxy_env import load_dotenv

    load_dotenv()
    cfg = dict(extra or {})

    def _pick(*keys: str, default: str = "") -> str:
        for key in keys:
            value = str(cfg.get(key, "") or "").strip()
            if value:
                return value
            value = str(os.getenv(key, "") or os.getenv(key.upper(), "") or "").strip()
            if value:
                return value
        return default

    # If caller passed None, only honor CFD1_PROXY. Never REGISTER_PROXY_DEFAULT
    # (stale SOCKS previously swallowed the entire OTP wait).
    if not proxy:
        proxy = str(os.getenv("CFD1_PROXY") or "").strip() or None

    return CloudflareD1Mailbox(
        api_token=_pick("cfd1_api_token", "cf_api_token", "CLOUDFLARE_API_TOKEN", "CF_API_TOKEN"),
        account_id=_pick("cfd1_account_id", "cf_account_id", "CLOUDFLARE_ACCOUNT_ID"),
        database_id=_pick("cfd1_database_id", "cf_d1_id", "d1_database_id", "CLOUDFLARE_D1_DB_ID", "CLOUDFLARE_D1_DATABASE_ID"),
        domain=_pick("cfd1_domain", "cf_mail_domain", "CLOUDFLARE_EMAIL_DOMAIN", "MAIL_DOMAIN", default="lg.zc.233159.xyz"),
        api_base=_pick("cfd1_api_base"),
        table=_pick("cfd1_table", default="raw_mails"),
        address_column=_pick("cfd1_address_column", default="address"),
        raw_column=_pick("cfd1_raw_column", default="raw"),
        id_column=_pick("cfd1_id_column", default="id"),
        local_part_length=int(_pick("cfd1_local_part_length", default="12") or 12),
        local_part_prefix=_pick("cfd1_local_part_prefix"),
        proxy=proxy,
    )


MAILBOX_FACTORY_REGISTRY = {
    "cloudflare_d1_api": _create_cfd1,
    "cloudflare_d1": _create_cfd1,
    "cfd1": _create_cfd1,
}


def create_mailbox(provider: str, extra: dict = None, proxy: str = None) -> "BaseMailbox":
    key = str(provider or "").strip() or "cloudflare_d1_api"
    factory = MAILBOX_FACTORY_REGISTRY.get(key)
    if not factory:
        raise RuntimeError(f"不支持的邮箱 provider: {key}（仅 cloudflare_d1）")
    return factory(extra or {}, proxy)


class CloudflareD1Mailbox(BaseMailbox):
    """Cloudflare Email Routing + Worker 落库 D1，本地建号，D1 HTTP API 读信。

    与 cfworker_admin_api 不同：
    - 不调用 Worker /admin/new_address
    - 不走 IMAP/POP3
    - 本地随机 local-part@domain
    - 通过 Cloudflare D1 REST API 查询 raw_mails 表
    """

    DEFAULT_API_BASE = "https://api.cloudflare.com/client/v4"
    DEFAULT_TABLE = "raw_mails"
    DEFAULT_ADDRESS_COLUMN = "address"
    DEFAULT_RAW_COLUMN = "raw"
    DEFAULT_ID_COLUMN = "id"

    def __init__(
        self,
        *,
        api_token: str,
        account_id: str,
        database_id: str,
        domain: str,
        api_base: str = "",
        table: str = "",
        address_column: str = "",
        raw_column: str = "",
        id_column: str = "",
        local_part_length: int = 12,
        local_part_prefix: str = "",
        proxy: str | None = None,
    ):
        self.api_token = str(api_token or "").strip()
        self.account_id = str(account_id or "").strip()
        self.database_id = str(database_id or "").strip()
        self.domain = str(domain or "").strip().lstrip("@")
        self.api_base = (api_base or self.DEFAULT_API_BASE).rstrip("/")
        self.table = self._safe_ident(table or self.DEFAULT_TABLE, "table")
        self.address_column = self._safe_ident(address_column or self.DEFAULT_ADDRESS_COLUMN, "address_column")
        self.raw_column = self._safe_ident(raw_column or self.DEFAULT_RAW_COLUMN, "raw_column")
        self.id_column = self._safe_ident(id_column or self.DEFAULT_ID_COLUMN, "id_column")
        self.local_part_length = max(4, int(local_part_length or 12))
        self.local_part_prefix = str(local_part_prefix or "").strip()
        self.proxy = {"http": proxy, "https": proxy} if proxy else None

        missing = [name for name, value in (
            ("cfd1_api_token", self.api_token),
            ("cfd1_account_id", self.account_id),
            ("cfd1_database_id", self.database_id),
            ("cfd1_domain", self.domain),
        ) if not value]
        if missing:
            raise RuntimeError("Cloudflare D1 邮箱缺少配置: " + ", ".join(missing))

    @staticmethod
    def _safe_ident(value: str, label: str) -> str:
        import re
        name = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"非法 D1 标识符 {label}={value!r}")
        return name

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _query_url(self) -> str:
        return (
            f"{self.api_base}/accounts/{self.account_id}/d1/database/"
            f"{self.database_id}/query"
        )

    def _d1_query(self, sql: str, params: list | None = None) -> list[dict]:
        import requests

        payload = {"sql": sql, "params": list(params or [])}
        session = requests.Session()
        session.trust_env = False
        r = session.post(
            self._query_url(),
            headers=self._headers(),
            json=payload,
            proxies=self.proxy,
            timeout=20,
        )
        try:
            data = r.json()
        except Exception as exc:
            raise RuntimeError(f"D1 响应非 JSON status={r.status_code} body={r.text[:200]}") from exc
        if r.status_code >= 400 or not data.get("success", False):
            errors = data.get("errors") or data.get("messages") or r.text[:300]
            raise RuntimeError(f"D1 查询失败 status={r.status_code}: {errors}")

        results = data.get("result") or []
        # Cloudflare D1 query API returns a list of statement results.
        if isinstance(results, list) and results:
            first = results[0]
            if isinstance(first, dict):
                rows = first.get("results")
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
            if isinstance(first, list):
                return [row for row in first if isinstance(row, dict)]
        if isinstance(results, dict):
            rows = results.get("results")
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return []

    def _make_local_part(self) -> str:
        import random
        import string
        body = "".join(random.choices(string.ascii_lowercase + string.digits, k=self.local_part_length))
        prefix = self.local_part_prefix
        if prefix and not prefix.endswith(("-", "_", ".")):
            # keep local-part valid; allow user-provided separator if already present
            return f"{prefix}{body}"
        return f"{prefix}{body}" if prefix else body

    def get_email(self) -> MailboxAccount:
        local = self._make_local_part()
        email = f"{local}@{self.domain}".lower()
        print(f"[CF-D1] 本地建号: {email}")
        return MailboxAccount(
            email=email,
            account_id=email,
            extra={
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "cloudflare_d1",
                    "resource_type": "mailbox",
                    "resource_identifier": email,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "domain": self.domain,
                        "account_id": self.account_id,
                        "database_id": self.database_id,
                        "table": self.table,
                    },
                },
            },
        )

    def _list_mails(self, email: str, *, limit: int = 30) -> list[dict]:
        sql = (
            f"SELECT {self.id_column} AS id, {self.address_column} AS address, "
            f"{self.raw_column} AS raw "
            f"FROM {self.table} "
            f"WHERE {self.address_column} = ? "
            f"ORDER BY {self.id_column} DESC "
            f"LIMIT {int(limit)}"
        )
        return self._d1_query(sql, [email])

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            mails = self._list_mails(account.email)
            return {str(m.get("id", "")) for m in mails if m.get("id") not in (None, "")}
        except Exception as exc:
            logger.warning("[CF-D1] get_current_ids failed: %s", exc)
            return set()

    @staticmethod
    def _extract_code_from_raw(raw: str, code_pattern: str | None = None) -> str:
        import re
        text = str(raw or "")
        if not text:
            return ""

        # OpenAI ChatGPT temporary codes are often rendered as a bare 6-digit
        # token between Outlook conditional comments:
        #   <![endif]-->\n  894462\n  <!--[if mso]>
        # Prefer that high-signal pattern before any generic 6-digit scan.
        # Apply the same blacklist to span/mso hits: brand colors and tm1
        # shadow OTP 493682 must not short-circuit the real code.
        high_signal_blacklist = {
            "233159",
            "353740",
            "216706",
            "000000",
            "ffffff",
            "493682",
        }
        # gpt-free-register: unique 6-digit in Subject is the OpenAI code.
        header = text.split("\r\n\r\n", 1)[0].split("\n\n", 1)[0]
        subj_m = re.search(r"(?im)^Subject:\s*(.+)$", header)
        if subj_m:
            unique = [
                c for c in re.findall(r"(?<!\d)(\d{6})(?!\d)", subj_m.group(1))
                if c not in high_signal_blacklist
            ]
            if len(unique) == 1:
                return unique[0]
        for pattern in (
            r"<!\[endif\]-->\s*(\d{6})\s*<!--\[if mso\]>",
            r"endif\]-->\s*(\d{6})\s*<!--\[if",
            r"<span[^>]*>\s*(\d{6})\s*</span>",
            r"<b[^>]*>\s*(\d{6})\s*</b>",
            r"<strong[^>]*>\s*(\d{6})\s*</strong>",
            r"(?:code|验证码|otp|one[- ]time|security code|認証コード|인증)[^0-9#]{0,40}(\d{6})",
        ):
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                code = m.group(1)
                if code and code not in high_signal_blacklist:
                    return code

        body_start = text.find("\r\n\r\n")
        if body_start == -1:
            body_start = text.find("\n\n")
        search_text = text[body_start:] if body_start != -1 else text
        # Drop noise that commonly contains 6-digit false positives.
        search_text = re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", " ", search_text)
        search_text = re.sub(r"https?://\S+", " ", search_text)
        search_text = re.sub(r"m=\+\d+\.\d+", " ", search_text)
        search_text = re.sub(r"\bt=\d+\b", " ", search_text)
        # CSS / brand colors (#353740) and quoted-printable color fragments.
        search_text = re.sub(r"#[0-9A-Fa-f]{6}\b", " ", search_text)
        search_text = re.sub(r"color:\s*#?[0-9A-Fa-f]{3,8}", " ", search_text, flags=re.I)
        search_text = re.sub(r"rgb\([^)]*\)", " ", search_text, flags=re.I)
        # Prefer whitespace-bounded standalone codes over mid-token digits.
        blacklist = {
            "233159",  # domain noise
            "353740",  # ChatGPT brand gray #353740
            "216706",  # sendgrid click host fragment
            "000000",
            "ffffff",
            "493682",  # tm1.openai.com broken sender (always this code, always 401)
        }
        # If caller provided a custom pattern, still honor it after noise scrub.
        pattern = code_pattern or r"(?<![\d#A-Fa-f])(\d{6})(?![\dA-Fa-f])"
        for m in re.finditer(pattern, search_text, re.IGNORECASE):
            code = next((g for g in m.groups() if g), None) or m.group(0)
            if code and code not in blacklist and code.isdigit():
                return code
        return ""

    @staticmethod
    def _mail_received_epoch(raw: str) -> float | None:
        """Best-effort parse Date header from raw MIME for otp_sent_at filtering."""
        import email.utils
        import re
        import time as _time

        text = str(raw or "")
        if not text:
            return None
        # Prefer true header section before body.
        header = text.split("\r\n\r\n", 1)[0].split("\n\n", 1)[0]
        m = re.search(r"(?im)^Date:\s*(.+)$", header)
        if not m:
            m = re.search(r"(?im)^Date:\s*(.+)$", text[:4000])
        if not m:
            return None
        try:
            ts = email.utils.parsedate_to_datetime(m.group(1).strip())
            if ts is None:
                return None
            if ts.tzinfo is None:
                return ts.timestamp()
            return ts.timestamp()
        except Exception:
            try:
                # fallback: email.utils.parsedate_tz
                tt = email.utils.parsedate_tz(m.group(1).strip())
                if not tt:
                    return None
                return float(email.utils.mktime_tz(tt))
            except Exception:
                return None

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        otp_sent_at: float | None = None,
        min_received_at: float | None = None,
        poll_interval: float = 2.5,
    ) -> str:
        import time

        seen = set(str(x) for x in (before_ids or set()) if x not in (None, ""))
        start = time.time()
        email = account.email
        # allow small clock skew between OpenAI send and worker Date header
        min_ts = otp_sent_at if otp_sent_at is not None else min_received_at
        if min_ts is not None:
            try:
                min_ts = float(min_ts) - 15.0
            except Exception:
                min_ts = None
        print(
            f"[CF-D1] 等待验证码: {email} timeout={timeout}s "
            f"before_ids={len(seen)} min_ts={int(min_ts) if min_ts else '-'}"
        )
        last_err = ""
        while time.time() - start < timeout:
            try:
                mails = self._list_mails(email)
                for mail in mails:
                    mid = str(mail.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    raw = str(mail.get("raw", "") or "")
                    if min_ts is not None:
                        received = self._mail_received_epoch(raw)
                        # If Date unparsable, still accept new id not in baseline.
                        if received is not None and received < min_ts:
                            seen.add(mid)
                            continue
                    seen.add(mid)
                    if keyword and keyword.lower() not in raw.lower():
                        continue
                    code = self._extract_code_from_raw(raw, code_pattern=code_pattern)
                    if code:
                        print(f"[CF-D1] 验证码: {code}")
                        return code
            except Exception as exc:
                last_err = str(exc)[:160]
                logger.warning("[CF-D1] poll failed: %s", exc)
            # adaptive poll: slightly faster early, then settle
            elapsed = time.time() - start
            sleep_s = float(poll_interval)
            if elapsed < 12:
                # OTP usually arrives within a few seconds; poll hard first.
                sleep_s = max(0.8, min(sleep_s, 1.2))
            elif elapsed < 30:
                sleep_s = max(1.2, sleep_s - 0.5)
            time.sleep(sleep_s)
        suffix = f" last_err={last_err}" if last_err else ""
        raise TimeoutError(f"等待验证码超时 ({timeout}s) email={email}{suffix}")
