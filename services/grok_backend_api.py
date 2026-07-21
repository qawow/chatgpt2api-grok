"""xAI / Grok Build (cli-chat-proxy) client — isolated from OpenAIBackendAPI."""
from __future__ import annotations

import base64
import json
import re
import time
from typing import Any
from urllib.parse import urljoin

import os

import requests

from utils.grok_models import DEFAULT_GROK_TEXT_MODEL, resolve_grok_image_model

DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
DEFAULT_TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"
DEFAULT_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
DEFAULT_HEADERS = {
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-version": "0.2.93",
    "x-grok-client-identifier": "grok-shell",
}

_DATA_URL_RE = re.compile(
    r"data:(?P<mime>image/[-+.\w]+);base64,(?P<data>[A-Za-z0-9+/=\s]+)",
    re.I,
)


class GrokBackendError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _config_data() -> dict[str, Any]:
    try:
        from services.config import config
        raw = config.data if isinstance(getattr(config, "data", None), dict) else {}
        return raw
    except Exception:
        return {}


def grok_settings() -> dict[str, Any]:
    data = _config_data()
    raw = data.get("grok") if isinstance(data.get("grok"), dict) else {}
    base_url = (
        str(raw.get("base_url") or "").strip()
        or str(os.environ.get("GROK_BASE_URL") or "").strip()
        or DEFAULT_BASE_URL
    )
    client_id = (
        str(raw.get("client_id") or "").strip()
        or str(os.environ.get("GROK_CLIENT_ID") or "").strip()
        or DEFAULT_CLIENT_ID
    )
    token_endpoint = str(raw.get("token_endpoint") or DEFAULT_TOKEN_ENDPOINT).strip()
    headers = dict(DEFAULT_HEADERS)
    extra = raw.get("default_headers")
    if isinstance(extra, dict):
        for key, value in extra.items():
            if str(key).strip() and value is not None:
                headers[str(key)] = str(value)
    probe_model = str(raw.get("probe_model") or DEFAULT_GROK_TEXT_MODEL).strip() or DEFAULT_GROK_TEXT_MODEL
    return {
        "base_url": normalize_base_url(base_url),
        "client_id": client_id,
        "token_endpoint": token_endpoint,
        "default_headers": headers,
        "probe_model": probe_model,
    }


def normalize_base_url(base_url: str) -> str:
    base = (base_url or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    # Free Build quota lives on cli-chat-proxy, not paid api.x.ai
    if "api.x.ai" in base:
        return DEFAULT_BASE_URL
    return base.rstrip("/")


def _global_proxy() -> str:
    try:
        from services.config import config
        return str(config.get_proxy_settings() or "").strip()
    except Exception:
        return str(os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "").strip()


def _proxies(proxy: str | None) -> dict[str, str] | None:
    value = (proxy or _global_proxy() or "").strip()
    if not value:
        return None
    return {"http": value, "https": value}


def build_request_headers(account: dict[str, Any] | None = None) -> dict[str, str]:
    settings = grok_settings()
    token = ""
    account = account or {}
    token = str(account.get("access_token") or "").strip()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "grok-cli/0.2.93",
        **settings["default_headers"],
    }
    extra = account.get("headers")
    if isinstance(extra, dict):
        for key, value in extra.items():
            if isinstance(key, str) and key.strip() and value is not None:
                headers[key] = str(value)
    return headers


def account_base_url(account: dict[str, Any] | None = None) -> str:
    settings = grok_settings()
    account = account or {}
    return normalize_base_url(str(account.get("base_url") or settings["base_url"]))


def account_proxy(account: dict[str, Any] | None = None) -> str:
    account = account or {}
    return str(account.get("proxy") or "").strip()


