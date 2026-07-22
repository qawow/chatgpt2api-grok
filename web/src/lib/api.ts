import { httpRequest, request } from "@/lib/request";

export type AccountType = string;
export type AccountStatus = "正常" | "限流" | "异常" | "禁用";
export type ImageModel = string;
export type AuthRole = "admin" | "user";
export type ImageStorageMode = "local" | "webdav" | "both";

export type ImageStorageSettings = {
  enabled: boolean;
  mode: ImageStorageMode;
  webdav_url: string;
  webdav_username: string;
  webdav_password: string;
  webdav_root_path: string;
  public_base_url: string;
};

export type AccountPoolProvider = "chatgpt" | "grok" | "g2a";

export type Account = {
  access_token: string;
  type: AccountType;
  source_type?: string | null;
  status: AccountStatus;
  quota: number;
  email?: string | null;
  user_id?: string | null;
  limits_progress?: Array<{
    feature_name?: string;
    remaining?: number;
    reset_after?: string;
  }>;
  default_model_slug?: string | null;
  restore_at?: string | null;
  success: number;
  fail: number;
  /** 当前图片在途数(正在生成、尚未结束的图片数)。号池空闲时持续 > 0 表示并发槽位泄漏。 */
  image_inflight?: number;
  last_used_at?: string | null;
  proxy?: string | null;
  /** Grok 号池字段 */
  provider?: "grok" | "g2a" | string | null;
  remaining_tokens?: number | null;
  limit_tokens?: number | null;
  base_url?: string | null;
  expired?: string | null;
  last_refresh?: string | null;
  last_error?: string | null;
  created_at?: string | null;
  account_id?: string | null;
  /** grokcli2api-go 远程脱敏状态字段（只读） */
  g2a_server_id?: string | null;
  g2a_server_name?: string | null;
  g2a_credential_id?: string | null;
  readonly?: boolean;
  remote?: boolean;
};

export type AccountImportPayload = {
  access_token: string;
  accessToken?: string;
  type?: string;
  export_type?: string;
  source_type?: string;
  [key: string]: unknown;
};

export type Model = {
  id: string;
  object: string;
  created: number;
  owned_by: string;
  permission: unknown[];
  root: string;
  parent: string | null;
};

type AccountListResponse = {
  items: Account[];
};

type ModelListResponse = {
  object: string;
  data: Model[];
};

type AccountMutationResponse = {
  items: Account[];
  added?: number;
  skipped?: number;
  removed?: number;
  refreshed?: number;
  relogined?: number;
  errors?: Array<{ access_token: string; error: string }>;
};

export type AccountRefreshResponse = {
  items: Account[];
  refreshed: number;
  relogined?: number;
  errors: Array<{ access_token: string; error: string }>;
};

export type RefreshProgressResponse = {
  total: number;
  processed: number;
  done: boolean;
  error: string | null;
  status_counts?: Record<string, number>;
  total_quota?: number;
  result?: AccountRefreshResponse | null;
  results?: Array<{ token: string; status: string; error?: string | null }>;
};

type AccountUpdateResponse = {
  item: Account;
  items: Account[];
};

export type ProxyRuntimeEgressMode = "direct" | "single_proxy";
export type ProxyRuntimeClearanceMode = "none" | "manual" | "flaresolverr";

export type ProxyRuntimeClearanceSettings = {
  enabled: boolean;
  mode: ProxyRuntimeClearanceMode;
  cf_cookies: string;
  cf_clearance: string;
  user_agent: string;
  browser: string;
  flaresolverr_url: string;
  timeout_sec: number | string;
  refresh_interval: number | string;
  warm_up_on_start: boolean;
  has_cf_cookies?: boolean;
  has_cf_clearance?: boolean;
};

export type ProxyRuntimeSettings = {
  enabled: boolean;
  egress_mode: ProxyRuntimeEgressMode;
  proxy_url: string;
  resource_proxy_url: string;
  skip_ssl_verify: boolean;
  reset_session_status_codes: number[];
  clearance: ProxyRuntimeClearanceSettings;
};

export type ProxyRuntimeStatus = {
  enabled: boolean;
  egress_mode: ProxyRuntimeEgressMode | string;
  proxy_source: string;
  has_proxy: boolean;
  clearance_enabled: boolean;
  clearance_mode: ProxyRuntimeClearanceMode | string;
  has_clearance_bundle: boolean;
  cached_clearance_hosts: string[];
};

export type ProxyRuntimeResponse = {
  runtime: ProxyRuntimeSettings;
  status: ProxyRuntimeStatus;
};

export type ThirdPartyAppsSettings = {
  infinite_canvas: {
    enabled: boolean;
    url: string;
  };
};

