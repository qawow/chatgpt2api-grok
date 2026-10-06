"""Shared log redaction for structured events, stored history, and console output."""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"
_URL = re.compile(r"\b(?:https?|socks5h?|socks)://[^\s<>\"']+", re.I)
_BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.I)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*\.[A-Za-z0-9_.-]+")
_QUOTED_SECRET = re.compile(
    r"(?i)(\b(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"session[_-]?token|id[_-]?token|client[_-]?secret|authorization|proxy-authorization|"
    r"cf_clearance|cookie|set-cookie|token|code[_-]?verifier|totp[_-]?secret|mfa[_-]?secret|"
    r"__Secure-next-auth\.session-token)(?:\.\d+)?[\"']?\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
)
_SECRET_PAIR = re.compile(
    r"(?i)(\b(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"session[_-]?token|id[_-]?token|client[_-]?secret|authorization|proxy-authorization|"
    r"cf_clearance|token|code[_-]?verifier|totp[_-]?secret|mfa[_-]?secret|"
    r"__Secure-next-auth\.session-token)(?:\.\d+)?(?:\"|')?\s*[:=]\s*(?:\"|')?)"
    r"(\[REDACTED\]|[^\s,;\"'&}\]]+)"
)
_QUERY_SECRET = re.compile(
    r"([?&](?:api_key|access_token|refresh_token|code_verifier|code|verifier|state|"
    r"key|token|password|secret)=)[^&#\s\"']+",
    re.I,
)
_COOKIE_HEADER = re.compile(r"(?im)\b((?:set-cookie|cookie)\s*[:=]\s*)([^\r\n]+)")
_OTP = re.compile(r"((?:\bOTP|verification[_ ]code|\u9a8c\u8bc1\u7801)\s*[:=\uff1a]?\s*)\d{4,10}\b", re.I)
_PASSWORD_LABEL = re.compile(r"(\u5bc6\u7801(?:\[[^\]\r\n]*\])?\s*[:=\uff1a]\s*)\S+")
_SECRETS = frozenset({
    "password", "passwd", "pwd", "secret", "apikey", "clientsecret", "authorization",
    "proxyauthorization", "cookie", "cookies", "setcookie", "cfclearance", "cfcookies",
    "accesstoken", "refreshtoken", "sessiontoken", "idtoken", "token", "oldtoken", "newtoken",
    "tempmailapikey", "chatgpt2apiauthkey", "totpsecret", "mfasecret", "otp", "verificationcode", "codeverifier",
})
_TOKEN_ALIAS = re.compile(r"token:[a-f0-9]{10,64}\Z")


def _mask_url(match: re.Match[str]) -> str:
    value = match.group(0)
    scheme, rest = value.split("://", 1)
    # Userinfo ends at the LAST @ before a path/query, including old raw-@ passwords.
    authority_end = min((rest.find(c) for c in "/?#" if c in rest), default=len(rest))
    authority, tail = rest[:authority_end], rest[authority_end:]
    if "@" in authority:
        authority = REDACTED + "@" + authority.rsplit("@", 1)[1]
    return f"{scheme}://{authority}{tail}"


def redact_text(text: object, *, limit: int | None = None) -> str:
    value = _URL.sub(_mask_url, str(text or ""))
    value = _BEARER.sub(lambda m: m.group(1) + " " + REDACTED, value)
    value = _JWT.sub(REDACTED, value)
    value = _QUOTED_SECRET.sub(lambda m: m.group(1) + REDACTED, value)
    value = _SECRET_PAIR.sub(
        lambda m: m.group(0) if _TOKEN_ALIAS.fullmatch(m.group(0)) else m.group(1) + REDACTED,
        value,
    )
    value = _QUERY_SECRET.sub(lambda m: m.group(1) + REDACTED, value)
    value = _COOKIE_HEADER.sub(lambda m: m.group(1) + REDACTED, value)
    value = _OTP.sub(lambda m: m.group(1) + REDACTED, value)
    value = _PASSWORD_LABEL.sub(lambda m: m.group(1) + REDACTED, value)
    if limit is not None and len(value) > max(0, limit):
        value = value[:max(0, limit - 3)] + ("..." if limit >= 3 else "")
    return value


def _error_excerpt(text: str) -> str:
    match = re.search(r"<!doctype\s+html|<html(?:\s|>)", text, re.I)
    if match:
        text = text[:match.start()].rstrip() + " [HTML response omitted]"
    return redact_text(text, limit=1600)


def sanitize_log_value(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in _SECRETS and item:
                result[key] = item if isinstance(item, str) and _TOKEN_ALIAS.fullmatch(item) else REDACTED
            elif normalized in {"error", "errormessage", "exception", "lasterror", "lastrefresherror", "lasttokenrefresherror"} and isinstance(item, str):
                result[key] = _error_excerpt(item)
            elif normalized == "errors" and isinstance(item, list):
                result[key] = [_error_excerpt(v) if isinstance(v, str) else sanitize_log_value(v) for v in item]
            elif normalized in {"proxy", "proxyurl", "resourceproxyurl"} and isinstance(item, str) and "://" not in item:
                parts = item.split(":", 3)
                result[key] = f"http://{REDACTED}@{parts[0]}:{parts[1]}" if len(parts) == 4 and parts[1].isdigit() else redact_text(item)
            else:
                result[key] = sanitize_log_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize_log_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