def refresh_access_token(
    refresh_token: str,
    *,
    client_id: str | None = None,
    token_endpoint: str | None = None,
    proxy: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    settings = grok_settings()
    rt = str(refresh_token or "").strip()
    if not rt:
        raise GrokBackendError("missing refresh_token")
    endpoint = (token_endpoint or settings["token_endpoint"]).strip()
    cid = (client_id or settings["client_id"]).strip()
    data = {
        "grant_type": "refresh_token",
        "client_id": cid,
        "refresh_token": rt,
    }
    try:
        resp = requests.post(
            endpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=timeout,
            proxies=_proxies(proxy),
        )
    except requests.RequestException as exc:
        raise GrokBackendError(f"refresh network error: {exc}") from exc
    if resp.status_code != 200:
        raise GrokBackendError(
            f"refresh failed: HTTP {resp.status_code}: {(resp.text or '')[:300]}",
            status=resp.status_code,
            body=(resp.text or "")[:500],
        )
    token = resp.json()
    if not isinstance(token, dict):
        raise GrokBackendError("refresh response is not an object")
    now = int(time.time())
    if "expires_in" in token and "expires_at" not in token:
        try:
            token["expires_at"] = now + int(token["expires_in"])
        except Exception:
            pass
    if "refresh_token" not in token:
        token["refresh_token"] = rt
    return token


def probe_responses(
    account: dict[str, Any],
    *,
    model: str | None = None,
    timeout: float = 45.0,
) -> dict[str, Any]:
    settings = grok_settings()
    base = account_base_url(account)
    url = urljoin(base.rstrip("/") + "/", "responses")
    body = {
        "model": (model or settings["probe_model"]),
        "input": "Reply exactly: OK",
        "max_output_tokens": 8,
    }
    try:
        resp = requests.post(
            url,
            headers=build_request_headers(account),
            json=body,
            timeout=timeout,
            proxies=_proxies(account_proxy(account)),
        )
    except requests.RequestException as exc:
        raise GrokBackendError(f"probe network error: {exc}") from exc

    out: dict[str, Any] = {
        "status": resp.status_code,
        "url": url,
        "remaining_tokens": _header_int(resp.headers, "x-ratelimit-remaining-tokens"),
        "limit_tokens": _header_int(resp.headers, "x-ratelimit-limit-tokens"),
        "remaining_requests": _header_int(resp.headers, "x-ratelimit-remaining-requests"),
        "limit_requests": _header_int(resp.headers, "x-ratelimit-limit-requests"),
    }
    text = resp.text or ""
    if resp.status_code == 429:
        out["error"] = text[:300]
        out["code"] = "rate_limited_or_quota"
        return out
    try:
        data = resp.json()
        if isinstance(data, dict):
            out["model"] = data.get("model")
            usage = data.get("usage")
            if isinstance(usage, dict):
                out["probe_total_tokens"] = usage.get("total_tokens") or usage.get("totalTokens")
            if resp.status_code >= 400:
                err = data.get("error")
                out["error"] = str(err or text)[:300]
    except Exception:
        if resp.status_code >= 400:
            out["error"] = text[:300]
    return out


def list_upstream_models(account: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    base = account_base_url(account)
    url = urljoin(base.rstrip("/") + "/", "models")
    try:
        resp = requests.get(
            url,
            headers=build_request_headers(account),
            timeout=timeout,
            proxies=_proxies(account_proxy(account)),
        )
    except requests.RequestException as exc:
        raise GrokBackendError(f"models network error: {exc}") from exc
    if resp.status_code != 200:
        raise GrokBackendError(
            f"models failed: HTTP {resp.status_code}: {(resp.text or '')[:200]}",
            status=resp.status_code,
            body=(resp.text or "")[:400],
        )
    try:
        return resp.json()
    except Exception as exc:
        raise GrokBackendError("models response is not JSON") from exc


def create_response(
    account: dict[str, Any],
    *,
    input_text: str,
    model: str | None = None,
    max_output_tokens: int | None = 1024,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    settings = grok_settings()
    base = account_base_url(account)
    url = urljoin(base.rstrip("/") + "/", "responses")
    body: dict[str, Any] = {
        "model": model or settings["probe_model"],
        "input": input_text,
    }
    if max_output_tokens is not None:
        body["max_output_tokens"] = max_output_tokens
    if tools:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    try:
        resp = requests.post(
            url,
            headers=build_request_headers(account),
            json=body,
            timeout=timeout,
            proxies=_proxies(account_proxy(account)),
        )
    except requests.RequestException as exc:
        raise GrokBackendError(f"responses network error: {exc}") from exc
    if resp.status_code >= 400:
        raise GrokBackendError(
            f"responses failed: HTTP {resp.status_code}: {(resp.text or '')[:300]}",
            status=resp.status_code,
            body=(resp.text or "")[:500],
        )
    try:
        data = resp.json()
    except Exception as exc:
        raise GrokBackendError("responses body is not JSON") from exc
    if not isinstance(data, dict):
        raise GrokBackendError("responses body is not an object")
    return data


def generate_image(
    account: dict[str, Any],
    *,
    prompt: str,
    model: str | None = None,
    n: int = 1,
    size: str | None = None,
    response_format: str = "b64_json",
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Generate images on Grok Build channel; never fall back to ChatGPT.

    Free Build accounts typically have no paid /images/generations credits.
    The working free path is:

        POST {base}/responses
        model=grok-4.5 (text / build-free)
        tools=[{type: image_generation}]

    which returns output items of type image_generation_call with JPEG/PNG
    base64 in ``result``.

    Returns OpenAI-like {created, data:[{b64_json|url}], _meta:{upstream_path,...}}.
    """
    resolved_model = resolve_grok_image_model(model)
    settings = grok_settings()
    base = account_base_url(account)
    headers = build_request_headers(account)
    proxies = _proxies(account_proxy(account))
    attempts: list[dict[str, Any]] = []
    want_n = max(1, min(int(n or 1), 4))
    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        raise GrokBackendError("prompt is required", status=400)

    # 1) Free Build path: /responses + image_generation tool (dialog-style free image)
    free_models = _free_image_response_models(resolved_model, settings["probe_model"])
    for free_model in free_models:
        try:
            # Free Build accepts tools=[{type:image_generation}] but rejects OpenAI-style
            # tool_choice object forms (422 ModelToolChoice). Omit tool_choice.
            data = create_response(
                account,
                input_text=prompt_text,
                model=free_model,
                max_output_tokens=None,
                tools=[{"type": "image_generation"}],
                timeout=timeout,
            )
        except GrokBackendError as exc:
            attempts.append(
                {
                    "path": "responses+image_generation",
                    "model": free_model,
                    "error": str(exc)[:200],
                    "status": exc.status,
                }
            )
            if exc.status in {401, 403}:
                break
            continue

        extracted = _extract_images_from_responses(data)
        upstream_model = str(data.get("model") or free_model)
        if not extracted:
            attempts.append(
                {
                    "path": "responses+image_generation",
                    "model": free_model,
                    "status": 200,
                    "error": "no_image_payload_in_response",
                    "response_status": data.get("status"),
                }
            )
            continue

        data_items = list(extracted[:want_n])
        while len(data_items) < want_n:
            try:
                more = create_response(
                    account,
                    input_text=prompt_text,
                    model=free_model,
                    max_output_tokens=None,
                    tools=[{"type": "image_generation"}],
                    timeout=timeout,
                )
                more_imgs = _extract_images_from_responses(more)
                if not more_imgs:
                    break
                data_items.extend(more_imgs)
            except GrokBackendError as exc:
                attempts.append(
                    {
                        "path": "responses+image_generation",
                        "model": free_model,
                        "error": str(exc)[:200],
                        "status": exc.status,
                    }
                )
                break
        data_items = data_items[:want_n]
        if response_format == "url":
            data_items = [
                {k: v for k, v in item.items() if k != "b64_json"} if item.get("url") else item
                for item in data_items
            ]
        return {
            "created": int(time.time()),
            "data": data_items,
            "_meta": {
                "upstream_path": "responses+image_generation",
                "upstream_model": upstream_model,
                "attempts": attempts
                + [
                    {
                        "path": "responses+image_generation",
                        "model": free_model,
                        "status": 200,
                        "images": len(data_items),
                    }
                ],
            },
        }

    # 2) Official-shaped images endpoint (paid credits / subscription)
    images_url = urljoin(base.rstrip("/") + "/", "images/generations")
    image_body: dict[str, Any] = {
        "prompt": prompt_text,
        "model": resolved_model,
        "n": want_n,
        "response_format": response_format or "b64_json",
    }
    if size:
        image_body["size"] = size

    for candidate_model in _image_model_candidates(resolved_model):
        body = dict(image_body)
        body["model"] = candidate_model
        try:
            resp = requests.post(
                images_url,
                headers=headers,
                json=body,
                timeout=timeout,
                proxies=proxies,
            )
        except requests.RequestException as exc:
            attempts.append({"path": "images/generations", "model": candidate_model, "error": str(exc)})
            continue
        attempts.append(
            {
                "path": "images/generations",
                "model": candidate_model,
                "status": resp.status_code,
                "body_prefix": (resp.text or "")[:160],
            }
        )
        if resp.status_code == 200:
            parsed = _parse_openai_images_response(resp, response_format=response_format)
            if parsed is not None:
                parsed["_meta"] = {
                    "upstream_path": "images/generations",
                    "upstream_model": candidate_model,
                    "attempts": attempts,
                }
                return parsed
        if resp.status_code in {401, 403}:
            # Auth / spending-limit — try remaining aliases once then stop this path
            # (free path already attempted above)
            continue
        if resp.status_code not in {404, 405, 400, 422}:
            break

    # 3) Last resort: /responses with image-like model from catalog (if any)
    image_model_from_catalog = _pick_image_model_from_catalog(account, timeout=min(timeout, 30.0))
    if image_model_from_catalog:
        try:
            data = create_response(
                account,
                input_text=f"Generate an image: {prompt_text}",
                model=image_model_from_catalog,
                max_output_tokens=2048,
                timeout=timeout,
            )
            extracted = _extract_images_from_responses(data)
            if extracted:
                return {
                    "created": int(time.time()),
                    "data": extracted[:want_n],
                    "_meta": {
                        "upstream_path": "responses",
                        "upstream_model": image_model_from_catalog,
                        "attempts": attempts,
                    },
                }
            attempts.append(
                {
                    "path": "responses",
                    "model": image_model_from_catalog,
                    "error": "no_image_payload_in_response",
                }
            )
        except GrokBackendError as exc:
            attempts.append(
                {
                    "path": "responses",
                    "model": image_model_from_catalog,
                    "error": str(exc)[:200],
                    "status": exc.status,
                }
            )

    raise GrokBackendError(
        "Grok Build channel has no usable image generation upstream "
        "(tried free /responses+image_generation, then /images/generations). "
        "Account pool and refresh still work. "
        f"attempts={json.dumps(attempts, ensure_ascii=False)[:800]}",
        status=502,
        body={"attempts": attempts},
    )


def responses_to_chat_completion(data: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Map a minimal Responses payload into OpenAI chat.completion shape."""
    text = _extract_text_from_responses(data)
    return {
        "id": f"chatcmpl-grok-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": data.get("usage") if isinstance(data.get("usage"), dict) else {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def _header_int(headers: Any, name: str) -> int | None:
    raw = headers.get(name) if headers is not None else None
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _image_model_candidates(model: str) -> list[str]:
    ordered = [model]
    for alias in ("grok-2-image", "grok-2-image-1212", "grok-imagine"):
        if alias not in ordered:
            ordered.append(alias)
    return ordered


def _free_image_response_models(requested_image_model: str, probe_model: str) -> list[str]:
    """Models accepted by free Build /responses for the image_generation tool.

    Free cli-chat-proxy only exposes text models like grok-4.5 (resolved upstream
    to grok-4.5-build-free). Image model ids such as grok-2-image return
    "Model not found" on /responses.
    """
    ordered: list[str] = []
    for candidate in (
        probe_model or DEFAULT_GROK_TEXT_MODEL,
        DEFAULT_GROK_TEXT_MODEL,
        "grok-4.5",
        "grok-4",
        "grok-3",
    ):
        name = str(candidate or "").strip()
        if name and name not in ordered:
            ordered.append(name)
    # Never put pure image model ids first — they 400 on free Build responses.
    _ = requested_image_model
    return ordered


def _looks_like_image_b64(value: str) -> bool:
    s = re.sub(r"\s+", "", value or "")
    if len(s) < 64:
        return False
    if s.startswith("data:image"):
        return True
    # Common raw base64 image prefixes (JPEG / PNG / GIF / WEBP RIFF)
    if s.startswith(("/9j/", "iVBOR", "R0lGOD", "UklGR")):
        return True
    try:
        raw = base64.b64decode(s[:96] + "====", validate=False)
    except Exception:
        return False
    return raw.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF"))


def _normalize_image_b64(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    m = _DATA_URL_RE.search(text)
    if m:
        return re.sub(r"\s+", "", m.group("data"))
    return re.sub(r"\s+", "", text)


def _pick_image_model_from_catalog(account: dict[str, Any], *, timeout: float) -> str | None:
    try:
        catalog = list_upstream_models(account, timeout=timeout)
    except GrokBackendError:
        return None
    data = catalog.get("data") if isinstance(catalog, dict) else None
    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        low = mid.lower()
        if mid and ("image" in low or "imagine" in low or "flux" in low or "aurora" in low):
            return mid
    return None


def _parse_openai_images_response(resp: requests.Response, *, response_format: str) -> dict[str, Any] | None:
    try:
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    items = data.get("data")
    if not isinstance(items, list) or not items:
        # Some gateways return b64 at top level
        if data.get("b64_json") or data.get("url"):
            items = [data]
        else:
            return None
    normalized: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry: dict[str, str] = {}
        b64 = item.get("b64_json") or item.get("base64")
        url = item.get("url")
        if b64:
            entry["b64_json"] = str(b64)
        if url:
            entry["url"] = str(url)
        if item.get("revised_prompt"):
            entry["revised_prompt"] = str(item.get("revised_prompt"))
        if entry:
            # Prefer requested format but keep whatever we got
            if response_format == "url" and "url" in entry and "b64_json" in entry:
                entry = {k: v for k, v in entry.items() if k != "b64_json"}
            normalized.append(entry)
    if not normalized:
        return None
    return {
        "created": int(data.get("created") or time.time()),
        "data": normalized,
        "usage": data.get("usage") if isinstance(data.get("usage"), dict) else None,
    }


def _extract_images_from_responses(data: dict[str, Any]) -> list[dict[str, str]]:
    """Pull image payloads from Build /responses output.

    Free image path yields items like::

        {"type": "image_generation_call", "status": "completed",
         "result": "<jpeg-or-png-base64>", "prompt": "..."}

    Also accepts OpenAI-style b64_json/url fields and data:image URLs in text.
    """
    found: list[dict[str, str]] = []
    text_blobs: list[str] = []

    def push_b64(raw: str) -> None:
        b64 = _normalize_image_b64(raw)
        if b64 and _looks_like_image_b64(b64):
            found.append({"b64_json": b64})

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            node_type = str(node.get("type") or "").strip().lower()

            # Free Build: image_generation_call.result is raw image base64
            if node_type in {"image_generation_call", "image_generation", "image_generation_result"}:
                result = node.get("result") or node.get("image") or node.get("b64_json") or node.get("base64")
                if isinstance(result, str) and result.strip():
                    push_b64(result)
                # Some gateways nest under result.b64_json / result.url
                if isinstance(result, dict):
                    walk(result)

            b64 = node.get("b64_json") or node.get("base64")
            if isinstance(b64, str) and b64.strip():
                push_b64(b64)

            # Generic "result" / "image" fields that look like image b64
            for key in ("result", "image", "image_base64", "data"):
                val = node.get(key)
                if isinstance(val, str) and _looks_like_image_b64(val):
                    push_b64(val)

            url = node.get("url") or node.get("image_url")
            if isinstance(url, str) and url.startswith(("http://", "https://", "data:image")):
                if url.startswith("data:image"):
                    m = _DATA_URL_RE.search(url)
                    if m:
                        found.append({"b64_json": re.sub(r"\s+", "", m.group("data"))})
                    else:
                        found.append({"url": url})
                else:
                    found.append({"url": url})
            for key in ("text", "content", "output_text"):
                val = node.get(key)
                if isinstance(val, str) and val:
                    text_blobs.append(val)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    for blob in text_blobs:
        for match in _DATA_URL_RE.finditer(blob):
            found.append({"b64_json": re.sub(r"\s+", "", match.group("data"))})
    # de-dupe preserving order
    uniq: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in found:
        key = item.get("b64_json") or item.get("url") or ""
        if key and key not in seen:
            seen.add(key)
            uniq.append(item)
    return uniq


def _extract_text_from_responses(data: dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return ""
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return data["output_text"].strip()
    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") in {"output_text", "text"} and isinstance(node.get("text"), str):
                chunks.append(node["text"])
            elif isinstance(node.get("content"), str):
                chunks.append(node["content"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data.get("output") or data)
    text = "\n".join(part.strip() for part in chunks if str(part).strip())
    return text or json.dumps(data, ensure_ascii=False)[:2000]


def b64_to_bytes(value: str) -> bytes:
    return base64.b64decode(value, validate=False)