export type SettingsConfig = {
  proxy: string;
  base_url?: string;
  global_system_prompt?: string;
  sensitive_words?: string[];
  ai_review?: {
    enabled?: boolean;
    base_url?: string;
    api_key?: string;
    model?: string;
    prompt?: string;
  };
  refresh_account_interval_minute?: number | string;
  image_retention_days?: number | string;
  image_poll_timeout_secs?: number | string;
  image_account_concurrency?: number | string;
  image_parallel_generation?: boolean;
  image_settle_enabled?: boolean;
  image_check_before_hit_enabled?: boolean;
  image_remove_conversation_after_result?: boolean;
  image_settle_secs?: number | string;
  image_timeout_retry_secs?: number | string;
  auto_remove_invalid_accounts?: boolean;
  auto_remove_rate_limited_accounts?: boolean;
  auto_relogin_after_refresh?: boolean;
  log_levels?: string[];
  image_storage?: ImageStorageSettings;
  proxy_runtime?: ProxyRuntimeSettings;
  third_party_apps?: ThirdPartyAppsSettings;
  backup?: BackupSettings;
  backup_state?: BackupState;
  [key: string]: unknown;
};

export type BackupInclude = {
  config: boolean;
  cpa: boolean;
  sub2api: boolean;
  logs: boolean;
  image_tasks: boolean;
  accounts_snapshot: boolean;
  auth_keys_snapshot: boolean;
  images: boolean;
};

export type BackupSettings = {
  enabled: boolean;
  provider: "cloudflare_r2" | string;
  account_id: string;
  access_key_id: string;
  secret_access_key: string;
  bucket: string;
  prefix: string;
  interval_minutes: number | string;
  rotation_keep: number | string;
  encrypt: boolean;
  passphrase: string;
  include: BackupInclude;
};

export type BackupState = {
  running: boolean;
  last_started_at?: string | null;
  last_finished_at?: string | null;
  last_status?: string;
  last_error?: string | null;
  last_object_key?: string | null;
};

export type BackupItem = {
  key: string;
  name: string;
  size: number;
  updated_at?: string | null;
  encrypted: boolean;
};

export type BackupDetail = {
  key: string;
  name: string;
  encrypted: boolean;
  created_at?: string | null;
  trigger?: string | null;
  app_version?: string | null;
  storage_backend?: Record<string, unknown> | null;
  files: Array<{
    name: string;
    exists: boolean;
    content_type?: string;
    size: number;
    sha256?: string;
  }>;
  snapshots: Array<{
    name: string;
    count: number;
  }>;
};

export type ManagedImage = {
  rel: string;
  path?: string;
  name: string;
  date: string;
  size: number;
  url: string;
  thumbnail_url?: string;
  created_at: string;
  width?: number;
  height?: number;
  tags?: string[];
};

export type SystemLog = {
  id: string;
  time: string;
  type: "call" | "account" | string;
  summary?: string;
  detail?: Record<string, unknown>;
  [key: string]: unknown;
};

export type ImageResponse = {
  created: number;
  data: Array<{ b64_json?: string; url?: string; revised_prompt?: string }>;
};

export type ImageTask = {
  id: string;
  status: "queued" | "running" | "success" | "error";
  mode: "generate" | "edit";
  model?: ImageModel;
  size?: string;
  quality?: string;
  created_at: string;
  updated_at: string;
  conversation_id?: string;
  data?: Array<{ b64_json?: string; url?: string; revised_prompt?: string }>;
  error?: string;
  progress?: string;
  elapsed_secs?: number;
  duration_ms?: number;
};

type ImageTaskListResponse = {
  items: ImageTask[];
  missing_ids: string[];
};

export type LoginResponse = {
  ok: boolean;
  version: string;
  role: AuthRole;
  subject_id: string;
  name: string;
};

export type UserKey = {
  id: string;
  name: string;
  role: "user";
  enabled: boolean;
  created_at: string | null;
  last_used_at: string | null;
};

export async function login(authKey: string) {
  const normalizedAuthKey = String(authKey || "").trim();
  return httpRequest<LoginResponse>("/auth/login", {
    method: "POST",
    body: {},
    headers: {
      Authorization: `Bearer ${normalizedAuthKey}`,
    },
    redirectOnUnauthorized: false,
  });
}

export async function fetchAccounts() {
  return httpRequest<AccountListResponse>("/api/accounts");
}

