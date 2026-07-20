"""Browser fingerprint profile for ChatGPT protocol registration.

Rules:
1. One registration account => one independent profile.
2. Within one account, TLS impersonate / HTTP headers / Sentinel p-token /
   Sentinel VM navigator must stay coherent (no Mac TLS + Windows UA).
3. Across accounts, profiles should differ (platform, chrome build, screen,
   hardware, language, timezone offset noise, etc.).
4. Client Hints (sec-ch-ua*) must match the chosen Chrome major and platform.
"""
from __future__ import annotations

import os
import random
import re
import secrets
import uuid
from typing import Any


DEFAULT_IMPERSONATE = "chrome142"
DEFAULT_CHROME_MAJOR = "142"
DEFAULT_PLATFORM = "mac"  # mac | windows

# Prefer modern impersonates known-good on current curl_cffi.
# Keep the set small: older chrome* can TLS-handshake chatgpt.com poorly under WARP.
_IMPERSONATE_CANDIDATES = (
    "chrome142",
    "chrome136",
    "chrome131",
    "chrome124",
)

# Official-ish sec-ch-ua brand strings keyed by Chrome major.
# Brand token order / Not_A Brand variants change across Chrome releases;
# keep them aligned with what real browsers emit for that major.
_CHROME_SEC_CH_UA: dict[str, str] = {
    "120": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "124": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "131": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "133": '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
    "136": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
    "142": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
    "145": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
}

# Realistic full-version build bases (major.0.build.patch).
_CHROME_BUILD_BASE: dict[str, tuple[int, tuple[int, int]]] = {
    # major -> (build, (patch_lo, patch_hi))
    "120": (6099, (62, 200)),
    "124": (6367, (60, 207)),
    "131": (6778, (69, 205)),
    "133": (6943, (33, 153)),
    "136": (7103, (48, 175)),
    "142": (7540, (30, 150)),
    "145": (7632, (20, 120)),
}

_MAC_OS_BUILDS = (
    "10_15_7",
    "11_7_10",
    "12_7_6",
    "13_6_7",
    "14_5",
    "14_6_1",
    "15_0",
    "15_1",
)
# UA token -> Client Hint platform-version (approx)
_MAC_PLATFORM_VERSIONS = {
    "10_15_7": "10.15.7",
    "11_7_10": "11.7.10",
    "12_7_6": "12.7.6",
    "13_6_7": "13.6.7",
    "14_5": "14.5.0",
    "14_6_1": "14.6.1",
    "15_0": "15.0.0",
    "15_1": "15.1.0",
}
_WIN_OS_TOKENS = (
    "Windows NT 10.0; Win64; x64",
    "Windows NT 10.0; Win64; x64",
    "Windows NT 11.0; Win64; x64",
)
_WIN_PLATFORM_VERSIONS = (
    "10.0.0",
    "15.0.0",  # Win11 often reports 15.0.0 in CH
)
_LANG_PRESETS = (
    ("en-US,en;q=0.9", ["en-US", "en"]),
    ("en-US,en;q=0.9,zh-CN;q=0.8", ["en-US", "en", "zh-CN"]),
    ("en-GB,en;q=0.9", ["en-GB", "en"]),
    ("en-US,en;q=0.9,ja;q=0.7", ["en-US", "en", "ja"]),
    ("en-US,en;q=0.9,ko;q=0.7", ["en-US", "en", "ko"]),
)
_SCREENS = (
    (1440, 900),
    (1512, 982),
    (1680, 1050),
    (1920, 1080),
    (1920, 1200),
    (2560, 1440),
    (1366, 768),
    (1536, 864),
)
_CORES = (4, 6, 8, 10, 12, 16)
_MEMORIES = (4, 8, 8, 16, 16, 32)


