#!/usr/bin/env python3
"""Live, sanitized capture of the ChatGPT Web register protocol.

Walks warmup → CSRF/signin → authorize → sentinel → authorize/continue
through the project's RegistrationEngine (same TLS/headers as production).
Stops before OTP / create_account unless --full.

Usage:
  REGISTER_PROXY='socks5h://user:pass@host:port' \\
    .venv/bin/python scripts/capture_openai_register_protocol.py

  # complete one register (needs CFD1 in data/gpt_register.env):
  REGISTER_PROXY='...' .venv/bin/python scripts/capture_openai_register_protocol.py --full

Output (gitignored):
  data/gpt_register_logs/protocol_capture_<ts>.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
ENGINES = ROOT / "gpt_free_register" / "engines"
DATA = ROOT / "data"
OUT_DIR = DATA / "gpt_register_logs"

sys.path.insert(0, str(ENGINES))
sys.path.insert(0, str(ROOT))

REDACT_HEADER = {
    "cookie",
    "set-cookie",
    "authorization",
    "openai-sentinel-token",
    "openai-sentinel-so-token",
    "x-datadog-trace-id",
    "x-datadog-parent-id",
    "traceparent",
    "tracestate",
}
KEEP_JSON_KEYS = {
    "page",
    "continue_url",
    "oai-client-auth-session",
    "error",
    "token",
    "proofofwork",
    "turnstile",
    "so",
    "so_token",
    "csrfToken",
    "url",
    "email_verification_mode",
    "signup_mode",
    "original_screen_hint",
    "type",
    "payload",
    "required",
    "difficulty",
    "seed",
    "dx",
}


def _strip_userinfo(url: str) -> str:
    try:
        parts = urlsplit(url)
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            netloc = host
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        pass
    return re.sub(r"://[^/@]+@", "://***@", url)


def _clip(value: Any, n: int = 80) -> str:
    text = str(value or "")
    if len(text) <= n:
        return text
    return text[:n] + f"...<{len(text)}>"


def _sanitize_headers(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not headers:
        return out
    items = headers.items() if hasattr(headers, "items") else []
    for key, val in items:
        name = str(key)
        low = name.lower()
        raw = str(val or "")
        if low in REDACT_HEADER or low.startswith("oai-"):
            out[name] = _clip(raw, 24)
        else:
            out[name] = _clip(raw, 160)
    return out


def _summarize_json(text: str, limit: int = 4000) -> Any:
    try:
        data = json.loads(text)
    except Exception:
        return {"_non_json": _clip(text, 240)}

    def walk(node: Any, depth: int = 0) -> Any:
        if isinstance(node, dict):
            slim: dict[str, Any] = {}
            for k, v in node.items():
                key = str(k)
                low = key.lower()
                if low in {"csrftoken", "accesstoken", "access_token", "refresh_token", "id_token", "sessiontoken", "code"}:
                    slim[key] = {"len": len(str(v or "")), "redacted": True}
                elif low in {"email", "value"} and "@" in str(v or ""):
                    slim[key] = "***@" + str(v).split("@", 1)[-1]
                elif low in {"dx", "token", "so", "so_token", "p", "t", "c", "seed"}:
                    slim[key] = {
                        "len": len(str(v or "")),
                        "prefix": _clip(v, 16),
                        "required_neighbor": node.get("required") if isinstance(node.get("required"), bool) else None,
                    }
                elif low in KEEP_JSON_KEYS or depth < 3:
                    slim[key] = walk(v, depth + 1)
                else:
                    slim[key] = type(v).__name__
            return slim
        if isinstance(node, list):
            return [walk(node[0], depth + 1)] if node else []
        if isinstance(node, str) and len(node) > 180:
            return _clip(node, 80)
        return node

    slim = walk(data)
    blob = json.dumps(slim, ensure_ascii=False)
    if len(blob) > limit:
        return {"_truncated": blob[:limit], "orig_keys": list(data)[:20] if isinstance(data, dict) else type(data).__name__}
    return slim


def _cookie_names(session: Any) -> list[str]:
    try:
        jar = session.cookies.jar
        return sorted({c.name for c in jar})
    except Exception:
        try:
            return sorted(session.cookies.keys())
        except Exception:
            return []


class Tracer:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def install(self, session: Any) -> None:
        inner = getattr(session, "_inner", session)
        orig = inner.request

        def wrapped(method: str, url: str, **kwargs: Any):
            started = time.time()
            resp = orig(method, url, **kwargs)
            body = kwargs.get("data")
            if body is None:
                body = kwargs.get("json")
            body_s = body if isinstance(body, str) else (json.dumps(body, ensure_ascii=False) if body is not None else "")
            rec = {
                "t": round(time.time() - started, 3),
                "method": str(method).upper(),
                "url": _strip_userinfo(str(url)),
                "req_headers": _sanitize_headers(kwargs.get("headers")),
                "req_body": _summarize_json(body_s) if body_s and str(body_s).lstrip()[:1] in "{[" else _clip(body_s, 200),
                "status": getattr(resp, "status_code", None),
                "resp_headers": _sanitize_headers(getattr(resp, "headers", None)),
                "resp": _summarize_json(getattr(resp, "text", "") or ""),
                "final_url": _strip_userinfo(str(getattr(resp, "url", "") or "")),
                "cookies_after": _cookie_names(session),
            }
            self.events.append(rec)
            return resp

        inner.request = wrapped


class DummyMailbox:
    service_type = type("T", (), {"value": "dummy_probe"})()

    def __init__(self, email: str) -> None:
        self._email = email

    def create_email(self) -> dict[str, str]:
        return {"email": self._email}


def _bootstrap() -> None:
    from gpt_free_register.runner import _bootstrap as boot, default_engines_dir

    boot(Path(default_engines_dir()))


def _redact_logs(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        text = str(line)
        text = re.sub(r"(验证码[:：]?\s*)(\d{6})", r"\1******", text)
        text = re.sub(r"(code(?: is)?[:：]?\s*)(\d{6})", r"\1******", text, flags=re.I)
        out.append(text)
    return out


def _event_index(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for ev in events:
        url = str(ev.get("url") or "")
        path = urlsplit(url).path
        rows.append(
            {
                "method": ev.get("method"),
                "path": path,
                "status": ev.get("status"),
                "t": ev.get("t"),
                "final_path": urlsplit(str(ev.get("final_url") or "")).path,
            }
        )
    return rows


def capture_prefix(*, proxy: str, email: str | None) -> tuple[Path, dict[str, Any]]:
    from platforms.chatgpt.register import RegistrationEngine

    probe_email = email or f"protocol-probe-{uuid.uuid4().hex[:10]}@cyt233.dpdns.org"
    engine = RegistrationEngine(
        email_service=DummyMailbox(probe_email),
        proxy_url=proxy,
        callback_logger=lambda m: print(m, flush=True),
    )
    tracer = Tracer()
    if not engine._init_session():
        raise RuntimeError("init session failed")
    tracer.install(engine.session)
    engine.email = probe_email
    engine.email_info = {"email": probe_email}

    notes: list[str] = []
    ip_ok, location = engine._check_ip_location()
    notes.append(f"ip_ok={ip_ok} location={location}")
    if not engine._warmup_chatgpt_home():
        notes.append("warmup failed: oai-did missing")
    else:
        notes.append("warmup ok")
    if not engine._start_oauth():
        notes.append("oauth/signin failed")
    else:
        notes.append("oauth ok")
        did = engine._get_device_id()
        notes.append(f"did={_clip(did, 18)}")
        if did:
            sen = None
            if engine._should_skip_authorize_continue():
                notes.append("sentinel=skipped (auto-OTP)")
            else:
                sen = engine._check_sentinel(did, flow="authorize_continue")
                notes.append("sentinel=" + ("ok" if sen else "fail"))
            signup = engine._submit_signup_form(did, sen)
            notes.append(
                f"continue success={signup.success} page={signup.page_type} "
                f"err={_clip(signup.error_message, 160)}"
            )
            notes.append(
                f"passwordless={getattr(engine, '_is_passwordless_signup', None)} "
                f"force_password={getattr(engine, '_force_password_path', None)} "
                f"existing={getattr(engine, '_is_existing_account', None)} "
                f"mode={getattr(engine, '_email_verification_mode', None)} "
                f"auto_otp={getattr(engine, '_otp_auto_sent', None)}"
            )
    payload = {
        "mode": "prefix",
        "proxy": _strip_userinfo(proxy),
        "email_domain": probe_email.split("@", 1)[-1],
        "notes": notes,
        "engine_logs": _redact_logs(engine.logs),
        "index": _event_index(tracer.events),
        "events": tracer.events,
    }
    return _write_capture(payload), payload


def capture_full(*, proxy: str) -> tuple[Path, dict[str, Any]]:
    from gpt_free_register import register_chatgpt_once
    from platforms.chatgpt.register import RegistrationEngine

    tracer = Tracer()
    orig_init = RegistrationEngine._init_session

    def hooked(self, *args, **kwargs):
        ok = orig_init(self, *args, **kwargs)
        sess = getattr(self, "session", None)
        if ok and sess is not None:
            tracer.install(sess)
        return ok

    RegistrationEngine._init_session = hooked  # type: ignore[method-assign]
    try:
        result = register_chatgpt_once(
            settings={
                "mail_provider": "cloudflare_d1_api",
                "executor": "protocol",
                "proxy": proxy,
                "push_enabled": False,
                "skip_codex": True,
                "register_no_delay": True,
            },
            log=print,
        )
    finally:
        RegistrationEngine._init_session = orig_init  # type: ignore[method-assign]

    extra = result.get("extra") if isinstance(result.get("extra"), dict) else {}
    payload = {
        "mode": "full",
        "proxy": _strip_userinfo(proxy),
        "email_domain": str(result.get("email") or "").split("@")[-1],
        "result": {
            "status": result.get("status"),
            "error": result.get("error") or extra.get("error"),
            "has_token": bool(result.get("token")),
            "has_refresh": bool(extra.get("refresh_token")),
            "has_session": bool(extra.get("session_token")),
            "user_id": bool(result.get("user_id")),
        },
        "notes": [
            f"status={result.get('status')}",
            f"error={_clip(result.get('error') or extra.get('error'), 200)}",
            f"events={len(tracer.events)}",
        ],
        "index": _event_index(tracer.events),
        "events": tracer.events,
    }
    return _write_capture(payload), payload


def _write_capture(payload: dict[str, Any]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    payload = dict(payload)
    payload["captured_at"] = ts
    path = OUT_DIR / f"protocol_capture_{payload.get('mode', 'run')}_{ts}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="also run one complete in-process register")
    parser.add_argument("--email", default="", help="override probe email (default fake local-part on CFD1 domain)")
    args = parser.parse_args()
    proxy = (os.environ.get("REGISTER_PROXY") or os.environ.get("REGISTER_PROXY_DEFAULT") or "").strip()
    if not proxy:
        print("REGISTER_PROXY is required", file=sys.stderr)
        return 2
    _bootstrap()
    os.environ.setdefault("OPENAI_SKIP_CODEX", "1")
    os.environ.setdefault("OPENAI_REGISTER_NO_DELAY", "1")
    if args.full:
        path, payload = capture_full(proxy=proxy)
    else:
        path, payload = capture_prefix(proxy=proxy, email=args.email.strip() or None)
    print(f"\nWROTE {path}")
    print("index:")
    for row in payload.get("index") or []:
        print(f"  {row.get('method'):4} {row.get('status')} {row.get('path')} -> {row.get('final_path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
