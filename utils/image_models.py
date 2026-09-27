"""Single source of truth for every public image model id.

Routing (api/ai.py), the /v1/models catalog, error messages and the backends
all read model names from here. Older modules (utils.helper, utils.grok_models,
services.zhitu360_backend) re-export these names for compatibility — define new
ids here, never as string literals at the call site.

Terminology:
  * canonical id — what /v1/models lists and what we log;
  * alias       — accepted from clients, mapped to a canonical id, not listed;
  * upstream id — what a backend sends to its provider. Not a public id.
"""
from __future__ import annotations

# ── ChatGPT web (chatgpt.com, picture_v2) ────────────────────────────────────
WEB_IMAGE_MODEL = "gpt-image-2.5"
# Pre-1.8.3 name. Still accepted so existing clients keep working; not listed.
LEGACY_WEB_IMAGE_MODELS = frozenset({"gpt-image-2"})

# ── Codex (Plus / Team / Pro only) ───────────────────────────────────────────
CODEX_IMAGE_MODEL = "codex-gpt-image-2"
CODEX_PLAN_PREFIXES = ("plus", "team", "pro")
PREFIXED_CODEX_IMAGE_MODELS = frozenset(f"{plan}-{CODEX_IMAGE_MODEL}" for plan in CODEX_PLAN_PREFIXES)
# Upstream: tools[0].model on /backend-api/codex/responses. Not a public id.
CODEX_UPSTREAM_TOOL_MODEL = "gpt-image-2"
# Upstream: the chat model that drives the Codex image tool.
CODEX_RESPONSES_MODEL = "gpt-5.5"

# ── Grok (xAI Build pool) ────────────────────────────────────────────────────
GROK_IMAGINE_IMAGE_MODEL = "grok-imagine-image"
GROK_2_IMAGE_MODEL = "grok-2-image"
GROK_CANONICAL_IMAGE_MODELS = (GROK_IMAGINE_IMAGE_MODEL, GROK_2_IMAGE_MODEL)
GROK_IMAGE_ALIASES: dict[str, str] = {
    "grok-imagine": GROK_IMAGINE_IMAGE_MODEL,
    "grok-2-image-1212": GROK_2_IMAGE_MODEL,
}
DEFAULT_GROK_IMAGE_MODEL = GROK_2_IMAGE_MODEL
# Upstream: the free Build chat agent that calls tools=[{type:image_generation}].
# A chat model — never a public image id.
DEFAULT_GROK_TEXT_MODEL = "grok-4.5"

# ── Doubao (www.doubao.com) ──────────────────────────────────────────────────
DOUBAO_IMAGE_MODEL = "doubao-image"
# Any model starting with this routes to Doubao (doubao, doubao-image, doubao-seedream…).
DOUBAO_MODEL_PREFIX = "doubao"

# ── 360 智图 ─────────────────────────────────────────────────────────────────
ZHITU360_IMAGE_MODELS = ("jimeng", "jimeng40", "jimeng45", "hunyuan", "tongyi", "wanx21plus")
DEFAULT_ZHITU360_MODEL = "jimeng"
ZHITU360_MODEL_ALIASES: dict[str, str] = {
    "即梦": "jimeng",
    "即梦3": "jimeng",
    "即梦3.0": "jimeng",
    "即梦4": "jimeng40",
    "即梦4.0": "jimeng40",
    "即梦4.5": "jimeng45",
    "混元": "hunyuan",
    "通义": "tongyi",
    "万相": "wanx21plus",
    "wanx": "wanx21plus",
    "zhitu360": "jimeng",
    "360": "jimeng",
    "360智图": "jimeng",
}

# ── Providers ────────────────────────────────────────────────────────────────
PROVIDER_CHATGPT = "chatgpt"
PROVIDER_GROK = "grok"
PROVIDER_DOUBAO = "doubao"
PROVIDER_ZHITU360 = "zhitu360"

# What the catalog advertises for each provider ("owned_by" in /v1/models).
OWNED_BY = {
    PROVIDER_CHATGPT: "chatgpt2api",
    PROVIDER_GROK: "grok",
    PROVIDER_DOUBAO: "doubao",
    PROVIDER_ZHITU360: "zhitu360",
}


def _norm(model: object) -> str:
    return str(model or "").strip().lower()


def is_grok_image_model(model: object) -> bool:
    name = _norm(model)
    if not name:
        return False
    if name in GROK_CANONICAL_IMAGE_MODELS or name in GROK_IMAGE_ALIASES:
        return True
    # Accept any other grok-* image id (e.g. new dated snapshots).
    return name.startswith("grok") and ("image" in name or "imagine" in name)


def is_doubao_model(model: object) -> bool:
    return _norm(model).startswith(DOUBAO_MODEL_PREFIX)


def is_zhitu_model(model: object) -> bool:
    raw = str(model or "").strip()
    if not raw:
        return False
    name = raw.lower()
    return name in ZHITU360_IMAGE_MODELS or raw in ZHITU360_MODEL_ALIASES or name in ZHITU360_MODEL_ALIASES


def image_model_provider(model: object) -> str | None:
    """Which backend serves ``model``; None for anything that is not an image id.

    Order matters only for overlapping prefixes; the ChatGPT ids are matched
    exactly by utils.helper.split_image_model, so they are checked last.
    """
    if is_doubao_model(model):
        return PROVIDER_DOUBAO
    if is_zhitu_model(model):
        return PROVIDER_ZHITU360
    if is_grok_image_model(model):
        return PROVIDER_GROK
    name = _norm(model)
    if (
        name == WEB_IMAGE_MODEL
        or name in LEGACY_WEB_IMAGE_MODELS
        or name == CODEX_IMAGE_MODEL
        or name in PREFIXED_CODEX_IMAGE_MODELS
    ):
        return PROVIDER_CHATGPT
    return None


# Shown when a client asks for a text model. Built from the constants so it can
# never drift again (it still said "gpt-image-2" two releases after the rename).
IMAGE_MODELS_HINT = " / ".join(
    (WEB_IMAGE_MODEL, CODEX_IMAGE_MODEL, GROK_IMAGINE_IMAGE_MODEL, GROK_2_IMAGE_MODEL, DOUBAO_IMAGE_MODEL,
     DEFAULT_ZHITU360_MODEL)
)
TEXT_MODELS_DISABLED = f"this backend only serves image models ({IMAGE_MODELS_HINT}); text models are disabled"
