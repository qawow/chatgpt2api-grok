"""Bounded proxy observations, not a guarantee of upstream account usability.

Only explicit diagnostics issue network requests. Callers choose their own proxy;
this module does not rotate credentials, create sessions, or change pool routing.
Legacy username-only cache keys are ignored because the gateway was lost.
"""
from __future__ import annotations

from contextlib import contextmanager
import ipaddress
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from services.config import DATA_DIR
from utils.atomic import atomic_write_json, secure_file_mode
from utils.curl_tls import create_cffi_session
from utils.log_safety import redact_text
from utils.network_diagnostics import error_details, proxy_identity, response_diagnostics

DATA_PATH = Path(os.environ.get("EGRESS_REPUTATION_PATH") or DATA_DIR / "egress_reputation.json")
_LOCK = threading.RLock()
STATE_CLEAR = "clear"
STATE_CF_BLOCKED = "cf_blocked"
STATE_TRANSPORT = "transport"
STATE_UNKNOWN = "unknown"
OBSERVATION_TTL_SECS = 15 * 60
MAX_ENTRIES = 1024
MAX_SAMPLES = 32


def _timeout(value: object, default: float = 8.0) -> float:
    try:
        result = float(value)
        if math.isfinite(result) and result > 0:
            return min(result, 60.0)
    except (ValueError, TypeError, OverflowError):
        pass
    return default


PROBE_TIMEOUT_SECS = _timeout(os.environ.get("EGRESS_PROBE_TIMEOUT_SECS"))


def _key(proxy: str) -> str:
    return proxy_identity(proxy)["proxy_id"]


def _canonical_proxy(proxy: str) -> str:
    """Match proxy_identity's scheme normalization: SOCKS always resolves remotely."""
    text = str(proxy or "").strip()
    lowered = text.lower()
    for prefix in ("socks://", "socks5://"):
        if lowered.startswith(prefix):
            return "socks5h://" + text[len(prefix):]
    return text


def classify_response(status: int, body: str) -> str:
    kind = response_diagnostics(status, body)["failure_kind"]
    return {"none": STATE_CLEAR, "challenge": STATE_CF_BLOCKED}.get(kind, kind)


def probe_exit(proxy: str, *, timeout: float | None = None) -> dict[str, Any]:
    """Read one trace through the supplied gateway; no account credentials used.

    A trace response only proves connectivity to that endpoint, not permission to
    use an authenticated API. Invalid/missing proxy input never falls back direct.
    """
    identity = proxy_identity(proxy)
    out: dict[str, Any] = {
        **identity, "state": STATE_UNKNOWN, "ip": "", "colo": "",
        "status": 0, "secs": 0.0, "target_host": "chatgpt.com",
        "probe_kind": "trace_only", "reachable": False, "ok": False,
    }
    if identity["scheme"] in {"invalid", "direct"}:
        return {**out, "failure_kind": "configuration", "error": "explicit proxy URL required"}
    budget = _timeout(timeout, PROBE_TIMEOUT_SECS)
    started = time.monotonic()
    session = None
    try:
        session = create_cffi_session(proxy=_canonical_proxy(proxy), trust_env=False, verify=True)
        response = session.get(
            "https://chatgpt.com/cdn-cgi/trace", timeout=budget,
            allow_redirects=False,
        )
        body = str(response.text or "")[:8192]
        status = int(response.status_code)
        diagnostic = response_diagnostics(status, body, getattr(response, "headers", {}))
        out.update(diagnostic)
        out.update(status=status, state={"none": STATE_CLEAR, "challenge": STATE_CF_BLOCKED}.get(
            diagnostic["failure_kind"], diagnostic["failure_kind"],
        ))
        if diagnostic["ok"]:
            fields = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)
            try:
                out["ip"] = str(ipaddress.ip_address(fields.get("ip", "")))
            except ValueError:
                out.update(state=STATE_UNKNOWN, ok=False, failure_kind="invalid_trace")
            colo = fields.get("colo", "")
            out["colo"] = colo if re.fullmatch(r"[A-Z]{3}", colo) else ""
    except Exception as exc:
        out.update(
            state=STATE_TRANSPORT, ok=False, error=redact_text(exc, limit=500),
            **error_details(exc),
        )
    finally:
        out["secs"] = round(time.monotonic() - started, 3)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
    return out