def _normalize_platform(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if text in {"win", "windows", "windows nt", "win32"}:
        return "windows"
    if text in {"mac", "macos", "darwin", "osx", "macintel"}:
        return "mac"
    return DEFAULT_PLATFORM


def resolve_impersonate(explicit: str | None = None) -> str:
    env = str(
        explicit
        or os.environ.get("HTTP_IMPERSONATE")
        or os.environ.get("CURL_CFFI_IMPERSONATE")
        or DEFAULT_IMPERSONATE
    ).strip()
    return env or DEFAULT_IMPERSONATE


def chrome_major_from_impersonate(impersonate: str | None = None) -> str:
    imp = resolve_impersonate(impersonate)
    m = re.search(r"(\d{2,3})", imp)
    return m.group(1) if m else DEFAULT_CHROME_MAJOR


def resolve_platform(explicit: str | None = None) -> str:
    return _normalize_platform(
        explicit or os.environ.get("OPENAI_BROWSER_PLATFORM") or DEFAULT_PLATFORM
    )


def _pick_impersonate(rng: random.Random) -> str:
    forced = str(os.environ.get("HTTP_IMPERSONATE") or os.environ.get("CURL_CFFI_IMPERSONATE") or "").strip()
    if forced:
        return forced
    # Prefer modern profiles; keep some diversity.
    weights = [6, 3, 2, 1][: len(_IMPERSONATE_CANDIDATES)]
    return rng.choices(list(_IMPERSONATE_CANDIDATES), weights=weights, k=1)[0]


def _chrome_full_version(major: str, rng: random.Random) -> str:
    base = _CHROME_BUILD_BASE.get(str(major))
    if base:
        build, (plo, phi) = base
        patch = rng.randint(plo, phi)
        return f"{major}.0.{build}.{patch}"
    # Fallback realistic-ish full version.
    build = rng.randint(6000, 7600)
    patch = rng.randint(0, 200)
    return f"{major}.0.{build}.{patch}"


def build_user_agent(
    *,
    chrome_major: str | None = None,
    platform: str | None = None,
    os_build: str | None = None,
    chrome_full: str | None = None,
    rng: random.Random | None = None,
) -> str:
    rng = rng or random.Random()
    major = str(chrome_major or DEFAULT_CHROME_MAJOR)
    full = chrome_full or f"{major}.0.0.0"
    plat = resolve_platform(platform)
    if plat == "windows":
        os_token = os_build or rng.choice(_WIN_OS_TOKENS)
        return (
            f"Mozilla/5.0 ({os_token}) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{full} Safari/537.36"
        )
    mac_ver = os_build or rng.choice(_MAC_OS_BUILDS)
    return (
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X {mac_ver}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{full} Safari/537.36"
    )


def sec_ch_ua(chrome_major: str | None = None) -> str:
    major = str(chrome_major or DEFAULT_CHROME_MAJOR)
    if major in _CHROME_SEC_CH_UA:
        return _CHROME_SEC_CH_UA[major]
    # Generic fallback for unknown majors.
    return (
        f'"Chromium";v="{major}", "Google Chrome";v="{major}", '
        f'"Not_A Brand";v="99"'
    )


def sec_ch_ua_full_version_list(chrome_full: str | None = None, chrome_major: str | None = None) -> str:
    major = str(chrome_major or DEFAULT_CHROME_MAJOR)
    full = str(chrome_full or f"{major}.0.0.0")
    # Mirror brand order of sec_ch_ua when known.
    if major in {"131"}:
        return (
            f'"Google Chrome";v="{full}", "Chromium";v="{full}", '
            f'"Not_A Brand";v="10.0.0.0"'
        )
    if major in {"133"}:
        return (
            f'"Not(A:Brand";v="10.0.0.0", "Google Chrome";v="{full}", '
            f'"Chromium";v="{full}"'
        )
    if major in {"145"}:
        return (
            f'"Google Chrome";v="{full}", "Not:A-Brand";v="10.0.0.0", '
            f'"Chromium";v="{full}"'
        )
    return (
        f'"Chromium";v="{full}", "Google Chrome";v="{full}", '
        f'"Not_A Brand";v="10.0.0.0"'
    )


def sec_ch_ua_platform(platform: str | None = None) -> str:
    plat = resolve_platform(platform)
    return '"Windows"' if plat == "windows" else '"macOS"'


def sec_ch_ua_platform_version(
    *,
    platform: str | None = None,
    os_build: str | None = None,
    rng: random.Random | None = None,
) -> str:
    rng = rng or random.Random(0)
    plat = resolve_platform(platform)
    if plat == "windows":
        ver = os_build if os_build and re.match(r"^\d+\.\d+", str(os_build)) else rng.choice(_WIN_PLATFORM_VERSIONS)
        # Map UA-style tokens to CH platform-version when needed.
        if "Windows NT 11" in str(os_build or ""):
            ver = "15.0.0"
        elif "Windows NT 10" in str(os_build or ""):
            ver = "10.0.0"
        return f'"{ver}"'
    mac_key = str(os_build or "10_15_7")
    ver = _MAC_PLATFORM_VERSIONS.get(mac_key) or mac_key.replace("_", ".")
    return f'"{ver}"'


def navigator_platform(platform: str | None = None) -> str:
    plat = resolve_platform(platform)
    return "Win32" if plat == "windows" else "MacIntel"


def _client_hints(
    *,
    major: str,
    full: str,
    plat: str,
    os_build: str,
    rng: random.Random | None = None,
) -> dict[str, str]:
    rng = rng or random.Random(0)
    return {
        "sec_ch_ua": sec_ch_ua(major),
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": sec_ch_ua_platform(plat),
        "sec_ch_ua_full_version_list": sec_ch_ua_full_version_list(full, major),
        "sec_ch_ua_arch": '"x86"' if plat == "mac" else '"x86_64"',
        "sec_ch_ua_bitness": '"64"',
        "sec_ch_ua_model": '""',
        "sec_ch_ua_platform_version": sec_ch_ua_platform_version(
            platform=plat, os_build=os_build, rng=rng
        ),
    }


def browser_profile(
    *,
    impersonate: str | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Deterministic/default profile (no randomness)."""
    imp = resolve_impersonate(impersonate)
    major = chrome_major_from_impersonate(imp)
    plat = resolve_platform(platform)
    os_build = "10_15_7" if plat == "mac" else "Windows NT 10.0; Win64; x64"
    full = f"{major}.0.0.0"
    # Pass fixed os_build + chrome_full so UA is fully deterministic.
    ua = build_user_agent(
        chrome_major=major,
        platform=plat,
        os_build=os_build,
        chrome_full=full,
        rng=random.Random(0),
    )
    screen = (1920, 1080)
    hints = _client_hints(major=major, full=full, plat=plat, os_build=os_build, rng=random.Random(0))
    return {
        "profile_id": "default",
        "impersonate": imp,
        "chrome_major": major,
        "chrome_full": full,
        "platform": plat,
        "os_build": os_build,
        "user_agent": ua,
        **hints,
        "navigator_platform": navigator_platform(plat),
        "accept_language": "en-US,en;q=0.9",
        "languages": ["en-US", "en"],
        "hardware_concurrency": 8,
        "device_memory": 8,
        "screen_width": screen[0],
        "screen_height": screen[1],
        "color_depth": 24,
        "device_pixel_ratio": 2 if plat == "mac" else 1,
        "max_touch_points": 0,
        "timezone_offset_min": 0,
        "vendor": "Google Inc.",
        "seed": "0",
    }


def random_browser_profile(
    *,
    seed: str | None = None,
    platform: str | None = None,
    impersonate: str | None = None,
) -> dict[str, Any]:
    """Create an independent, self-consistent profile for one account."""
    seed_s = str(seed or uuid.uuid4().hex)
    rng = random.Random(seed_s)

    env_plat = str(os.environ.get("OPENAI_BROWSER_PLATFORM") or "").strip()
    if platform:
        plat = resolve_platform(platform)
    elif env_plat and env_plat.lower() not in {"auto", "mixed", "random"}:
        plat = resolve_platform(env_plat)
    else:
        # yukkcat/public registrars almost always use Windows Client Hints.
        # Default weight: windows-heavy mix keeps TLS/UA coherent while
        # preserving some mac diversity. Force with OPENAI_BROWSER_PLATFORM=
        # windows|mac, or auto/mixed for weighted pick.
        win_weight = 70
        try:
            win_weight = int(os.environ.get("OPENAI_BROWSER_WINDOWS_WEIGHT", "70") or 70)
        except Exception:
            win_weight = 70
        win_weight = max(0, min(100, win_weight))
        plat = "windows" if rng.randrange(100) < win_weight else "mac"

    imp = resolve_impersonate(impersonate) if (impersonate or os.environ.get("HTTP_IMPERSONATE") or os.environ.get("CURL_CFFI_IMPERSONATE")) else _pick_impersonate(rng)
    major = chrome_major_from_impersonate(imp)
    full = _chrome_full_version(major, rng)
    os_build = rng.choice(_MAC_OS_BUILDS) if plat == "mac" else rng.choice(_WIN_OS_TOKENS)
    ua = build_user_agent(
        chrome_major=major,
        platform=plat,
        os_build=os_build,
        chrome_full=full,
        rng=rng,
    )
    lang_header, languages = rng.choice(_LANG_PRESETS)
    screen_w, screen_h = rng.choice(_SCREENS)
    cores = rng.choice(_CORES)
    memory = rng.choice(_MEMORIES)
    dpr = rng.choice([1, 1.25, 1.5, 2, 2]) if plat == "mac" else rng.choice([1, 1.25, 1.5, 1.75])
    tz_off = rng.choice([0, -60, -120, -180, -240, -300, -360, -420, -480, 60, 120, 180, 330, 480, 540])
    hints = _client_hints(major=major, full=full, plat=plat, os_build=os_build, rng=rng)

    return {
        "profile_id": secrets.token_hex(8),
        "seed": seed_s,
        "impersonate": imp,
        "chrome_major": major,
        "chrome_full": full,
        "platform": plat,
        "os_build": os_build,
        "user_agent": ua,
        **hints,
        "navigator_platform": navigator_platform(plat),
        "accept_language": lang_header,
        "languages": list(languages),
        "hardware_concurrency": cores,
        "device_memory": memory,
        "screen_width": screen_w,
        "screen_height": screen_h,
        "color_depth": 24,
        "device_pixel_ratio": dpr,
        "max_touch_points": 0,
        "timezone_offset_min": tz_off,
        "vendor": "Google Inc.",
    }


def _profile_client_hint_headers(p: dict[str, Any]) -> dict[str, str]:
    """Build sec-ch-ua* header map from a profile dict."""
    major = str(p.get("chrome_major") or DEFAULT_CHROME_MAJOR)
    full = str(p.get("chrome_full") or f"{major}.0.0.0")
    plat = str(p.get("platform") or DEFAULT_PLATFORM)
    os_build = str(p.get("os_build") or "")
    # Prefer stored values; recompute missing ones for older profiles.
    return {
        "sec-ch-ua": str(p.get("sec_ch_ua") or sec_ch_ua(major)),
        "sec-ch-ua-mobile": str(p.get("sec_ch_ua_mobile") or "?0"),
        "sec-ch-ua-platform": str(p.get("sec_ch_ua_platform") or sec_ch_ua_platform(plat)),
        "sec-ch-ua-full-version-list": str(
            p.get("sec_ch_ua_full_version_list")
            or sec_ch_ua_full_version_list(full, major)
        ),
        "sec-ch-ua-arch": str(p.get("sec_ch_ua_arch") or ('"x86"' if plat == "mac" else '"x86_64"')),
        "sec-ch-ua-bitness": str(p.get("sec_ch_ua_bitness") or '"64"'),
        "sec-ch-ua-model": str(p.get("sec_ch_ua_model") or '""'),
        "sec-ch-ua-platform-version": str(
            p.get("sec_ch_ua_platform_version")
            or sec_ch_ua_platform_version(platform=plat, os_build=os_build)
        ),
    }


def default_request_headers(
    *,
    profile: dict[str, Any] | None = None,
    impersonate: str | None = None,
    platform: str | None = None,
    for_api: bool = True,
) -> dict[str, str]:
    p = profile or browser_profile(impersonate=impersonate, platform=platform)
    headers = {
        "User-Agent": p["user_agent"],
        "Accept": "application/json" if for_api else (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": p.get("accept_language") or "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        **_profile_client_hint_headers(p),
        "Sec-Fetch-Dest": "empty" if for_api else "document",
        "Sec-Fetch-Mode": "cors" if for_api else "navigate",
        "Sec-Fetch-Site": "same-site" if for_api else "none",
    }
    # Match yukkcat common_headers: privacy signals common on desktop Chrome.
    if str(os.environ.get("OPENAI_SEND_GPC", "1")).strip().lower() in {
        "1", "true", "yes", "on",
    }:
        headers.setdefault("DNT", "1")
        headers.setdefault("Sec-GPC", "1")
    return headers


def apply_profile_to_session(session: Any, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge profile headers into a curl_cffi/requests session."""
    p = profile or browser_profile()
    headers = {
        "User-Agent": p["user_agent"],
        "Accept-Language": p.get("accept_language") or "en-US,en;q=0.9",
        **_profile_client_hint_headers(p),
    }
    try:
        session.headers.update(headers)
    except Exception:
        for k, v in headers.items():
            session.headers[k] = v
    return p


def profile_summary(profile: dict[str, Any] | None) -> str:
    p = profile or {}
    return (
        f"id={p.get('profile_id') or '-'} plat={p.get('platform') or '-'} "
        f"imp={p.get('impersonate') or '-'} chrome={p.get('chrome_major') or '-'} "
        f"screen={p.get('screen_width')}x{p.get('screen_height')} "
        f"cores={p.get('hardware_concurrency')} mem={p.get('device_memory')}"
    )
