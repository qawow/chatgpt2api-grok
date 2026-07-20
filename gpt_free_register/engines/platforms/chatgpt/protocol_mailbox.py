"""ChatGPT 协议邮箱注册 worker。"""
from __future__ import annotations

from typing import Callable

from platforms.chatgpt.register import RegistrationEngine


class _MailboxEmailService:
    def __init__(self, *, mailbox, mailbox_account, provider: str):
        self.service_type = type("ST", (), {"value": provider})()
        self._mailbox = mailbox
        self._mailbox_account = mailbox_account
        self._acct = None
        self._baseline_ids: set[str] = set()
        self._baseline_ready = False

    def create_email(self, config=None):
        self._acct = self._mailbox_account
        # Snapshot existing mail ids for THIS address only.
        # Empty is valid for a freshly generated address — do NOT re-snapshot later,
        # or a fast-arriving OTP would be marked as "already seen".
        try:
            if hasattr(self._mailbox, "get_current_ids"):
                self._baseline_ids = {
                    str(x)
                    for x in (self._mailbox.get_current_ids(self._mailbox_account) or set())
                    if x not in (None, "")
                }
            else:
                self._baseline_ids = set()
        except Exception:
            self._baseline_ids = set()
        self._baseline_ready = True
        return {
            "email": self._mailbox_account.email,
            "service_id": getattr(self._mailbox_account, "account_id", ""),
            "token": getattr(self._mailbox_account, "account_id", ""),
        }

    def get_verification_code(
        self,
        email=None,
        email_id=None,
        timeout=300,
        pattern=None,
        otp_sent_at=None,
    ):
        acct = self._acct or self._mailbox_account
        if not self._baseline_ready:
            # Defensive: if create_email was skipped, snapshot once now.
            try:
                if hasattr(self._mailbox, "get_current_ids"):
                    self._baseline_ids = {
                        str(x)
                        for x in (self._mailbox.get_current_ids(acct) or set())
                        if x not in (None, "")
                    }
            except Exception:
                self._baseline_ids = set()
            self._baseline_ready = True

        before_ids = set(self._baseline_ids)
        kwargs = {
            "keyword": "",
            "timeout": timeout,
            "before_ids": before_ids,
            "code_pattern": pattern,
        }
        try:
            return self._mailbox.wait_for_code(
                acct,
                otp_sent_at=otp_sent_at,
                min_received_at=otp_sent_at,
                **kwargs,
            )
        except TypeError:
            return self._mailbox.wait_for_code(acct, **kwargs)

    def update_status(self, success, error=None):
        return None

    @property
    def status(self):
        return None


class ChatGPTProtocolMailboxWorker:
    def __init__(
        self,
        *,
        mailbox,
        mailbox_account,
        provider: str,
        proxy_url: str | None = None,
        log_fn: Callable[[str], None] = print,
    ):
        if not mailbox or not mailbox_account:
            raise ValueError("ChatGPT 注册流程依赖 mailbox provider，当前未获取到邮箱账号")
        email_service = _MailboxEmailService(
            mailbox=mailbox,
            mailbox_account=mailbox_account,
            provider=provider,
        )
        self.engine = RegistrationEngine(
            email_service=email_service,
            proxy_url=proxy_url,
            callback_logger=log_fn,
        )

    def run(self, *, email: str, password: str):
        self.engine.email = email
        self.engine.password = password
        result = self.engine.run()
        if not result or not result.success:
            raise RuntimeError(result.error_message if result else "注册失败")
        return result
