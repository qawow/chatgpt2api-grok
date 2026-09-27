"""Grok/xAI model ids — kept separate from ChatGPT IMAGE_MODELS to avoid pool mix-ups.

Public image ids (what clients send to /v1/images):
  grok-2-image / grok-2-image-1212  — older Flux image models (paid /images/generations)
  grok-imagine-image / grok-imagine — current Imagine image models
                                       (xAI SDK: client.image.sample(model="grok-imagine-image"))

NOT an image model:
  grok-4.5 — chat/reasoning (https://x.ai/news/grok-4-5). Never list it as a
  generation model. Free cli-chat-proxy may still *internally* ask this chat
  model to call tools=[{type:image_generation}]; that is an agent, not a catalog id.
"""
from __future__ import annotations

# Ids live in utils/image_models.py; re-exported here for existing imports.
from utils.image_models import (
    DEFAULT_GROK_IMAGE_MODEL,
    DEFAULT_GROK_TEXT_MODEL,
    GROK_CANONICAL_IMAGE_MODELS,
    GROK_IMAGE_ALIASES,
    is_grok_image_model,
)

# Every id the Grok image route accepts: canonical ids plus aliases.
GROK_IMAGE_MODELS: frozenset[str] = frozenset(GROK_CANONICAL_IMAGE_MODELS) | frozenset(GROK_IMAGE_ALIASES)

GROK_TEXT_MODELS_DISABLED = (
    f"{DEFAULT_GROK_TEXT_MODEL} is a chat model, not an image model; "
    f"use {' or '.join(GROK_CANONICAL_IMAGE_MODELS)}"
)

GROK_TEXT_MODEL_PREFIXES = (
    "grok-",
    "grok.",
)


def _norm(model: object) -> str:
    return str(model or "").strip().lower()


def is_grok_text_model(model: object) -> bool:
    """Text models that should never hit the ChatGPT pool when routed via /v1/grok/*."""
    name = _norm(model)
    if not name:
        return False
    if is_grok_image_model(name):
        return False
    return name.startswith(GROK_TEXT_MODEL_PREFIXES) or name in {"grok", "xai"}


def resolve_grok_image_model(model: object | None) -> str:
    """Map a client id to a canonical *image* catalog id. Never returns grok-4.5."""
    raw = str(model or "").strip()
    if not raw:
        return DEFAULT_GROK_IMAGE_MODEL
    name = _norm(raw)
    alias = GROK_IMAGE_ALIASES.get(name)
    if alias:
        return alias
    if is_grok_image_model(name):
        return name
    return DEFAULT_GROK_IMAGE_MODEL
