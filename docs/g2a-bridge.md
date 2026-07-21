# grokcli2api-go 接入（设置页）

在 chatgpt2api **设置 → GrokCLI2API** 中对接
[Futureppo/grokcli2api-go](https://github.com/Futureppo/grokcli2api-go)。

## 远程接口（对方）

需在 grokcli2api-go 配置 `GROK_ADMIN_KEY`，管理接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/admin/credentials` | 脱敏列表（**无 token**） |
| POST | `/v1/admin/credentials` | 上传/覆盖 OAuth JSON（body 或 multipart `file`） |
| DELETE | `/v1/admin/credentials/{id}` | 按脱敏 id 删除 |

OpenAI 兼容客户端接口（Bearer = API Key 或 Admin Key）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/images/generations` | 生图（远程自有号池轮转） |
| POST | `/v1/chat/completions` | 文本 |
| GET | `/v1/models` | 模型列表 |

鉴权：`Authorization: Bearer <KEY>`；管理接口另支持 `X-Admin-Key`。

默认端口：`8088`。

## 本侧接口

配置存 `data/g2a_config.json`（`admin_key` / `api_key` 不回显，仅 `has_admin_key` / `has_api_key` / `can_proxy_image`）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST/DELETE | `/api/g2a/servers` | 连接 CRUD |
| POST | `/api/g2a/servers/{id}` | 更新（含 `api_key`、`prefer_for_image`） |
| POST | `/api/g2a/servers/{id}/ping` | 连通探测 |
| GET | `/api/g2a/servers/{id}/credentials` | 远程脱敏列表 |
| GET | `/api/g2a/pool` | 聚合远程号池状态（号池管理 UI） |
| POST | `/api/g2a/servers/{id}/push` | 推送本地 Grok 号池 |
| DELETE | `/api/g2a/servers/{id}/credentials/{cid}` | 删远程凭证 |

均需 chatgpt2api **管理员** Bearer。

连接字段：

| 字段 | 说明 |
|---|---|
| `base_url` | 服务根，如 `http://host:8088`（自动去尾 `/v1`） |
| `admin_key` | 远程 `GROK_ADMIN_KEY`，管理接口必填 |
| `api_key` | 可选；OpenAI 兼容生图用。空则回退 `admin_key` |
| `prefer_for_image` | 默认 `true`：Grok 生图优先走该远程 |
| `proxy` | 可选出站代理；空=直连（忽略环境代理） |

## 数据方向（重要）

```
本地 Grok 号池 (data/grok_accounts.json)
        │  push（可选）
        ▼
grokcli2api-go  /v1/admin/credentials  →  auths/

chatgpt2api Grok 生图
        │  proxy（prefer_for_image）
        ▼
grokcli2api-go  POST /v1/images/generations
        （远程自己轮转号池；本机无需本地 token）

号池管理「GrokCLI2API」页
        │  status only
        ▼
GET /api/g2a/pool ← 远程脱敏 credentials
```

远程列表**不含** access/refresh token，因此：

- ✅ 本地 → 远程上传
- ✅ 远程脱敏列表 / 删除
- ✅ **生图直连远程**（号池可完全留在 grokcli2api-go）
- ✅ 号池管理只读镜像远程状态
- ❌ 远程 → 本地拉 token（做不到，对方设计如此）

推送体为 cliproxy 兼容 JSON（`type=xai` + token 三件套 + headers）。

## 脱敏与安全边界（重要）

### API 回传前端

| 接口 | 回传内容 | 是否含密钥 / token |
|---|---|---|
| `GET/POST/DELETE /api/g2a/servers*` | `sanitize_g2a_server` 结果 | **否**：去掉 `admin_key` / `api_key`，仅 `has_admin_key` / `has_api_key` / `can_proxy_image` |
| `GET /api/g2a/pool` | 远程号池状态行 + 连接元信息 | **否**：`access_token` 为合成 id `g2a:{server_id}:{credential_id}`，`readonly: true` |
| `GET /api/g2a/servers/{id}/credentials` | 远程凭证摘要 | **否**：解析时剔除 `access_token` / `refresh_token` / `id_token` |
| `POST /api/g2a/servers/{id}/push` | 推送结果计数 / 错误摘要 | **否**（响应不回显 token） |

实现入口：

- `services/g2a_service.sanitize_g2a_server` / `sanitize_g2a_servers`
- `credential_to_account_row`（合成 id，永不透传远程 token 字段）
- `_extract_credential_list` 的 `raw` 字段已剥离 token 三件套

### 密钥落盘（运行时，不进 git）

| 文件 | 内容 | 提交策略 |
|---|---|---|
| `data/g2a_config.json` | 连接配置，含 `admin_key` / `api_key` | **禁止提交** |
| `data/grok_accounts.json` | 本地 Grok OAuth token | **禁止提交** |
| `data/*.env` / 代理凭据 | 注册机、代理等 | **禁止提交** |

### 两种「上传」不要混

| 方向 | 是否带真实 token | 说明 |
|---|---|---|
| **Git 推送到 GitHub** | 否 | 只推源码/文档/测试；密钥与号池文件不入库 |
| **设置页「推送本地 Grok 号池」** | **会** | 主动把本地 OAuth 凭证发给已配置的 grokcli2api-go，远程才能轮转生图；属业务行为，需管理员 Bearer |
| **号池管理「GrokCLI2API」列表** | 否 | 只读镜像远程脱敏状态，不能导出 / 编辑 token |

### 管理鉴权

- 所有 `/api/g2a/*` 均需 chatgpt2api **管理员** Bearer（`require_admin`）
- 远程管理接口另需 `GROK_ADMIN_KEY`；OpenAI 兼容生图可用 `api_key`（空则回退 `admin_key`）
- 密钥写入仅发生在：创建/更新连接（body 传入）或推送号池（读本地池后发往远程）

### 前端展示约定

- 设置页连接卡片只显示「已配置 Admin Key / API Key / 可代理生图」徽章，**不回显**密钥明文
- 号池页 GrokCLI2API 标签为只读：可刷新、可删远程凭证，**无**导入/导出 token、无编辑 token
- 表格里的 `access_token` 列若出现 `g2a:...` 形态，那是合成 id，不是可用 OAuth token

## 使用步骤（号池留在远程）

1. 部署 grokcli2api-go，设置 `GROK_ADMIN_KEY`，确保管理接口与 `/v1/images/generations` 可访问  
2. chatgpt2api 设置 → **GrokCLI2API** → 添加连接  
   - 地址：服务根，如 `http://host:8088`（**不要**填 `/v1`，也不要填本地 Clash/系统代理端口）  
   - Admin Key：与远程一致  
   - API Key：可选；与 Admin 相同可留空  
   - 勾选「优先用此连接代理 Grok 生图」  
3. **探测连通**  
4. 号池管理切到 **GrokCLI2API** 看远程状态  
5. 文生图选 `grok-2-image` 等 → 请求会代理到远程，**无需**导入本地 `data/grok_accounts.json`

若仍想维护本地副本：先导入本地 Grok 号池，再「推送本地 Grok 号池」。

## Grok 生图路由顺序

`services/protocol/grok_v1_image_generations.py`：

1. 若存在 `prefer_for_image` 且已配置 key 的 G2A 连接 → 远程  
2. 否则本地 `data/grok_accounts.json` 免费 Build 路径  
3. **永不**落入 ChatGPT 号池  

Body 可选：`force_g2a` / `prefer_g2a` / `force_local` / `prefer_local` / `g2a_server_id`。

## 常见错误：HTTP 405 only CONNECT supported

这不是 grokcli2api-go 返回的业务错误，而是请求被 **CONNECT-only 转发代理**（常见于本机 `HTTP_PROXY`/`HTTPS_PROXY` 指向 Clash/v2ray 的 mixed/HTTP 端口）截走了。

本侧修复：

- G2A 管理/生图请求使用独立 `Session`，**默认 `trust_env=False` 且 `proxies` 清空**，不再继承环境代理  
- base_url 自动去掉尾部 `/v1`、`/v1/admin/credentials` 等误粘贴后缀  
- 可选 per-server `proxy` 字段仅在需要时启用出站代理  
- 远端 405/502 映射为本 API 的 **502**，错误文案会提示 CONNECT-only 代理问题  

## 与 CPA / 本地 Grok 池的关系

| 组件 | 作用 |
|---|---|
| `/api/accounts` | ChatGPT 号池 |
| `/api/grok/accounts` | 本地 Grok 号池（本进程调度） |
| `/api/g2a/pool` | 远程 grokcli2api-go 号池状态（只读） |
| `/api/cpa/*` | CLIProxyAPI 管理（ChatGPT 远程 auth-files） |
| `/api/g2a/*` | grokcli2api-go 管理 + 生图代理 |

存储与选号互不串池；远程状态行使用合成 id `g2a:{server_id}:{credential_id}`。

## curl 示例

```bash
export KEY='你的 auth-key'
export BASE='http://127.0.0.1:8000'

# 添加连接（开启生图代理）
curl -s -X POST "$BASE/api/g2a/servers" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"name":"local","base_url":"http://127.0.0.1:8088","admin_key":"YOUR_ADMIN","prefer_for_image":true}'

# 列表
curl -s "$BASE/api/g2a/servers" -H "Authorization: Bearer $KEY"

# 探测
SID='连接 id'
curl -s -X POST "$BASE/api/g2a/servers/$SID/ping" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{}'

# 远程号池状态（号池管理同源）
curl -s "$BASE/api/g2a/pool" -H "Authorization: Bearer $KEY"

# 推送全部本地 Grok 号（可选）
curl -s -X POST "$BASE/api/g2a/servers/$SID/push" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"access_tokens":[]}'

# Grok 生图（会优先走 G2A）
curl -s -X POST "$BASE/v1/images/generations" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"prompt":"a red cube","model":"grok-2-image","n":1,"response_format":"b64_json"}'
```

运维总册：[operations.md](./operations.md)。Grok 本地池：[grok-pool.md](./grok-pool.md)。

## 代码

| 路径 | 作用 |
|---|---|
| `services/g2a_service.py` | 配置 + Admin/OpenAI 客户端 + 推送 + 状态聚合 |
| `services/protocol/grok_v1_image_generations.py` | Grok 生图：G2A 优先，本地回退 |
| `api/g2a.py` | 管理路由 + `/api/g2a/pool` |
| `web/.../g2a-connections.tsx` | 设置页 UI |
| `web/.../accounts/page.tsx` | 号池管理 GrokCLI2API 标签 |
