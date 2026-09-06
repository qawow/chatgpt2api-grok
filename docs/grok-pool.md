# Grok 号池（与 ChatGPT 号池隔离）

chatgpt2api 内的 **并行 Grok/xAI Build 号池**。与 `/api/accounts` / `data/accounts.json` **完全分离**。

## 隔离

| | ChatGPT | Grok |
|---|---|---|
| 存储 | `data/accounts.json` | `data/grok_accounts.json` |
| 管理 API | `/api/accounts*` | `/api/grok/accounts*` |
| Service | `account_service` | `grok_account_service` |
| 上游 | ChatGPT web / Codex | `cli-chat-proxy.grok.com` |
| 刷新 | OpenAI OAuth | `auth.x.ai/oauth2/token` |

**禁止**把 `type=xai` 的 cliproxy 文件导入 `/api/accounts`。

## 账号格式（cliproxyapi_auth）

```json
{
  "type": "xai",
  "auth_kind": "oauth",
  "email": "user@example.com",
  "access_token": "...",
  "refresh_token": "...",
  "id_token": "...",
  "base_url": "https://cli-chat-proxy.grok.com/v1",
  "token_endpoint": "https://auth.x.ai/oauth2/token",
  "headers": {
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-version": "0.2.93",
    "x-grok-client-identifier": "grok-shell"
  }
}
```

`base_url` 若含 `api.x.ai` 会强制改回 cli-chat-proxy（免费 Build 额度通道）。

## Web 号池管理

管理后台 **号池管理** 页顶部有 **ChatGPT / Grok** 切换：

- ChatGPT → `/api/accounts*`、`data/accounts.json`
- Grok → `/api/grok/accounts*`、`data/grok_accounts.json`

Grok 页支持列表、导入 cliproxy JSON、刷新（同步 OAuth refresh + 探活）、编辑状态/代理、删除。  
**不支持** ChatGPT 的密码重登 / OAuth 网页登录。

`data/grok_accounts.json` 为运行时密钥文件，**禁止提交 git**。

## 管理 API（admin Bearer）

```bash
# 列表
curl -s http://127.0.0.1:8000/api/grok/accounts \
  -H "Authorization: Bearer $KEY"

# 导入
curl -s -X POST http://127.0.0.1:8000/api/grok/accounts \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"accounts":[ { ...cliproxy json... } ]}'

# 目录批量导入
python scripts/import_grok_cliproxy_auth.py \
  --dir /path/to/cliproxyapi_auth \
  --base-url http://127.0.0.1:8000 \
  --auth-key "$KEY"

# 刷新 + 探活（POST /responses）
curl -s -X POST http://127.0.0.1:8000/api/grok/accounts/refresh \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"access_tokens":[]}'

# 更新
curl -s -X POST http://127.0.0.1:8000/api/grok/accounts/update \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"access_token":"...","status":"禁用"}'

# 删除
curl -s -X DELETE http://127.0.0.1:8000/api/grok/accounts \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"tokens":["..."]}'
```

## 生图（OpenAI 官方格式）

### 按 model 分流（共用 `/v1/images/generations`）

```bash
curl -s http://127.0.0.1:8000/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{
    "prompt": "a red cube on white background",
    "model": "grok-2-image",
    "n": 1,
    "response_format": "b64_json"
  }'
```

识别为 Grok 的 model：`grok-2-image`、`grok-2-image-1212`、`grok-imagine`，以及 `grok*` 且含 `image`/`imagine` 的 id。  
`gpt-image-2` / `codex-gpt-image-2` **仍只走 ChatGPT 池**。

生图只走本地 `data/grok_accounts.json` 免费 Build（`/responses` + `image_generation` tool），**永不**落入 ChatGPT 号池。

### 独立路径（强制 Grok 路径）

```bash
curl -s http://127.0.0.1:8000/v1/grok/images/generations \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"prompt":"a red cube","n":1}'
```

响应形状与 OpenAI Images 一致：`{created, data:[{b64_json|url}]}`。

### Web 文生图页 / 任务队列

前端默认走 `POST /api/image-tasks/generations`（不是 `/v1/images/generations`）。  
任务层 `image_task_service.route_image_generation` 会按 model 分流：

- `grok-2-image` / `grok-imagine` 等 → Grok 本地池
- 其它 → ChatGPT `IMAGE_MODELS` 白名单

若看到 `unsupported image model, supported models: gpt-image-2, codex-...`，说明请求仍进了 ChatGPT 校验（旧进程或未分流）；重启服务后选 Grok 模型即可。

