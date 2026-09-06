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

GROK_IMAGE_MODELS: set[str] = {
    "grok-2-image",
    "grok-2-image-1212",
    "grok-imagine-image",
    "grok-imagine",
}

# Client aliases → canonical image catalog id.
GROK_IMAGE_ALIASES: dict[str, str] = {
    "grok-imagine": "grok-imagine-image",
}

DEFAULT_GROK_IMAGE_MODEL = "grok-2-image"

# Internal only: Build probe + free /responses chat agent. Never a public image id.
DEFAULT_GROK_TEXT_MODEL = "grok-4.5"

GROK_TEXT_MODELS_DISABLED = (
    "grok-4.5 is a chat model, not an image model; "
    "use grok-imagine-image or grok-2-image"
)

GROK_TEXT_MODEL_PREFIXES = (
    "grok-",
    "grok.",
)


def _norm(model: object) -> str:
    return str(model or "").strip().lower()


def is_grok_image_model(model: object) -> bool:
    name = _norm(model)
    if not name:
        return False
    if name in GROK_IMAGE_MODELS:
        return True
    # Accept any grok-* containing "image" or exact imagine alias
    if name.startswith("grok") and ("image" in name or name.endswith("imagine") or "imagine" in name):
        return True
    return False


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
        return raw if raw in GROK_IMAGE_MODELS else name
    return DEFAULT_GROK_IMAGE_MODEL