/** 将 Grok 后端账号归一成号池 UI 共用的 Account 形状。 */
export function normalizeGrokAccount(item: Record<string, unknown>): Account {
  const accessToken = String(item.access_token || item.accessToken || "").trim();
  const remaining = Number(item.remaining_tokens ?? item.quota ?? 0);
  const statusRaw = String(item.status || "正常").trim();
  const status = (["正常", "限流", "异常", "禁用"].includes(statusRaw)
    ? statusRaw
    : item.disabled
      ? "禁用"
      : "正常") as AccountStatus;
  const provider =
    item.provider != null
      ? String(item.provider)
      : String(item.source_type || "") === "g2a"
        ? "g2a"
        : "grok";
  return {
    access_token: accessToken,
    type: String(item.type || (provider === "g2a" ? "g2a-remote" : "xai")),
    source_type: provider === "g2a" ? "g2a" : "grok",
    status,
    quota: Number.isFinite(remaining) ? Math.max(0, remaining) : 0,
    email: item.email != null ? String(item.email) : null,
    user_id: item.account_id != null ? String(item.account_id) : item.sub != null ? String(item.sub) : null,
    success: Number(item.success || 0),
    fail: Number(item.fail || 0),
    last_used_at: item.last_used_at != null ? String(item.last_used_at) : null,
    proxy: item.proxy != null ? String(item.proxy) : null,
    provider,
    remaining_tokens: item.remaining_tokens != null ? Number(item.remaining_tokens) : null,
    limit_tokens: item.limit_tokens != null ? Number(item.limit_tokens) : null,
    base_url: item.base_url != null ? String(item.base_url) : null,
    expired: item.expired != null ? String(item.expired) : null,
    last_refresh: item.last_refresh != null ? String(item.last_refresh) : null,
    last_error: item.last_error != null ? String(item.last_error) : null,
    created_at: item.created_at != null ? String(item.created_at) : null,
    account_id: item.account_id != null ? String(item.account_id) : null,
    restore_at: item.expired != null ? String(item.expired) : null,
    g2a_server_id: item.g2a_server_id != null ? String(item.g2a_server_id) : null,
    g2a_server_name: item.g2a_server_name != null ? String(item.g2a_server_name) : null,
    g2a_credential_id: item.g2a_credential_id != null ? String(item.g2a_credential_id) : null,
    readonly: Boolean(item.readonly ?? provider === "g2a"),
    remote: Boolean(item.remote ?? provider === "g2a"),
  };
}

/** 拉取 grokcli2api-go 远程脱敏号池状态（不含 token）。 */
export async function fetchG2APoolStatus(serverId?: string) {
  const qs = serverId ? `?server_id=${encodeURIComponent(serverId)}` : "";
  const data = await httpRequest<{
    items?: Array<Record<string, unknown>>;
    servers?: Array<Record<string, unknown>>;
    errors?: Array<{ server_id?: string; error?: string }>;
    total?: number;
    has_image_proxy?: boolean;
    note?: string;
  }>(`/api/g2a/pool${qs}`);
  return {
    items: (data.items || []).map((item) => normalizeGrokAccount(item)),
    servers: data.servers || [],
    errors: data.errors || [],
    total: data.total ?? (data.items || []).length,
    has_image_proxy: Boolean(data.has_image_proxy),
    note: data.note || "",
    provider: "g2a" as const,
  };
}

export async function fetchGrokAccounts() {
  const data = await httpRequest<{ items: Array<Record<string, unknown>>; provider?: string }>(
    "/api/grok/accounts",
  );
  return {
    items: (data.items || []).map((item) => normalizeGrokAccount(item)),
    provider: "grok" as const,
  };
}

export async function createGrokAccounts(accounts: AccountImportPayload[]) {
  const data = await httpRequest<{
    items?: Array<Record<string, unknown>>;
    added?: number;
    skipped?: number;
  }>("/api/grok/accounts", {
    method: "POST",
    body: { accounts },
  });
  return {
    ...data,
    items: (data.items || []).map((item) => normalizeGrokAccount(item)),
  };
}

export async function deleteGrokAccounts(tokens: string[]) {
  const data = await httpRequest<{
    items?: Array<Record<string, unknown>>;
    removed?: number;
  }>("/api/grok/accounts", {
    method: "DELETE",
    body: { tokens },
  });
  return {
    ...data,
    items: (data.items || []).map((item) => normalizeGrokAccount(item)),
  };
}

/** Grok 刷新是同步接口，直接返回 items（无 progress_id）。 */
export async function refreshGrokAccounts(accessTokens: string[] = []) {
  const data = await httpRequest<{
    items?: Array<Record<string, unknown>>;
    refreshed?: number;
    errors?: Array<{ access_token?: string; error?: string }>;
  }>("/api/grok/accounts/refresh", {
    method: "POST",
    body: { access_tokens: accessTokens },
  });
  return {
    refreshed: data.refreshed ?? 0,
    errors: data.errors ?? [],
    items: (data.items || []).map((item) => normalizeGrokAccount(item)),
  };
}

export async function updateGrokAccount(
  accessToken: string,
  updates: {
    status?: AccountStatus;
    disabled?: boolean;
    proxy?: string;
    base_url?: string;
  },
) {
  const data = await httpRequest<{
    item?: Record<string, unknown>;
    items?: Array<Record<string, unknown>>;
  }>("/api/grok/accounts/update", {
    method: "POST",
    body: {
      access_token: accessToken,
      ...updates,
    },
  });
  return {
    item: data.item ? normalizeGrokAccount(data.item) : null,
    items: (data.items || []).map((item) => normalizeGrokAccount(item)),
  };
}

