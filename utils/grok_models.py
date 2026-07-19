"""Grok/xAI model ids — kept separate from ChatGPT IMAGE_MODELS to avoid pool mix-ups."""
from __future__ import annotations

# Public OpenAI-compatible image model ids exposed by this proxy.
# Prefer names containing "image" so the existing web image UI filter can pick them up.
GROK_IMAGE_MODELS: set[str] = {
    "grok-2-image",
    "grok-2-image-1212",
    "grok-imagine",  # alias; also accepted by routing
}

# Default when client omits model on /v1/grok/images/generations
DEFAULT_GROK_IMAGE_MODEL = "grok-2-image"

# Build/CLI text probe / chat default
DEFAULT_GROK_TEXT_MODEL = "grok-4.5"

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
    name = str(model or "").strip()
    if not name:
        return DEFAULT_GROK_IMAGE_MODEL
    if is_grok_image_model(name):
        return name
    return DEFAULT_GROK_IMAGE_MODEL
