"""Minimal OpenAI chat.completions surface over Grok Build /responses."""
from __future__ import annotations

from typing import Any

from services.grok_account_service import grok_account_service
from services.grok_backend_api import GrokBackendError, create_response, responses_to_chat_completion
from utils.grok_models import DEFAULT_GROK_TEXT_MODEL


def _messages_to_input(messages: list[dict[str, Any]] | None, prompt: str | None) -> str:
    if prompt and str(prompt).strip():
        return str(prompt).strip()
    parts: list[str] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").strip()
        content = msg.get("content")
        if isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") in {"text", "input_text"}:
                    texts.append(str(block.get("text") or ""))
                elif isinstance(block, str):
                    texts.append(block)
            content = "\n".join(t for t in texts if t)
        text = str(content or "").strip()
        if text:
            parts.append(f"{role}: {text}")
    return "\n".join(parts).strip() or "Hello"


def handle(body: dict[str, Any]) -> dict[str, Any]:
    model = str(body.get("model") or DEFAULT_GROK_TEXT_MODEL).strip() or DEFAULT_GROK_TEXT_MODEL
    messages = body.get("messages") if isinstance(body.get("messages"), list) else None
    prompt = body.get("prompt") if isinstance(body.get("prompt"), str) else None
    input_text = _messages_to_input(messages, prompt)
    max_tokens = body.get("max_tokens") or body.get("max_output_tokens") or 1024
    try:
        max_output_tokens = max(1, int(max_tokens))
    except (TypeError, ValueError):
        max_output_tokens = 1024

    exclude: set[str] = set()
    last_error: str | None = None
    for _ in range(3):
        account = grok_account_service.get_next_account(exclude_tokens=exclude)
        if account is None:
            break
        token = str(account.get("access_token") or "")
        exclude.add(token)
        try:
            data = create_response(
                account,
                input_text=input_text,
                model=model,
                max_output_tokens=max_output_tokens,
            )
            grok_account_service.mark_result(token, True)
            return responses_to_chat_completion(data, model=model)
        except GrokBackendError as exc:
            last_error = str(exc)
            grok_account_service.mark_result(token, False, error=str(exc)[:300])
            continue
        except Exception as exc:
            last_error = str(exc)
            grok_account_service.mark_result(token, False, error=str(exc)[:300])
            continue

    raise RuntimeError(last_error or "no available grok accounts for chat")