export async function importGrokAccountFiles(
  files: Array<{ name: string; content: string | Record<string, unknown> }>,
) {
  const data = await httpRequest<{
    items?: Array<Record<string, unknown>>;
    added?: number;
    skipped?: number;
    parse_errors?: Array<{ name: string; error: string }>;
  }>("/api/grok/accounts/import-files", {
    method: "POST",
    body: { files },
  });
  return {
    ...data,
    items: (data.items || []).map((item) => normalizeGrokAccount(item)),
  };
}

export async function fetchModels() {
  return httpRequest<ModelListResponse>("/v1/models");
}

export async function createAccounts(tokens: string[], accounts: AccountImportPayload[] = []) {
  return httpRequest<AccountMutationResponse>("/api/accounts", {
    method: "POST",
    body: { tokens, accounts },
  });
}

export type OAuthLoginStartResponse = {
  session_id: string;
  authorize_url: string;
  expires_in: string;
  redirect_uri_prefix: string;
};

export async function startOAuthLogin(emailHint?: string) {
  return httpRequest<OAuthLoginStartResponse>("/api/accounts/oauth/start", {
    method: "POST",
    body: { email_hint: emailHint ?? "" },
  });
}

export async function finishOAuthLogin(sessionId: string, callback: string) {
  return httpRequest<AccountMutationResponse>("/api/accounts/oauth/finish", {
    method: "POST",
    body: { session_id: sessionId, callback },
  });
}

export async function deleteAccounts(tokens: string[]) {
  return httpRequest<AccountMutationResponse>("/api/accounts", {
    method: "DELETE",
    body: { tokens },
  });
}

export async function refreshAccounts(accessTokens: string[]) {
  return httpRequest<{ progress_id: string }>("/api/accounts/refresh", {
    method: "POST",
    body: { access_tokens: accessTokens },
  });
}

export async function fetchRefreshProgress(progressId: string) {
  return httpRequest<RefreshProgressResponse>(`/api/accounts/refresh/progress/${progressId}`);
}

export async function reLoginAccounts(accessTokens: string[]) {
  return httpRequest<{ progress_id: string }>("/api/accounts/re-login", {
    method: "POST",
    body: { access_tokens: accessTokens },
  });
}

export async function fetchReLoginProgress(progressId: string) {
  return httpRequest<RefreshProgressResponse>(`/api/accounts/re-login/progress/${progressId}`);
}

export async function updateAccount(
  accessToken: string,
  updates: {
    type?: AccountType;
    status?: AccountStatus;
    quota?: number;
    proxy?: string;
  },
) {
  return httpRequest<AccountUpdateResponse>("/api/accounts/update", {
    method: "POST",
    body: {
      access_token: accessToken,
      ...updates,
    },
  });
}

export async function generateImage(prompt: string, model?: ImageModel, size?: string, quality = "auto") {
  return httpRequest<ImageResponse>(
    "/v1/images/generations",
    {
      method: "POST",
      body: {
        prompt,
        ...(model ? { model } : {}),
        ...(size ? { size } : {}),
        quality,
        n: 1,
        response_format: "b64_json",
      },
    },
  );
}

export async function editImage(files: File | File[], prompt: string, model?: ImageModel, size?: string, quality = "auto") {
  const formData = new FormData();
  const uploadFiles = Array.isArray(files) ? files : [files];

  uploadFiles.forEach((file) => {
    formData.append("image", file);
  });
  formData.append("prompt", prompt);
  if (model) {
    formData.append("model", model);
  }
  if (size) {
    formData.append("size", size);
  }
  formData.append("quality", quality);
  formData.append("n", "1");

  return httpRequest<ImageResponse>(
    "/v1/images/edits",
    {
      method: "POST",
      body: formData,
    },
  );
}

export async function createImageGenerationTask(clientTaskId: string, prompt: string, model?: ImageModel, size?: string, quality = "auto") {
  return httpRequest<ImageTask>("/api/image-tasks/generations", {
    method: "POST",
    body: {
      client_task_id: clientTaskId,
      prompt,
      ...(model ? { model } : {}),
      ...(size ? { size } : {}),
      quality,
    },
  });
}

export async function createImageEditTask(
  clientTaskId: string,
  files: File | File[],
  prompt: string,
  model?: ImageModel,
  size?: string,
  quality = "auto",
) {
  const formData = new FormData();
  const uploadFiles = Array.isArray(files) ? files : [files];

  uploadFiles.forEach((file) => {
    formData.append("image", file);
  });
  formData.append("client_task_id", clientTaskId);
  formData.append("prompt", prompt);
  if (model) {
    formData.append("model", model);
  }
  if (size) {
    formData.append("size", size);
  }
  formData.append("quality", quality);

  return httpRequest<ImageTask>("/api/image-tasks/edits", {
    method: "POST",
    body: formData,
  });
}

