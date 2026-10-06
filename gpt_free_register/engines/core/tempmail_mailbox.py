"""123nhh/tempmail（自建 Go + PostgreSQL 临时邮箱）邮箱池实现。

与 Cloudflare D1 的区别：
- 服务端建号：POST /api/mailboxes 返回 mailbox.id + full_address（TTL 默认 30 分钟）
- 收信走 REST：GET /api/mailboxes/:id/emails（只含主题）+ /emails/:email_id（正文）
- 鉴权：Authorization: Bearer tm_xxx
- Postfix 收信后 ≤100ms 落库，不用等 Worker → D1 → Cloudflare API 这一圈

请求预算：tempmail 的限流按 API Key 计数，而且每个请求（包括被 429 拒掉的）
都会把 60s 窗口续期——只要一直有请求，计数就永远不归零。所以这里：
- OpenAI 验证码在主题里，先用列表的 subject 提取，拿不到才读正文
- 刚建的空邮箱不去查基线
- 一旦 429，本进程所有 tempmail 请求静默一个完整窗口，让计数过期
"""
from __future__ import annotations

import email.utils
import hashlib
import logging
import secrets
import string
import threading
import time
from datetime import datetime

from core.base_mailbox import BaseMailbox, CloudflareD1Mailbox, MailboxAccount

logger = logging.getLogger(__name__)

# 标签形状：真实邮件主机名（mail1 / mx2 / smtp3 / mx-a / email01），不是纯随机串。
# 纯随机串（如 x9k2m）在地址里是「临时邮箱」的强特征；混入 mail/mx/smtp 词根更像企业域。
_LABEL_ROOTS = (
    "mail", "mx", "smtp", "mailer", "mailbox", "inbox", "mxmail", "mailgw",
    "email", "imap", "pop", "relay", "post", "host", "edge", "gate", "node",
    "correo", "posta", "courriel", "serwer", "correio", "messagerie",
)
_LABEL_SUFFIXES = ("", "1", "2", "3", "01", "02", "03", "10", "20", "x", "a", "b", "s", "i", "n", "e")
# 无词根时的备选：短小写词，避免出现纯随机 4~8 位串
_LABEL_WORDS = (
    "alpha", "bravo", "cedar", "delta", "eagle", "flint", "gamma", "harbor", "iris",
    "juno", "kelp", "lumen", "maple", "north", "orbit", "pixel", "quartz", "raven",
    "sierra", "tango", "umber", "violet", "willow", "xenon", "yarrow", "zephyr",
    "amber", "birch", "cobalt", "dune", "ember", "frost", "grove", "haze", "inlet",
    "jasper", "kite", "larch", "mica", "nimbus", "onyx", "prism", "quill", "ridge",
)
# 标签要满足服务端 domainLabelPattern：^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$
_LABEL_MAX_LEN = 63

# 域名黑名单预检：DBL 会向下继承（dpdns.org 命中时其 18 级子域同样命中），
# 所以只看「域名池里的主域」就能判断整棵子树。命中就别再拿它建号了。
_DBL_ZONES = (
    "dbl.spamhaus.org",
    "dbl.nordspam.com",
)
_DBL_CACHE: dict[str, tuple[float, bool]] = {}
_DBL_LOCK = threading.Lock()
_DBL_TTL_SECS = 1800

# api_key 指纹 -> 限流静默截止时间（本机时钟）。所有注册线程共享，
# 否则别的线程的轮询会一直给服务端窗口续期。
_COOLDOWN_UNTIL: dict[str, float] = {}
_COOLDOWN_LOCK = threading.Lock()
_DEFAULT_RATE_WINDOW_SECS = 60

# (base_url, api_key 指纹) -> (取回时间, 已激活域名行)。域名池里写子域名 / *.通配时
# 要靠它找出主域；每次注册都查一遍太浪费请求。
_DOMAINS_CACHE: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_DOMAINS_LOCK = threading.Lock()


class TempMailError(RuntimeError):
    """tempmail 服务端错误；status 用于区分鉴权/限流/邮箱不存在。"""

    def __init__(self, message: str, *, status: int = 0, detail: str = ""):
        super().__init__(message)
        self.status = int(status or 0)
        self.detail = str(detail or "")