@contextmanager
def _locked():
    with _LOCK:
        DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        lock_path = DATA_PATH.with_suffix(DATA_PATH.suffix + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                import fcntl
            except ImportError:  # One-process fallback on Windows.
                fcntl = None
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


class ObservationStoreError(RuntimeError):
    pass


def _load() -> dict[str, Any]:
    try:
        table = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError) as exc:
        raise ObservationStoreError("Proxy observation cache could not be read") from exc
    if not isinstance(table, dict):
        raise ObservationStoreError("Proxy observation cache must contain an object")
    return {
        key: value for key, value in table.items()
        if re.fullmatch(r"proxy:[a-f0-9]{24}", key)
        and isinstance(value, dict) and value.get("schema") == 2
        and isinstance(value.get("samples"), list)
    }


def _save(table: dict[str, Any]) -> None:
    atomic_write_json(DATA_PATH, table, ensure_ascii=True)
    secure_file_mode(DATA_PATH)


def _samples(row: dict[str, Any], now: float, max_age: float) -> list[dict[str, Any]]:
    samples = []
    for sample in row.get("samples", [])[-MAX_SAMPLES:]:
        if not isinstance(sample, dict):
            continue
        try:
            age = now - float(sample.get("at", 0))
        except (TypeError, ValueError, OverflowError):
            continue
        if 0 <= age < max_age and isinstance(sample.get("state"), str):
            samples.append(sample)
    return samples


def record(proxy: str, state: str, *, ip: str = "", colo: str = "", secs: float = 0.0) -> dict[str, Any]:
    identity = proxy_identity(proxy)
    if identity["scheme"] in {"direct", "invalid"}:
        raise ValueError("explicit valid proxy URL required")
    known = {STATE_CLEAR, STATE_CF_BLOCKED, STATE_TRANSPORT, STATE_UNKNOWN,
             "http_auth", "http_forbidden", "rate_limit", "proxy_auth", "upstream",
             "redirect", "unexpected_html", "http_error", "invalid_trace"}
    if state not in known:
        state = STATE_UNKNOWN
    now = time.time()
    with _locked():
        table = _load()
        row = dict(table.get(identity["proxy_id"], {}))
        samples = _samples(row, now, OBSERVATION_TTL_SECS)
        samples = (samples + [{"at": now, "state": state}])[-MAX_SAMPLES:]
        try:
            address = str(ipaddress.ip_address(ip)) if ip else ""
        except ValueError:
            address = ""
        row.update(
            schema=2, **identity, samples=samples, last_state=state,
            last_seen=now, last_secs=round(_timeout(secs, 0.0), 3),
            ip=address, colo=colo if re.fullmatch(r"[A-Z]{3}", str(colo)) else "",
        )
        for field, observed in (("clear", STATE_CLEAR), ("blocked", STATE_CF_BLOCKED), ("transport", STATE_TRANSPORT)):
            row[field] = sum(s["state"] == observed for s in samples)
        table[identity["proxy_id"]] = row
        # Stale failures expire just like successes; no permanent blacklist.
        retained = {}
        for key, value in table.items():
            recent = _samples(value, now, OBSERVATION_TTL_SECS)
            if recent:
                retained[key] = {**value, "samples": recent}
        table = dict(sorted(retained.items(), key=lambda kv: float(kv[1]["samples"][-1]["at"]), reverse=True)[:MAX_ENTRIES])
        _save(table)
        return row


def is_trusted(proxy: str, *, max_age_secs: float = OBSERVATION_TTL_SECS) -> bool:
    """Compatibility name: True only for a recent successful connectivity sample."""
    samples = _samples(_load().get(_key(proxy), {}), time.time(), min(max_age_secs, OBSERVATION_TTL_SECS))
    return bool(samples) and samples[-1]["state"] == STATE_CLEAR


def pick_proxies(candidates: list[str], *, count: int = 3, probe_missing: bool = False,
                 timeout: float | None = None) -> list[str]:
    """Compatibility helper for explicitly supplied proxies, without implicit I/O.

    It preserves caller order and does not treat a 401/403/429 as success.
    No registration or account scheduler calls this helper automatically.
    """
    if count <= 0:
        return []
    picked = []
    for proxy in dict.fromkeys(candidates):
        if is_trusted(proxy):
            picked.append(proxy)
        elif probe_missing:
            result = probe_exit(proxy, timeout=timeout)
            record(proxy, result["state"], ip=result.get("ip", ""), colo=result.get("colo", ""), secs=result.get("secs", 0.0))
            if result["state"] == STATE_CLEAR:
                picked.append(proxy)
        if len(picked) >= count:
            break
    return picked