export async function fetchImageTasks(ids: string[]) {
  const params = new URLSearchParams();
  if (ids.length > 0) {
    params.set("ids", ids.join(","));
  }
  params.set("_t", String(Date.now()));
  return httpRequest<ImageTaskListResponse>(`/api/image-tasks?${params.toString()}`);
}

export async function resumeImagePoll(taskId: string, extraTimeoutSecs = 30) {
  return httpRequest<ImageTask>(`/api/image-tasks/${encodeURIComponent(taskId)}/resume-poll`, {
    method: "POST",
    body: { extra_timeout_secs: extraTimeoutSecs },
  });
}

export async function fetchSettingsConfig() {
  return httpRequest<{ config: SettingsConfig }>("/api/settings");
}

export async function updateSettingsConfig(settings: SettingsConfig) {
  return httpRequest<{ config: SettingsConfig }>("/api/settings", {
    method: "POST",
    body: settings,
  });
}

export async function fetchThirdPartyApps() {
  return httpRequest<{ third_party_apps: ThirdPartyAppsSettings }>("/api/third-party-apps");
}

export async function testBackupConnection() {
  return httpRequest<{ result: { ok: boolean; status: number } }>("/api/backup/test", {
    method: "POST",
    body: {},
  });
}

export async function testImageStorageConnection() {
  return httpRequest<{ result: { ok: boolean; status: number; error?: string } }>("/api/image-storage/test", {
    method: "POST",
    body: {},
  });
}

export async function syncImageStorage() {
  return httpRequest<{ result: { uploaded: number; skipped: number; failed: number } }>("/api/image-storage/sync", {
    method: "POST",
    body: {},
  });
}

export async function fetchBackups() {
  return httpRequest<{ items: BackupItem[]; state: BackupState; settings: BackupSettings }>("/api/backups");
}

export async function runBackupNow() {
  return httpRequest<{ result: { key: string; size: number; encrypted: boolean } }>("/api/backups/run", {
    method: "POST",
    body: {},
  });
}

export async function deleteBackup(key: string) {
  return httpRequest<{ ok: boolean }>("/api/backups/delete", {
    method: "POST",
    body: { key },
  });
}

export async function fetchBackupDetail(key: string) {
  const params = new URLSearchParams();
  params.set("key", key);
  return httpRequest<{ item: BackupDetail }>(`/api/backups/detail?${params.toString()}`);
}

export function getBackupDownloadUrl(key: string) {
  const params = new URLSearchParams();
  params.set("key", key);
  return `/api/backups/download?${params.toString()}`;
}

export async function fetchManagedImages(filters: { start_date?: string; end_date?: string }) {
  const params = new URLSearchParams();
  if (filters.start_date) params.set("start_date", filters.start_date);
  if (filters.end_date) params.set("end_date", filters.end_date);
  return httpRequest<{ items: ManagedImage[]; groups: Array<{ date: string; items: ManagedImage[] }> }>(
    `/api/images${params.toString() ? `?${params.toString()}` : ""}`,
  );
}

export async function deleteManagedImages(body: { paths?: string[]; start_date?: string; end_date?: string; all_matching?: boolean }) {
  return httpRequest<{ removed: number }>("/api/images/delete", { method: "POST", body });
}

