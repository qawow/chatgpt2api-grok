"""按出口 IP 地理动态解析客户端环境画像（时区 / 语言 / 日期格式）。

生图与对话链路里所有「客户端环境」字段（payload 时区、PoW config 的日期与语言）
都从这里取，随出口环境变化；环境变量可强制覆盖，检测失败回退默认。

检测用 ip-api.com（免费档），结果按代理出口缓存 6 小时。
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# 浏览器 `""+new Date` 尾部的时区显示名（en-US locale 下 Chrome 的输出）
_TZ_DISPLAY = {
    "Asia/Tokyo": "Japan Standard Time",
    "Asia/Shanghai": "China Standard Time",
    "Asia/Hong_Kong": "Hong Kong Standard Time",
    "Asia/Singapore": "Singapore Standard Time",
    "Asia/Seoul": "Korea Standard Time",
    "Asia/Taipei": "Taipei Standard Time",
    "America/Los_Angeles": "Pacific Standard Time",
    "America/Denver": "Mountain Standard Time",
    "America/Chicago": "Central Standard Time",
    "America/New_York": "Eastern Standard Time",
    "America/Sao_Paulo": "E. South America Standard Time",
    "Europe/London": "Greenwich Mean Time",
    "Europe/Paris": "Central European Standard Time",
    "Europe/Berlin": "Central European Standard Time",
    "Australia/Sydney": "Australian Eastern Standard Time",
}

# 国家 → navigator.language / navigator.languages
_COUNTRY_LANG = {
    "JP": ("ja-JP", "ja-JP,ja,en-US,en"),
    "CN": ("zh-CN", "zh-CN,zh,en-US,en"),
    "HK": ("zh-HK", "zh-HK,zh,en-US,en"),
    "TW": ("zh-TW", "zh-TW,zh,en-US,en"),
    "KR": ("ko-KR", "ko-KR,ko,en-US,en"),
    "SG": ("en-SG", "en-SG,en-US,en"),
    "US": ("en-US", "en-US,en"),
    "GB": ("en-GB", "en-GB,en-US,en"),
    "AU": ("en-AU", "en-AU,en-US,en"),
    "DE": ("de-DE", "de-DE,de,en-US,en"),
    "FR": ("fr-FR", "fr-FR,fr,en-US,en"),
}
_DEFAULT_LANG = ("en-US", "en-US,en")

_DEFAULT_TIMEZONE = "Asia/Tokyo"  # 当前部署出口在东京
_CACHE_TTL = 6 * 3600

_cache: dict[str, tuple[float, "EgressLocale"]] = {}
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class EgressLocale:
    timezone: str      # IANA 名，如 Asia/Tokyo
    offset_min: int    # UTC 偏移（分钟，含符号），东京 = -540
    gmt_offset: str    # "+0900"
    tz_display: str    # "Japan Standard Time"
    language: str      # navigator.language
    languages: str     # navigator.languages.join(",")
    source: str        # detected | env | default

    @property
    def date_suffix(self) -> str:
        """`""+new Date` 的尾部，如 ' GMT+0900 (Japan Standard Time)'。"""
        return f" GMT{self.gmt_offset} ({self.tz_display})"

    def format_browser_date(self, now: datetime | None = None) -> str:
        """浏览器 `""+new Date` 完整格式。"""
        now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(self.timezone))
        return now.strftime("%a %b %d %Y %H:%M:%S") + self.date_suffix


def _offset_parts(tz_name: str) -> tuple[int, str]:
    off = ZoneInfo(tz_name).utcoffset(datetime.now(timezone.utc)) or timedelta(0)
    total = -int(off.total_seconds() // 60)  # payload 里东京为 -540
    mins = abs(int(off.total_seconds() // 60))
    sign = "+" if off >= timedelta(0) else "-"
    return total, f"{sign}{mins // 60:02d}{mins % 60:02d}"


def _build(tz_name: str, country: str, source: str) -> EgressLocale:
    offset_min, gmt = _offset_parts(tz_name)
    lang, langs = _COUNTRY_LANG.get(country, _DEFAULT_LANG)
    return EgressLocale(
        timezone=tz_name,
        offset_min=offset_min,
        gmt_offset=gmt,
        tz_display=_TZ_DISPLAY.get(tz_name, f"{tz_name.split('/')[-1].replace('_', ' ')} Standard Time"),
        language=lang,
        languages=langs,
        source=source,
    )


def default_locale() -> EgressLocale:
    """纯本地兜底，不做任何网络请求。"""
    env_tz = str(os.environ.get("OAI_CLIENT_TIMEZONE") or "").strip()
    if env_tz:
        return _build(env_tz, str(os.environ.get("OAI_CLIENT_COUNTRY") or "").strip().upper(), "env")
    return _build(_DEFAULT_TIMEZONE, "JP", "default")


def _detect(proxy_url: str | None) -> EgressLocale | None:
    """经指定代理出口探测地理（无代理则探直连出口）。"""
    try:
        from curl_cffi import requests as curl_requests
        kwargs: dict = {"timeout": 8}
        if proxy_url:
            kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
        r = curl_requests.get(
            "http://ip-api.com/json/?fields=status,countryCode,timezone",
            **kwargs,
        )
        data = r.json()
        if data.get("status") == "success" and data.get("timezone"):
            return _build(str(data["timezone"]), str(data.get("countryCode") or ""), "detected")
    except Exception:
        return None
    return None


def resolve_egress_locale(proxy_url: str | None = None, *, force: bool = False) -> EgressLocale:
    """解析当前出口环境的客户端画像。环境变量优先，其次探测（带缓存），最后兜底。"""
    env_tz = str(os.environ.get("OAI_CLIENT_TIMEZONE") or "").strip()
    if env_tz:
        return _build(env_tz, str(os.environ.get("OAI_CLIENT_COUNTRY") or "").strip().upper(), "env")
    key = proxy_url or "<direct>"
    now = time.time()
    if not force:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    locale = _detect(proxy_url) or default_locale()
    with _cache_lock:
        _cache[key] = (now, locale)
    return locale
