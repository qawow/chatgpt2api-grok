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

### 独立路径（强制 Grok 池）

```bash
curl -s http://127.0.0.1:8000/v1/grok/images/generations \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"prompt":"a red cube","n":1}'
```

响应形状与 OpenAI Images 一致：`{created, data:[{b64_json|url}]}`。

### 上游说明

Build 通道在本地代码中**只稳定验证过** `/v1/responses` 与 `/v1/models`。  
生图会依次尝试：

1. `POST {base_url}/images/generations`
2. 若 `/models` 出现 image 类 id，再试 `/responses`

若全部失败 → **502**，错误信息标明 attempts；**不会**回落到 ChatGPT 号池。

## 文本（Grok 专用路径）

```bash
curl -s http://127.0.0.1:8000/v1/grok/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{
    "model": "grok-4.5",
    "messages": [{"role":"user","content":"Reply exactly: OK"}]
  }'
```

内部翻译为 Build `POST /responses`（`input` / `max_output_tokens`）。  
`/v1/chat/completions` **默认仍只走 ChatGPT**（本阶段不按 model 抢 Grok 文本，避免串路由）。

## 模型列表

- `GET /v1/models`：有 Grok 号时注入 `grok-2-image*` / `grok-imagine` / `grok-4.5`（`owned_by: grok`）
- `GET /v1/grok/models`：仅 Grok 侧

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

## 与 grokcli2api-go 同步

设置页 **GrokCLI2API** 可将本地 Grok 号池推送到
[Futureppo/grokcli2api-go](https://github.com/Futureppo/grokcli2api-go) 的
`/v1/admin/credentials`。详见 [g2a-bridge.md](./g2a-bridge.md)。

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
| `services/g2a_service.py` / `api/g2a.py` | grokcli2api-go 桥 |