export async function downloadImages(paths: string[]) {
  const response = await request.post("/api/images/download", { paths }, { responseType: "blob" });
  const blob = response.data as Blob;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "images.zip";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export async function downloadSingleImage(path: string) {
  const response = await request.get(`/api/images/download/${path}`, { responseType: "blob" });
  const blob = response.data as Blob;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = path.split("/").pop() || "image.png";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export async function fetchImageTags() {
  return httpRequest<{ tags: string[] }>("/api/images/tags");
}

export async function setImageTags(path: string, tags: string[]) {
  return httpRequest<{ ok: boolean; tags: string[] }>("/api/images/tags", {
    method: "POST",
    body: { path, tags },
  });
}

export async function deleteImageTag(tag: string) {
  return httpRequest<{ ok: boolean; removed_from: number }>(`/api/images/tags/${encodeURIComponent(tag)}`, {
    method: "DELETE",
  });
}

export type ImageStorageStats = {
  disk_total_mb: number; disk_used_mb: number; disk_free_mb: number;
  image_count: number; image_size_mb: number; image_size_bytes: number;
};

export async function fetchImageStorage() {
  return httpRequest<ImageStorageStats>("/api/images/storage");
}

export async function compressAllImages() {
  return httpRequest<{ compressed: number; saved_bytes: number; saved_mb: number }>("/api/images/storage/compress", { method: "POST" });
}

export async function deleteToTarget(targetFreeMb: number) {
  return httpRequest<{ removed: number; freed_mb: number; done: boolean }>(
    `/api/images/storage/cleanup-to-target?target_free_mb=${targetFreeMb}&dry_run=false`,
    { method: "POST" },
  );
}

export async function fetchSystemLogs(filters: { type?: string; start_date?: string; end_date?: string }) {
  const params = new URLSearchParams();
  if (filters.type) params.set("type", filters.type);
  if (filters.start_date) params.set("start_date", filters.start_date);
  if (filters.end_date) params.set("end_date", filters.end_date);
  return httpRequest<{ items: SystemLog[] }>(`/api/logs${params.toString() ? `?${params.toString()}` : ""}`);
}

export async function deleteSystemLogs(ids: string[]) {
  return httpRequest<{ removed: number }>("/api/logs/delete", {
    method: "POST",
    body: { ids },
  });
}

export async function fetchUserKeys() {
  return httpRequest<{ items: UserKey[] }>("/api/auth/users");
}

export async function createUserKey(name: string) {
  return httpRequest<{ item: UserKey; key: string; items: UserKey[] }>("/api/auth/users", {
    method: "POST",
    body: { name },
  });
}

export async function updateUserKey(keyId: string, updates: { enabled?: boolean; name?: string; key?: string }) {
  return httpRequest<{ item: UserKey; items: UserKey[] }>(`/api/auth/users/${keyId}`, {
    method: "POST",
    body: updates,
  });
}

export async function deleteUserKey(keyId: string) {
  return httpRequest<{ items: UserKey[] }>(`/api/auth/users/${keyId}`, {
    method: "DELETE",
  });
}

// ── CPA (CLIProxyAPI) ──────────────────────────────────────────────

export type CPAPool = {
  id: string;
  name: string;
  base_url: string;
  import_job?: CPAImportJob | null;
};

export type CPARemoteFile = {
  name: string;
  email: string;
};

export type CPAImportJob = {
  job_id: string;
  status: "pending" | "running" | "completed" | "failed";
  created_at: string;
  updated_at: string;
  total: number;
  completed: number;
  added: number;
  skipped: number;
  refreshed: number;
  failed: number;
  errors: Array<{ name: string; error: string }>;
};

export async function fetchCPAPools() {
  return httpRequest<{ pools: CPAPool[] }>("/api/cpa/pools");
}

export async function createCPAPool(pool: { name: string; base_url: string; secret_key: string }) {
  return httpRequest<{ pool: CPAPool; pools: CPAPool[] }>("/api/cpa/pools", {
    method: "POST",
    body: pool,
  });
}

export async function updateCPAPool(
  poolId: string,
  updates: { name?: string; base_url?: string; secret_key?: string },
) {
  return httpRequest<{ pool: CPAPool; pools: CPAPool[] }>(`/api/cpa/pools/${poolId}`, {
    method: "POST",
    body: updates,
  });
}

export async function deleteCPAPool(poolId: string) {
  return httpRequest<{ pools: CPAPool[] }>(`/api/cpa/pools/${poolId}`, {
    method: "DELETE",
  });
}

export async function fetchCPAPoolFiles(poolId: string) {
  return httpRequest<{ pool_id: string; files: CPARemoteFile[] }>(`/api/cpa/pools/${poolId}/files`);
}

export async function startCPAImport(poolId: string, names: string[]) {
  return httpRequest<{ import_job: CPAImportJob | null }>(`/api/cpa/pools/${poolId}/import`, {
    method: "POST",
    body: { names },
  });
}

export async function fetchCPAPoolImportJob(poolId: string) {
  return httpRequest<{ import_job: CPAImportJob | null }>(`/api/cpa/pools/${poolId}/import`);
}

// ── Sub2API ────────────────────────────────────────────────────────

export type Sub2APIServer = {
  id: string;
  name: string;
  base_url: string;
  email: string;
  has_api_key: boolean;
  group_id: string;
  import_job?: CPAImportJob | null;
};

export type Sub2APIRemoteAccount = {
  id: string;
  name: string;
  email: string;
  plan_type: string;
  status: string;
  expires_at: string;
  has_refresh_token: boolean;
};

export type Sub2APIRemoteGroup = {
  id: string;
  name: string;
  description: string;
  platform: string;
  status: string;
  account_count: number;
  active_account_count: number;
};

export async function fetchSub2APIServers() {
  return httpRequest<{ servers: Sub2APIServer[] }>("/api/sub2api/servers");
}

export async function createSub2APIServer(server: {
  name: string;
  base_url: string;
  email: string;
  password: string;
  api_key: string;
  group_id: string;
}) {
  return httpRequest<{ server: Sub2APIServer; servers: Sub2APIServer[] }>("/api/sub2api/servers", {
    method: "POST",
    body: server,
  });
}

export async function updateSub2APIServer(
  serverId: string,
  updates: {
    name?: string;
    base_url?: string;
    email?: string;
    password?: string;
    api_key?: string;
    group_id?: string;
  },
) {
  return httpRequest<{ server: Sub2APIServer; servers: Sub2APIServer[] }>(`/api/sub2api/servers/${serverId}`, {
    method: "POST",
    body: updates,
  });
}

export async function fetchSub2APIServerGroups(serverId: string) {
  return httpRequest<{ server_id: string; groups: Sub2APIRemoteGroup[] }>(
    `/api/sub2api/servers/${serverId}/groups`,
  );
}

export async function deleteSub2APIServer(serverId: string) {
  return httpRequest<{ servers: Sub2APIServer[] }>(`/api/sub2api/servers/${serverId}`, {
    method: "DELETE",
  });
}

export async function fetchSub2APIServerAccounts(serverId: string) {
  return httpRequest<{ server_id: string; accounts: Sub2APIRemoteAccount[] }>(
    `/api/sub2api/servers/${serverId}/accounts`,
  );
}

export async function startSub2APIImport(serverId: string, accountIds: string[]) {
  return httpRequest<{ import_job: CPAImportJob | null }>(`/api/sub2api/servers/${serverId}/import`, {
    method: "POST",
    body: { account_ids: accountIds },
  });
}

export async function fetchSub2APIImportJob(serverId: string) {
  return httpRequest<{ import_job: CPAImportJob | null }>(`/api/sub2api/servers/${serverId}/import`);
}

// ── Upstream proxy ────────────────────────────────────────────────

export type ProxySettings = {
  enabled: boolean;
  url: string;
};

export type ProxyTestResult = {
  ok: boolean;
  status: number;
  latency_ms: number;
  error: string | null;
  proxy_source?: string;
  has_proxy?: boolean;
};

export type ClearanceTestResult = {
  ok: boolean;
  status: string;
  latency_ms: number;
  has_cookies: boolean;
  user_agent: string;
  error: string | null;
  runtime: ProxyRuntimeStatus;
};

export async function fetchProxy() {
  return httpRequest<{ proxy: ProxySettings }>("/api/proxy");
}

export async function updateProxy(updates: { enabled?: boolean; url?: string }) {
  return httpRequest<{ proxy: ProxySettings }>("/api/proxy", {
    method: "POST",
    body: updates,
  });
}

export async function testProxy(url?: string) {
  return httpRequest<{ result: ProxyTestResult }>("/api/proxy/test", {
    method: "POST",
    body: { url: url ?? "" },
  });
}

export async function fetchProxyRuntime() {
  return httpRequest<ProxyRuntimeResponse>("/api/proxy/runtime");
}

export async function updateProxyRuntime(runtime: ProxyRuntimeSettings) {
  return httpRequest<ProxyRuntimeResponse>("/api/proxy/runtime", {
    method: "POST",
    body: runtime,
  });
}

export async function testProxyClearance(targetUrl?: string) {
  return httpRequest<{ result: ClearanceTestResult }>("/api/proxy/clearance/test", {
    method: "POST",
    body: { target_url: targetUrl ?? "https://chatgpt.com" },
  });
}

// ── GrokCLI2API-Go (Futureppo) ──────────────────────────────────

export type G2AServer = {
  id: string;
  name: string;
  base_url: string;
  has_admin_key: boolean;
  has_api_key?: boolean;
  can_proxy_image?: boolean;
  prefer_for_image?: boolean;
  enabled?: boolean;
  note?: string;
  /** Optional outbound proxy for admin calls only; empty = direct (no env proxy). */
  proxy?: string;
  last_error?: string | null;
  last_ok_at?: string | null;
  created_at?: string;
  updated_at?: string;
};

export type G2ARemoteCredential = {
  id: string;
  email?: string | null;
  disabled?: boolean;
  status?: string | null;
  type?: string | null;
  scopes?: string[] | null;
  model_discovery?: unknown;
};

export async function fetchG2AServers() {
  return httpRequest<{ servers: G2AServer[] }>("/api/g2a/servers");
}

export async function createG2AServer(server: {
  name: string;
  base_url: string;
  admin_key: string;
  api_key?: string;
  note?: string;
  proxy?: string;
  prefer_for_image?: boolean;
}) {
  return httpRequest<{ server: G2AServer; servers: G2AServer[] }>("/api/g2a/servers", {
    method: "POST",
    body: server,
  });
}

export async function updateG2AServer(
  serverId: string,
  updates: {
    name?: string;
    base_url?: string;
    admin_key?: string;
    api_key?: string;
    note?: string;
    enabled?: boolean;
    proxy?: string;
    prefer_for_image?: boolean;
  },
) {
  return httpRequest<{ server: G2AServer; servers: G2AServer[] }>(`/api/g2a/servers/${serverId}`, {
    method: "POST",
    body: updates,
  });
}

export async function deleteG2AServer(serverId: string) {
  return httpRequest<{ servers: G2AServer[] }>(`/api/g2a/servers/${serverId}`, {
    method: "DELETE",
  });
}

export async function pingG2AServer(serverId: string) {
  return httpRequest<{ ok: boolean; count?: number; servers: G2AServer[] }>(
    `/api/g2a/servers/${serverId}/ping`,
    { method: "POST", body: {} },
  );
}

export async function fetchG2ACredentials(serverId: string) {
  return httpRequest<{ server_id: string; items: G2ARemoteCredential[]; servers: G2AServer[] }>(
    `/api/g2a/servers/${serverId}/credentials`,
  );
}

export async function pushLocalGrokToG2A(serverId: string, accessTokens: string[] = []) {
  return httpRequest<{
    total: number;
    pushed: number;
    failed: number;
    errors: Array<{ email?: string; error?: string }>;
    servers: G2AServer[];
  }>(`/api/g2a/servers/${serverId}/push`, {
    method: "POST",
    body: { access_tokens: accessTokens },
  });
}

export async function deleteG2ACredential(serverId: string, credentialId: string) {
  return httpRequest<{ ok: boolean }>(`/api/g2a/servers/${serverId}/credentials/${credentialId}`, {
    method: "DELETE",
  });
}

// ── GPT Free Register (any-register-engines) ──────────────────────

export type GptRegisterSettings = {
  engines_dir: string;
  python_bin: string;
  run_mode?: string;
  count: number;
  concurrency: number;
  interval_secs: number;
  timeout_secs: number;
  executor: string;
  mail_provider: string;
  captcha: string;
  proxy: string;
  bind_register_proxy: boolean;
  plan_type: string;
  source_type: string;
  cfd1_domain: string;
  push_enabled: boolean;
  push_mode: string;
  chatgpt2api_base_url: string;
  chatgpt2api_auth_key?: string;
  has_chatgpt2api_auth_key?: boolean;
  dry_run: boolean;
  /** 默认 true：跳过 Codex 二次 OTP（free 号几乎总是 add_phone 失败） */
  skip_codex?: boolean;
  /** 关闭步骤间随机抖动（OPENAI_REGISTER_NO_DELAY） */
  register_no_delay?: boolean;
  /** 覆盖 OPENAI_SO_COLLECT_MS；空=引擎默认 */
  so_collect_ms?: string;
};

export type GptRegisterJobLog = {
  at?: string;
  level?: string;
  message?: string;
};

export type GptRegisterJobItem = {
  index?: number;
  ok?: boolean;
  email?: string | null;
  error?: string | null;
  added?: number;
  has_token?: boolean;
  push?: unknown;
  mode?: string;
  logs_tail?: string[];
};

export type GptRegisterJobSummary = {
  status?: string;
  total?: number;
  completed?: number;
  success?: number;
  failed?: number;
  added?: number;
  duration_secs?: number;
  success_rate?: number;
  success_emails?: string[];
  failed_items?: Array<{ index?: number; email?: string | null; error?: string }>;
  run_mode?: string;
  mail_provider?: string;
  executor?: string;
  engines_dir?: string;
  push_mode?: string;
  push_enabled?: boolean;
  error?: string | null;
};

export type GptRegisterJob = {
  job_id: string;
  status: string;
  created_at?: string;
  updated_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  settings?: Partial<GptRegisterSettings>;
  total: number;
  completed: number;
  success: number;
  failed: number;
  added: number;
  items?: GptRegisterJobItem[];
  logs?: GptRegisterJobLog[];
  summary?: GptRegisterJobSummary;
  error?: string | null;
  cancel_requested?: boolean;
};

export async function fetchGptRegisterSettings() {
  return httpRequest<{ settings: GptRegisterSettings }>("/api/gpt-register/settings");
}

export async function saveGptRegisterSettings(settings: Partial<GptRegisterSettings>) {
  return httpRequest<{ settings: GptRegisterSettings }>("/api/gpt-register/settings", {
    method: "POST",
    body: settings,
  });
}

export async function fetchGptRegisterJobs() {
  return httpRequest<{ jobs: GptRegisterJob[] }>("/api/gpt-register/jobs");
}

export async function fetchGptRegisterJob(jobId: string) {
  return httpRequest<{ job: GptRegisterJob }>(`/api/gpt-register/jobs/${jobId}`);
}

export async function startGptRegisterJob(overrides: Partial<GptRegisterSettings> = {}) {
  return httpRequest<{ job: GptRegisterJob }>("/api/gpt-register/start", {
    method: "POST",
    body: overrides,
  });
}

export async function cancelGptRegisterJob(jobId: string) {
  return httpRequest<{ job: GptRegisterJob }>(`/api/gpt-register/jobs/${jobId}/cancel`, {
    method: "POST",
    body: {},
  });
}
