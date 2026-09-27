// Image model ids used by the UI. Mirrors utils/image_models.py (the backend's
// single source of truth) — add or rename ids there first, then here. The live
// list always comes from GET /v1/models; these constants are only defaults,
// documentation examples and capability checks.

export const WEB_IMAGE_MODEL = "gpt-image-2.5";
export const CODEX_IMAGE_MODEL = "codex-gpt-image-2";
export const GROK_IMAGINE_IMAGE_MODEL = "grok-imagine-image";
export const GROK_2_IMAGE_MODEL = "grok-2-image";
export const DOUBAO_IMAGE_MODEL = "doubao-image";
export const ZHITU360_IMAGE_MODELS = ["jimeng", "jimeng40", "jimeng45", "hunyuan", "tongyi", "wanx21plus"] as const;

export const DEFAULT_IMAGE_MODEL = WEB_IMAGE_MODEL;

/** Canonical ids shown in docs / the API card. */
export const DOCUMENTED_IMAGE_MODELS: readonly string[] = [
  WEB_IMAGE_MODEL,
  CODEX_IMAGE_MODEL,
  GROK_IMAGINE_IMAGE_MODEL,
  GROK_2_IMAGE_MODEL,
  DOUBAO_IMAGE_MODEL,
  ...ZHITU360_IMAGE_MODELS,
];

function norm(model?: string | null) {
  return String(model || "").trim().toLowerCase();
}

export function isGrokImageModel(model?: string | null) {
  const id = norm(model);
  return id.startsWith("grok") && (id.includes("image") || id.includes("imagine"));
}

export function isCnImageModel(model?: string | null) {
  const id = norm(model);
  return id.startsWith("doubao") || (ZHITU360_IMAGE_MODELS as readonly string[]).includes(id);
}

/** Only the ChatGPT pool accepts reference images (图生图). */
export function supportsImageEdits(model?: string | null) {
  return !isGrokImageModel(model) && !isCnImageModel(model);
}

export function imageEditsUnsupportedMessage(model?: string | null) {
  const name = isGrokImageModel(model) ? "Grok 本地池" : "豆包 / 360智图";
  return `${name}不支持图生图，请先移除参考图或切回 ${DEFAULT_IMAGE_MODEL}`;
}