def normalize_base_url(value: str) -> str:
    """https://mail.example.com[/api][/] → https://mail.example.com"""
    url = str(value or "").strip().rstrip("/")
    if url.lower().endswith("/api"):
        url = url[:-4].rstrip("/")
    return url


def _parse_iso(value) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class TempMailMailbox(BaseMailbox):
    PROVIDER_NAME = "tempmail"
    MODES = ("single", "multi")
    MAX_CREATE_ATTEMPTS = 4
    LIST_PAGE_SIZE = 100
    LIST_MAX_PAGES = 10
    # Codex 补 OTP 复用旧地址时，剩余 TTL 少于这个值就删掉重建。
    MIN_REMAINING_TTL_SECS = 180
    # multi 模式默认自己生成 17 级子域名（abc@mail2.mx1.smtp3.….example.com）。
    # 0 = 交给 tempmail 生成：它会拼 10~14 级 gmail/yahoo/proton… 单词，地址又长又像伪装。
    DEFAULT_SUBDOMAIN_DEPTH = 17
    # 24 级实测可用（地址仍在上限内）；再深地址会逼近 RFC 5321 的 254 字符上限。
    # 改这里要同步 services/gpt_register_service.py 的 TEMPMAIL_MAX_SUBDOMAIN_DEPTH。
    MAX_SUBDOMAIN_DEPTH = 24
    # RFC 5321 路径上限 254 字符；域名部分留够 local-part（含 @）的预算。
    MAX_DOMAIN_LEN = 240
    DOMAINS_CACHE_TTL_SECS = 600
    # 主域被 DBL 之类拉黑时直接跳过：整棵子树都会命中，建号也收不到信。
    SKIP_BLOCKLISTED_DOMAINS = True

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        domain: str = "",
        mode: str = "",
        subdomain_depth: int = DEFAULT_SUBDOMAIN_DEPTH,
        local_part_length: int = 12,
        local_part_prefix: str = "",
        proxy: str | None = None,
        timeout: float = 15.0,
    ):
        self.base_url = normalize_base_url(base_url)
        self.api_key = str(api_key or "").strip()
        # example.com（主域）/ *.example.com（其下随机子域名）/ a.example.com（固定子域名）
        self.domain = str(domain or "").strip().lstrip("@").lower()
        mode = str(mode or "").strip().lower()
        self.mode = mode if mode in self.MODES else ""
        try:
            depth = int(subdomain_depth)
        except (TypeError, ValueError):
            depth = self.DEFAULT_SUBDOMAIN_DEPTH
        self.subdomain_depth = max(0, min(self.MAX_SUBDOMAIN_DEPTH, depth))
        self.local_part_length = max(4, int(local_part_length or 12))
        self.local_part_prefix = str(local_part_prefix or "").strip().lower()
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self.timeout = float(timeout)
        self._session = None
        self._key_fp = hashlib.sha256(self.api_key.encode()).hexdigest()[:16]
        # 服务端时钟 - 本机时钟（秒）。received_at 是服务端时间，otp_sent_at 是本机时间。
        self._clock_offset = 0.0
        self._ids_by_email: dict[str, str] = {}
        # 本实例刚建、还没轮询过的邮箱：基线必然为空，不必请求。
        self._fresh: set[str] = set()
        # 已经取走验证码的邮件：注册后的第二次 OTP 不能再拿到第一封。
        self._consumed: dict[str, set[str]] = {}

        missing = [name for name, value in (
            ("tempmail_base_url", self.base_url),
            ("tempmail_api_key", self.api_key),
        ) if not value]
        if missing:
            raise RuntimeError("tempmail 邮箱缺少配置: " + ", ".join(missing))

    # ------------------------------------------------------------------ HTTP

    def _http(self):
        if self._session is None:
            import requests

            session = requests.Session()
            session.trust_env = False
            session.headers.update({
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            })
            self._session = session
        return self._session

    def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    def cooldown_remaining(self) -> float:
        with _COOLDOWN_LOCK:
            return max(0.0, _COOLDOWN_UNTIL.get(self._key_fp, 0.0) - time.time())

    def _enter_cooldown(self, secs: float) -> None:
        now = time.time()
        with _COOLDOWN_LOCK:
            previous = _COOLDOWN_UNTIL.get(self._key_fp, 0.0)
            _COOLDOWN_UNTIL[self._key_fp] = max(previous, now + secs)
        if previous <= now:  # 并发线程各自撞到 429 时只提示一次
            print(
                f"[tempmail] 触发限流，全部 tempmail 请求静默 {int(secs)}s "
                "（服务端每个请求都会续期窗口，调大 tempmail 的 RATE_LIMIT 可避免）"
            )

    def _note_server_clock(self, resp) -> None:
        date = resp.headers.get("Date") if resp is not None else None
        if not date:
            return
        try:
            server_now = email.utils.parsedate_to_datetime(date).timestamp()
        except Exception:
            return
        self._clock_offset = server_now - time.time()

    def _request(self, method: str, path: str, *, json_body: dict | None = None,
                 params: dict | None = None) -> dict:
        import requests

        remaining = self.cooldown_remaining()
        if remaining > 0:
            raise TempMailError(f"tempmail 限流静默中，还剩 {remaining:.0f}s", status=429)
        url = f"{self.base_url}/api/{path.lstrip('/')}"
        try:
            resp = self._http().request(
                method, url, json=json_body, params=params,
                proxies=self.proxy, timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise TempMailError(f"tempmail 请求失败 {method} /api/{path}: {exc}") from exc
        self._note_server_clock(resp)
        try:
            data = resp.json() if (resp.text or "").strip() else {}
        except ValueError:
            data = None
        if resp.status_code >= 400:
            detail = data.get("error") if isinstance(data, dict) and data.get("error") else (resp.text or "")[:200]
            if resp.status_code == 429:
                window = _DEFAULT_RATE_WINDOW_SECS
                if isinstance(data, dict):
                    try:
                        window = max(1, int(data.get("retry_after") or window))
                    except (TypeError, ValueError):
                        pass
                self._enter_cooldown(window + 2)
            hint = f"API Key 无效或缺失（{detail}）" if resp.status_code == 401 else detail
            raise TempMailError(f"tempmail {method} /api/{path} → {resp.status_code}: {hint}",
                                status=resp.status_code, detail=str(detail))
        if not isinstance(data, dict):
            # 常见于 base_url 填成了前端页面（nginx 把未知路径 rewrite 到 index.html）
            raise TempMailError(
                f"tempmail 响应非 JSON（检查 base_url）status={resp.status_code}: {(resp.text or '')[:120]}",
                status=resp.status_code,
            )
        return data

    # ------------------------------------------------------------ 建号 / 找回

    def _make_local_part(self) -> str:
        """local-part：密码学随机，长度在配置值附近抖动，避免固定长度指纹。"""
        size = max(6, self.local_part_length + secrets.randbelow(6) - 2)
        body = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(size))
        return f"{self.local_part_prefix}{body}"

    def _local_part_budget(self) -> int:
        """最长 local-part 的字符数（含前缀），用于给域名留长度预算。"""
        return len(self.local_part_prefix) + max(6, self.local_part_length + 3) + 1  # +1 = '@'

    @staticmethod
    def _random_label(limit: int = _LABEL_MAX_LEN) -> str:
        """单个主机名标签：优先 mail/mx/smtp 词根 + 可选数字，退化为短词。

        纯随机串（x9k2m）在地址里是「一次性邮箱」的强特征；词根 + 数字更像企业
        邮件主机（mail2 / mx1 / smtp3 / mx-a）。服务端 domainLabelPattern 允许
        数字开头与连字符：^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$
        """
        if secrets.randbelow(100) < 80:
            root = secrets.choice(_LABEL_ROOTS)
            suffix = secrets.choice(_LABEL_SUFFIXES)
            sep = secrets.choice(("", "", "-")) if suffix and secrets.randbelow(100) < 25 else ""
            label = f"{root}{sep}{suffix}"
        else:
            label = secrets.choice(_LABEL_WORDS)
        # 偶尔再叠一个数字，进一步拉开形状；仍保证合法
        if secrets.randbelow(100) < 35:
            label = f"{label}{secrets.randbelow(100)}"
        label = label[:max(1, limit)].rstrip("-")
        return label or "mail"

    @classmethod
    def _random_labels(cls, depth: int, *, base_len: int = 0, local_len: int = 0) -> str:
        """depth 个主机名标签，字母/数字开头（满足 tempmail 的 domainLabelPattern）。

        层数很深时按剩余长度预算裁剪每个标签，保证整个地址不越过
        RFC 5321 的 254 字符路径上限（见 MAX_DOMAIN_LEN）。
        """
        count = max(0, depth)
        if not count:
            return ""
        budget = cls.MAX_DOMAIN_LEN - (count - 1) - max(0, base_len) - max(0, local_len)
        per_label = max(1, budget // count)
        return ".".join(cls._random_label(min(_LABEL_MAX_LEN, per_label)) for _ in range(count))

    # --------------------------------------------------------- 域名黑名单预检

    @staticmethod
    def _dbl_lookup(name: str) -> bool:
        """name 在 DBL 命中返回 True；解析失败/超时返回 False（不拦路）。"""
        import socket

        try:
            socket.gethostbyname(name)
            return True
        except (socket.gaierror, socket.herror, OSError):
            return False
        except Exception:  # 任何解析层异常都不该拦住建号
            return False

    def domain_is_blocklisted(self, base_domain: str) -> bool:
        """主域是否被 Spamhaus DBL / NordSpam 收录（子域会继承，只需查主域）。"""
        base = str(base_domain or "").strip().lower().lstrip("*.").rstrip(".")
        if not base or not self.SKIP_BLOCKLISTED_DOMAINS:
            return False
        now = time.time()
        with _DBL_LOCK:
            hit = _DBL_CACHE.get(base)
        if hit and now - hit[0] < _DBL_TTL_SECS:
            return hit[1]
        try:
            blocked = any(self._dbl_lookup(f"{base}.{zone}") for zone in _DBL_ZONES)
        except Exception as exc:
            print(f"[tempmail] DBL 查询 {base} 异常（{type(exc).__name__}），按未命中处理")
            blocked = False
        with _DBL_LOCK:
            _DBL_CACHE[base] = (now, blocked)
        return blocked

    def _first_clean_base(self) -> str:
        """域名池里的干净主域，随机取一个；查不到/全黑时返回空串（调用方回退）。"""
        try:
            rows = self._active_domains()
        except Exception as exc:  # 域名查询失败不能连累建号
            print(f"[tempmail] 域名列表查询失败（{type(exc).__name__}），跳过黑名单预检")
            return ""
        candidates: list[str] = []
        for row in rows:
            base = str(row.get("base_domain") or row.get("domain") or "").strip().lower().lstrip("*.")
            if base and base not in candidates:
                candidates.append(base)
        clean = [base for base in candidates if not self.domain_is_blocklisted(base)]
        if clean:
            return secrets.choice(clean)
        if candidates:
            print(f"[tempmail] 警告：域名池全部命中黑名单（{', '.join(candidates)}），仍按原域名建号")
        return ""

    def _active_domains(self, *, refresh: bool = False) -> list[dict]:
        cache_key = (self.base_url, self._key_fp)
        with _DOMAINS_LOCK:
            hit = _DOMAINS_CACHE.get(cache_key)
        if hit and not refresh and time.time() - hit[0] < self.DOMAINS_CACHE_TTL_SECS:
            return hit[1]
        data = self._request("GET", "domains")
        rows = [row for row in (data.get("domains") or []) if isinstance(row, dict) and row.get("is_active", True)]
        with _DOMAINS_LOCK:
            _DOMAINS_CACHE[cache_key] = (time.time(), rows)
        return rows

    def _split_host(self, host: str) -> tuple[str, str] | None:
        """host → (主域, 子域名前缀)；不属于任何已激活域名返回 None。缓存没命中会刷新一次。"""
        for refresh in (False, True):
            best = ""
            for row in self._active_domains(refresh=refresh):
                base = str(row.get("base_domain") or row.get("domain") or "").strip().lower().lstrip("*.")
                if base and (host == base or host.endswith("." + base)) and len(base) > len(best):
                    best = base
            if best:
                return best, host[: -len(best)].rstrip(".")
        return None

    def _target_body(self) -> dict:
        """建号请求里除 address 以外的部分（domain / mode / subdomain）。"""
        wildcard = self.domain.startswith("*.")
        host = self.domain[2:] if wildcard else self.domain
        if not host:
            # 没指定域名：由 tempmail 挑。multi 时自定义子域名会拼到它挑的主域上。
            # 但服务端可能挑到黑名单主域（如 cyt233.dpdns.org），所以先自己挑一个干净的。
            clean = self._first_clean_base()
            body = {"mode": self.mode} if self.mode else {}
            if clean:
                body["domain"] = clean
            if self.mode == "multi" and self.subdomain_depth:
                body["subdomain"] = self._random_labels(self.subdomain_depth,
                                                        local_len=self._local_part_budget())
            return body
        split = self._split_host(host)
        if split is None:
            raise TempMailError(f"域名 {host} 不在这个 tempmail 实例的已激活域名里", status=400)
        base, prefix = split
        if prefix and not wildcard:
            # 固定子域名 a.example.com：所有号共用这个主机名
            return {"domain": base, "mode": "multi", "subdomain": prefix}
        if wildcard or self.mode == "multi":
            # 带固定前缀时不能交给服务端生成（它只会直接挂在主域下），至少自己生成默认层数
            depth = self.subdomain_depth or (self.DEFAULT_SUBDOMAIN_DEPTH if prefix else 0)
            # 主域被 DBL 拉黑时子域同样命中，换池子里干净的主域
            if self.domain_is_blocklisted(base):
                alt = self._first_clean_base()
                if alt and alt != base:
                    print(f"[tempmail] 主域 {base} 命中 DBL（子域会继承），改用 {alt}")
                    base, prefix = alt, ""
            # 前缀要算进长度预算，否则固定前缀 + 深层数会越过 254 字符
            fixed = f".{prefix}" if prefix else ""
            labels = self._random_labels(depth, base_len=len(base) + len(fixed),
                                         local_len=self._local_part_budget())
            subdomain = ".".join(part for part in (labels, prefix) if part)
            body = {"domain": base, "mode": "multi"}
            if subdomain:
                body["subdomain"] = subdomain
            return body
        if self.domain_is_blocklisted(base):
            alt = self._first_clean_base()
            if alt and alt != base:
                print(f"[tempmail] 主域 {base} 命中 DBL，改用 {alt}")
                return {"domain": alt, "mode": self.mode} if self.mode else {"domain": alt}
        return {"domain": base, "mode": self.mode} if self.mode else {"domain": base}

    def _create(self, body: dict) -> dict:
        data = self._request("POST", "mailboxes", json_body=body)
        mailbox = data.get("mailbox")
        if not isinstance(mailbox, dict) or not mailbox.get("id") or not mailbox.get("full_address"):
            raise TempMailError(f"tempmail 建号响应缺少 mailbox: {str(data)[:200]}")
        return mailbox

    def _account_for(self, mailbox: dict, *, fresh: bool) -> MailboxAccount:
        email_addr = str(mailbox["full_address"]).strip().lower()
        mailbox_id = str(mailbox["id"])
        self._ids_by_email[email_addr] = mailbox_id
        if fresh:
            self._fresh.add(mailbox_id)
        return MailboxAccount(
            email=email_addr,
            account_id=mailbox_id,
            extra={
                "tempmail_mailbox_id": mailbox_id,
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": self.PROVIDER_NAME,
                    "resource_type": "mailbox",
                    "resource_identifier": mailbox_id,
                    "handle": email_addr,
                    "display_name": email_addr,
                    "metadata": {
                        "email": email_addr,
                        "domain": email_addr.split("@", 1)[-1],
                        "mailbox_id": mailbox_id,
                        "expires_at": str(mailbox.get("expires_at") or ""),
                    },
                },
            },
        )

    def get_email(self) -> MailboxAccount:
        for attempt in range(self.MAX_CREATE_ATTEMPTS):
            wait = self.cooldown_remaining()
            if wait > 0:
                time.sleep(wait)
            try:
                # 每次重试都重新生成 local-part 和随机子域名
                body = {**self._target_body(), "address": self._make_local_part()}
                mailbox = self._create(body)
            except TempMailError as exc:
                # 0=网络 409=地址撞了 429=限流 5xx=服务端抖动；其余（400 域名/模式不对、
                # 401 Key、503 没有可用域名、非 JSON）是配置问题，重试没用。
                retryable = exc.status in (0, 409, 429) or (exc.status >= 500 and exc.status != 503)
                if not retryable or attempt == self.MAX_CREATE_ATTEMPTS - 1:
                    raise
                if exc.status != 429:
                    time.sleep(0.5 * (attempt + 1))
                continue
            account = self._account_for(mailbox, fresh=True)
            print(f"[tempmail] 建号: {account.email} expires_at={mailbox.get('expires_at') or '-'}")
            return account
        raise TempMailError("tempmail 建号失败")

    def _find(self, email_addr: str) -> dict | None:
        for page in range(1, self.LIST_MAX_PAGES + 1):
            data = self._request("GET", "mailboxes", params={"page": page, "size": self.LIST_PAGE_SIZE})
            rows = [row for row in (data.get("data") or []) if isinstance(row, dict)]
            for row in rows:
                if str(row.get("full_address") or "").strip().lower() == email_addr:
                    return row
            if len(rows) < self.LIST_PAGE_SIZE:
                return None
        return None

    def _recreate_body(self, email_addr: str) -> dict | None:
        """已过期的地址按原 local@host 重建；host 必须属于本 tempmail 的某个域名。"""
        local, _, host = email_addr.partition("@")
        split = self._split_host(host)
        if split is None:
            return None
        base, prefix = split
        if not prefix:
            return {"address": local, "domain": base, "mode": "single"}
        return {"address": local, "domain": base, "mode": "multi", "subdomain": prefix}

    def bind_existing(self, email_addr: str) -> MailboxAccount:
        """绑定一个已注册过的地址（Codex 补 OTP）。过期/快过期的邮箱会删掉按原地址重建。"""
        email_addr = str(email_addr or "").strip().lower()
        found = self._find(email_addr)
        if found is not None:
            expires = _parse_iso(found.get("expires_at"))
            server_now = time.time() + self._clock_offset
            if expires is None or expires - server_now >= self.MIN_REMAINING_TTL_SECS:
                return self._account_for(found, fresh=False)
            try:
                self._request("DELETE", f"mailboxes/{found['id']}")
            except TempMailError as exc:
                if exc.status != 404:
                    raise
        body = self._recreate_body(email_addr)
        if body is None:
            raise TempMailError(f"{email_addr} 的域名不在这个 tempmail 实例的域名池里，无法收信", status=404)
        mailbox = self._create(body)
        print(f"[tempmail] 重建过期邮箱: {email_addr}")
        # 重建的邮箱没有旧信，但不是本轮 get_email 建的——仍按真实列表取基线。
        return self._account_for(mailbox, fresh=False)

    def _mailbox_id(self, account: MailboxAccount) -> str:
        extra = getattr(account, "extra", None) or {}
        mailbox_id = str(extra.get("tempmail_mailbox_id") or "").strip()
        if mailbox_id:
            return mailbox_id
        email_addr = str(getattr(account, "email", "") or "").strip().lower()
        if email_addr in self._ids_by_email:
            return self._ids_by_email[email_addr]
        return str(self.bind_existing(email_addr).account_id)

    # ------------------------------------------------------------------ 收信

    def _list_emails(self, mailbox_id: str) -> list[dict]:
        data = self._request("GET", f"mailboxes/{mailbox_id}/emails", params={"page": 1, "size": 20})
        return [row for row in (data.get("data") or []) if isinstance(row, dict)]

    def _message_text(self, mailbox_id: str, summary: dict) -> str:
        """拼成 _extract_code_from_raw 认识的「头 + 空行 + 正文」形状（正文用服务端已解码的）。"""
        try:
            data = self._request("GET", f"mailboxes/{mailbox_id}/emails/{summary['id']}")
        except TempMailError as exc:
            if exc.status == 404 and "email" in exc.detail and "mailbox" not in exc.detail:
                return ""  # 这封信被删了；邮箱本身还在
            raise
        detail = data.get("email") if isinstance(data.get("email"), dict) else {}
        body = "\n".join(
            part for part in (str(detail.get("body_text") or ""), str(detail.get("body_html") or "")) if part.strip()
        ) or str(detail.get("raw_message") or "")
        return (
            f"From: {detail.get('sender') or summary.get('sender') or ''}\r\n"
            f"Subject: {detail.get('subject') or summary.get('subject') or ''}\r\n\r\n{body}"
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            mailbox_id = self._mailbox_id(account)
            if mailbox_id in self._fresh:
                return set()
            return {str(row["id"]) for row in self._list_emails(mailbox_id) if row.get("id")}
        except Exception as exc:
            logger.warning("[tempmail] get_current_ids failed: %s", exc)
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        otp_sent_at: float | None = None,
        min_received_at: float | None = None,
        poll_interval: float = 2.0,
    ) -> str:
        email_addr = account.email
        start = time.time()
        min_ts = otp_sent_at if otp_sent_at is not None else min_received_at
        if min_ts is not None:
            try:
                min_ts = float(min_ts) - 15.0
            except (TypeError, ValueError):
                min_ts = None
        mailbox_id = ""
        consumed: set[str] = set()
        seen = {str(x) for x in (before_ids or set()) if x not in (None, "")}
        print(
            f"[tempmail] 等待验证码: {email_addr} timeout={timeout}s "
            f"before_ids={len(seen)} min_ts={int(min_ts) if min_ts else '-'}"
        )
        last_err = ""
        rebound = False
        while time.time() - start < timeout:
            try:
                if not mailbox_id:
                    mailbox_id = self._mailbox_id(account)
                    self._fresh.discard(mailbox_id)
                    consumed = self._consumed.setdefault(mailbox_id, set())
                    seen |= consumed
                for summary in self._list_emails(mailbox_id):  # received_at DESC：新信优先
                    mid = str(summary.get("id") or "")
                    if not mid or mid in seen:
                        continue
                    received = _parse_iso(summary.get("received_at"))
                    if min_ts is not None and received is not None \
                            and received - self._clock_offset < min_ts:
                        seen.add(mid)
                        continue
                    head = f"From: {summary.get('sender') or ''}\r\nSubject: {summary.get('subject') or ''}\r\n\r\n"
                    code = ""
                    if not keyword or keyword.lower() in head.lower():
                        code = CloudflareD1Mailbox._extract_code_from_raw(head, code_pattern=code_pattern)
                    if not code:
                        # 正文请求失败会抛出，本封不进 seen，下轮重试
                        text = self._message_text(mailbox_id, summary)
                        if not keyword or keyword.lower() in text.lower():
                            code = CloudflareD1Mailbox._extract_code_from_raw(text, code_pattern=code_pattern)
                    seen.add(mid)
                    if code:
                        consumed.add(mid)
                        print("[tempmail] 验证码已收到")
                        return code
            except TempMailError as exc:
                last_err = str(exc)[:160]
                if exc.status in (401, 403):
                    raise
                if exc.status == 404 and mailbox_id and not rebound:
                    # 邮箱被服务端 TTL 清掉了：按原地址重建一次，继续等
                    rebound = True
                    self._ids_by_email.pop(str(email_addr).lower(), None)
                    if isinstance(getattr(account, "extra", None), dict):
                        account.extra.pop("tempmail_mailbox_id", None)
                    mailbox_id = ""
                    continue
                if exc.status != 429:
                    logger.warning("[tempmail] poll failed: %s", exc)
            except Exception as exc:
                last_err = str(exc)[:160]
                logger.warning("[tempmail] poll failed: %s", exc)
            elapsed = time.time() - start
            # 验证码一般几秒内到：前 15s 每秒一次，之后放慢，给限流留余量
            sleep_s = 1.0 if elapsed < 15 else max(1.0, float(poll_interval))
            cooldown = self.cooldown_remaining()
            if cooldown > 0:
                sleep_s = max(sleep_s, cooldown)
            time.sleep(max(0.0, min(sleep_s, timeout - elapsed)))
        suffix = f" last_err={last_err}" if last_err else ""
        raise TimeoutError(f"等待验证码超时 ({timeout}s) email={email_addr}{suffix}")