### 上游说明

型号不要混：

| id | 类型 | 本代理 |
|---|---|---|
| `grok-4.5` | **对话**（[Grok 4.5](https://x.ai/news/grok-4-5)） | **不**出现在 `/v1/models`；仅内部探活 / 免费 Build agent |
| `grok-imagine-image` | **生图**（xAI SDK `client.image.sample`） | 对外生图 id |
| `grok-2-image` / `grok-2-image-1212` | 旧版 Flux 生图 | 对外生图 id；免费 Build 的 `/responses` 上会 Model not found |
| `grok-imagine` | 别名 | 路由到生图 |

Build 免费通道（`cli-chat-proxy.grok.com`）对话 agent 通常是 `grok-4.5`（上游 `grok-4.5-build-free`）。  
`POST /images/generations` 对免费号常见 `403 personal-team-blocked:spending-limit`。

**免费生图**不是「用 grok-4.5 当生图模型」，而是让对话 agent 调工具：

```http
POST {base_url}/responses
{
  "model": "grok-4.5",
  "input": "<prompt>",
  "tools": [{"type": "image_generation"}]
}
```

不要传 OpenAI 风格的 `tool_choice` 对象——免费 Build 会 422 `ModelToolChoice`。

成功时 `output[]` 含 `type=image_generation_call`，图片在 `result` 字段（JPEG/PNG 的 base64）。  
本代理会把它归一成 OpenAI Images 形状 `{created, data:[{b64_json}]}`。

本地 `generate_image` 尝试顺序：

1. **免费** `POST /responses` + `tools=image_generation`（对话 agent `grok-4.5`，不是生图 catalog id）
2. 付费 `POST /images/generations`（`grok-imagine-image` / `grok-2-image`）
3. 若 `/models` 出现 image 类 id，再试裸 `/responses`

若全部失败 → **502**，错误信息标明 attempts；**不会**回落到 ChatGPT 号池。

账号建议带 `proxy`（SOCKS5）；`requests` 走 SOCKS 需要环境里有 `PySocks`（`pyproject.toml` 已声明）。

### 凭证过期与自动刷新

Build access token 通常约 6 小时有效。`expired` 过期后上游会返回：

```text
HTTP 401 ... Invalid or expired credentials ... reason=no auth context
```

本侧处理：

1. **选号时自动刷新**：`get_next_account` 在 token 已过期或距过期 ≤5 分钟时，用 `refresh_token` 调 `auth.x.ai/oauth2/token` 换新 access token  
2. **401 再试一次**：本地生图若仍 401/403，会强制 `ensure_fresh_account` 后重试该号一次  
3. **号池管理 → 刷新**：手动批量 refresh + probe（`POST /api/grok/accounts/refresh`）

若 refresh_token 也失效，需重新导入 cliproxy OAuth 凭证。

## 文本

Grok 文本模型已关闭：`POST /v1/grok/chat/completions` 返回 400。号池探活仍可内部使用 `probe_model`（默认 grok-4.5），不对外暴露。

## 模型列表

- `GET /v1/models`：本地 Grok 号池非空时注入 `grok-2-image*` / `grok-imagine-image` / `grok-imagine`（`owned_by: grok`），**不含对话模型 grok-4.5**
- `GET /v1/grok/models`：同上，仅生图 id

## 配置（可选 `config.json`）

```json
"grok": {
  "base_url": "https://cli-chat-proxy.grok.com/v1",
  "client_id": "b1a00492-073a-47ea-816f-4c329264a828",
  "token_endpoint": "https://auth.x.ai/oauth2/token",
  "probe_model": "grok-4.5"
}
```

环境变量：`GROK_BASE_URL`、`GROK_CLIENT_ID`。

运维 / 调用总册：[operations.md](./operations.md)。  
GPT free 注册（写入 ChatGPT 池，与 Grok 无关）：[gpt-register.md](./gpt-register.md)。

## 代码入口

| 路径 | 作用 |
|---|---|
| `services/grok_account_service.py` | 号池 |
| `services/grok_backend_api.py` | 上游客户端 |
| `api/grok_accounts.py` | 管理 API |
| `services/protocol/grok_v1_image_generations.py` | 生图 |
| `services/protocol/grok_v1_chat.py` | 文本 |
| `utils/grok_models.py` | 模型判定 |
| `scripts/import_grok_cliproxy_auth.py` | 目录导入 |
