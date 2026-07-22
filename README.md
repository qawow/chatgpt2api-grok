<h1 align="center">ChatGPT2API-Grok</h1>

<p align="center">
  基于 <a href="https://github.com/basketikun/chatgpt2api">basketikun/chatgpt2api</a> 的二次开发分支：<br/>
  保留原版 ChatGPT 号池 / 生图能力，并新增<strong>独立 Grok 号池</strong>与
  <a href="https://github.com/Futureppo/grokcli2api-go">grokcli2api-go</a> 设置页接入。
</p>

<p align="center">
  <a href="https://github.com/qawow/chatgpt2api-grok">GitHub（本仓库）</a> ·
  <a href="./docs/grok-pool.md">Grok 号池</a> ·
  <a href="./docs/g2a-bridge.md">GrokCLI2API 桥</a> ·
  <a href="./docs/gpt-register.md">GPT 批量注册</a> ·
  <a href="./docs/operations.md">运维与调用</a> ·
  <a href="./docs/deployment.md">部署说明</a>
</p>

> [!IMPORTANT]
> **这是二开仓库，不是官方镜像。**  
> 部署时必须用当前源码 **本地构建**（`docker-compose.local.yml`）。  
> 不要使用 `ghcr.io/basketikun/chatgpt2api:latest` 或默认 `docker compose up` 拉官方镜像，否则会丢掉 Grok / G2A / GPT 注册改动。

## 本分支相对上游新增

| 能力 | 说明 |
| --- | --- |
| 独立 Grok 号池 | `data/grok_accounts.json`，管理接口 `/api/grok/accounts*`，与 ChatGPT 号池完全隔离 |
| Grok 上游 | 默认 `cli-chat-proxy.grok.com`（Build/CLI），刷新走 `auth.x.ai` |
| 生图分流 | `model=grok-2-image` / `grok-imagine` 走 Grok 池；另有 `/v1/grok/images/generations` |
| 文本探活 | `/v1/grok/chat/completions`（内部映射 Build `/responses`） |
| GrokCLI2API 接入 | 设置页「GrokCLI2API」：对接远程 Admin API，**推送**本地 Grok 号到 [grokcli2api-go](https://github.com/Futureppo/grokcli2api-go) |
| GPT Free 批量注册 | 设置页「GPT注册」：内置 `gpt_free_register` 纯协议注册 free 号并入库 ChatGPT 号池；入库后自动刷新额度；无 refresh 的 session 号标 fragile |
| 导入脚本 | `scripts/import_grok_cliproxy_auth.py` 批量导入 `type=xai` cliproxy JSON |

隔离原则：ChatGPT 与 Grok **不同存储、不同管理 API、不同选号**，禁止混池。

> [!WARNING]
> 免责声明：
>
> 本项目涉及对 ChatGPT / Grok 相关能力的逆向或非官方兼容封装，仅供个人学习、技术研究与非商业性技术交流使用。
>
> - 严禁将本项目用于任何商业用途、盈利性使用、批量操作、自动化滥用或规模化调用。
> - 严禁将本项目用于破坏市场秩序、恶意竞争、套利倒卖、二次售卖相关服务，以及任何违反 OpenAI / xAI 服务条款或当地法律法规的行为。
> - 严禁将本项目用于生成、传播或协助生成违法、暴力、色情、未成年人相关内容，或用于诈骗、欺诈、骚扰等非法或不当用途。
> - 使用者应自行承担全部风险，包括但不限于账号被限制、临时封禁或永久封禁以及因违规使用等所导致的法律责任。
> - 使用本项目即视为你已充分理解并同意本免责声明全部内容；如因滥用、违规或违法使用造成任何后果，均由使用者自行承担。
> - 请勿使用重要账号、常用账号或高价值账号进行测试。

## 快速开始（二开推荐）

### 1. 克隆本仓库

私有仓需要 GitHub 登录或 Token：

```bash
git clone https://github.com/qawow/chatgpt2api-grok.git
cd chatgpt2api-grok
```

### 2. 配置密钥

```bash
# 编辑 config.json 中的 auth-key（务必改成强随机值）
# 或使用环境变量覆盖：
# export CHATGPT2API_AUTH_KEY='your_strong_secret'

mkdir -p data
```

### 3. 本地构建并启动

```bash
docker compose -f docker-compose.local.yml up -d --build
```

- Web / API：`http://localhost:8000`
- OpenAI 兼容前缀：`http://localhost:8000/v1`
- 数据目录：`./data`（ChatGPT 号池、Grok 号池、日志、图片、注册机配置等）
- 配置挂载：`./config.json`
- 容器内监听 **:80**（compose 映射 8000→80）

验证二开接口：

```bash
export KEY='你的 auth-key'

curl -s http://127.0.0.1:8000/api/grok/accounts \
  -H "Authorization: Bearer $KEY"

curl -s http://127.0.0.1:8000/api/g2a/servers \
  -H "Authorization: Bearer $KEY"

curl -s http://127.0.0.1:8000/api/gpt-register/settings \
  -H "Authorization: Bearer $KEY"
```

### 4. 导入 Grok 账号（可选）

支持 CLIProxyAPI / cliproxy 风格 `type=xai` JSON：

```bash
# 服务已启动时
python scripts/import_grok_cliproxy_auth.py \
  --dir /path/to/cliproxyapi_auth \
  --base-url http://127.0.0.1:8000 \
  --auth-key "$KEY"
```

或在管理 API：

```bash
curl -s -X POST http://127.0.0.1:8000/api/grok/accounts \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"accounts":[{ ...cliproxy json... }]}'
```

### 5. 对接 grokcli2api-go（可选）

1. 远端启用 `GROK_ADMIN_KEY`（默认端口 `8088`）  
2. Web 设置 → **GrokCLI2API** → 添加连接（base URL + Admin Key）  
3. 探测连通 → **推送本地 Grok 号池**

说明：远程 `GET /v1/admin/credentials` **不含 token**，只能「本地 → 远程」推送，不能反向拉号。详见 [docs/g2a-bridge.md](./docs/g2a-bridge.md)。

### 6. GPT Free 批量注册（可选）

1. 写 `data/gpt_register.env`（`CFD1_*`、`REGISTER_PROXY*` 等，勿提交 git）  
2. Web 设置 → **GPT注册** → 填数量等 → 开始  
3. 或 `POST /api/gpt-register/start`  

成功账号进入 **ChatGPT 号池**。完整调用 / 维护 / 排障见 [docs/gpt-register.md](./docs/gpt-register.md)。  
总运维手册：[docs/operations.md](./docs/operations.md)。

### WARP / FlareSolverr 稳定代理部署

若 ChatGPT 上游经常被 Cloudflare 拦截：

```bash
cp .env.example .env
# 修改 CHATGPT2API_AUTH_KEY 等

# 注意：请确认 warp compose 使用本地 build 镜像，而不是官方 ghcr 镜像
docker compose -f docker-compose.warp.yml up -d --build
```

也可先构建本地镜像再替换 compose 中的 `image`：

```bash
docker build -t chatgpt2api:local .
```

### 本地开发

后端：

```bash
git clone https://github.com/qawow/chatgpt2api-grok.git
cd chatgpt2api-grok
uv sync
uv run main.py
```

前端：

```bash
cd web
bun install   # 或 npm install
bun run dev   # 或 npm run dev
```

### 更新本分支

```bash
git pull
docker compose -f docker-compose.local.yml up -d --build
```

**不要**再执行：

```bash
docker pull ghcr.io/basketikun/chatgpt2api:latest   # 官方镜像，无本分支改动
```

### 存储后端配置

支持通过环境变量 `STORAGE_BACKEND` 切换存储方式：

- `json` - 本地 JSON 文件（默认；Grok 池固定写 `data/grok_accounts.json`）
- `sqlite` - 本地 SQLite 数据库（ChatGPT 池）
- `postgres` - 外部 PostgreSQL（需配置 `DATABASE_URL`）
- `git` - Git 私有仓库（需配置 `GIT_REPO_URL` 和 `GIT_TOKEN`）

示例：使用 PostgreSQL

```yaml
environment:
  - STORAGE_BACKEND=postgres
  - DATABASE_URL=postgresql://user:password@host:5432/dbname
```

## 功能

### API 兼容能力

- 兼容 `POST /v1/images/generations` 图片生成接口
- 兼容 `POST /v1/images/edits` 图片编辑接口
- 兼容面向图片场景的 `POST /v1/chat/completions`
- 兼容面向图片场景的 `POST /v1/responses`
- `GET /v1/models` 返回 `gpt-image-2`、`codex-gpt-image-2`、`auto`、`gpt-5`、`gpt-5-1`、`gpt-5-2`、`gpt-5-3`、`gpt-5-3-mini`、
  `gpt-5-mini`；若存在 Grok 号还会注入 `grok-2-image` / `grok-imagine` / `grok-4.5` 等
- 支持通过 `n` 返回多张生成结果
- 支持生成可编辑 PPT 文件
- 支持生成可编辑 PSD 文件
- 支持 Codex 中的画图接口逆向，仅 `Plus` / `Team` / `Pro` 订阅可用，模型别名为 `codex-gpt-image-2`，如有需要可自行在其他场景映射回
  `gpt-image-2`，用于和官网画图区分；也就意味着同一账号会同时有官网和 Codex 两份生图额度

### 在线画图功能

- 内置在线画图工作台，支持生成、图片编辑与多图组图编辑
- 支持 `gpt-image-2`、`codex-gpt-image-2`、`auto`、`gpt-5`、`gpt-5-1`、`gpt-5-2`、`gpt-5-3`、`gpt-5-3-mini`、`gpt-5-mini` 模型选择
- 若 Grok 号池非空，模型列表也会出现 `grok-*-image*` / `grok-imagine`
- 编辑模式支持参考图上传
- 前端支持多图生成交互
- 本地保存图片会话历史，支持回看、删除和清空
- 支持服务端缓存图片URL
- 图片生成进度追踪，超时后可继续等待
- 图片懒加载与滚动位置记忆，优化大量图片场景性能

### 号池管理功能（ChatGPT）

- 自动刷新账号邮箱、类型、额度和恢复时间（异步进度追踪）
- 轮询可用账号执行图片生成与图片编辑
- 遇到 Token 失效类错误时自动剔除无效 Token
- 定时检查限流账号并自动刷新
- 支持密码重新登录恢复异常账号，刷新后可自动重登
- 支持网页端配置全局 HTTP / HTTPS / SOCKS5 / SOCKS5H 代理
- 支持 WARP / FlareSolverr 稳定代理运行时
- 支持搜索、筛选、批量刷新、导出、手动编辑和清理账号
- 支持四种导入方式：本地 CPA JSON 文件导入、远程 CPA 服务器导入、`sub2api` 服务器导入、`access_token` 导入
- 支持在设置页配置 `sub2api` 服务器，筛选并批量导入其中的 OpenAI OAuth 账号

### Grok 号池（独立）

- 存储：`data/grok_accounts.json`（不进 `accounts.json`）
- 管理：`GET/POST/DELETE /api/grok/accounts`、`/refresh`、`/update`、`/import-files`
- 上游：`cli-chat-proxy.grok.com` + cliproxy 兼容 headers
- 生图：
  - `POST /v1/images/generations` + `model=grok-2-image|grok-imagine`（model 分流）
  - `POST /v1/grok/images/generations`（强制 Grok 池）
  - G2A 优先：远程 `POST /v1/responses` + `image_generation` 工具（grokcli2api-go 0.4.x；无 Images API）
  - 本地回退：免费 Build 同路径；**永不**落入 ChatGPT 号池
- 文本：`POST /v1/grok/chat/completions`
- 模型列表：`GET /v1/grok/models`；有号时也会注入总 `GET /v1/models`
- 设置页对接 [grokcli2api-go](https://github.com/Futureppo/grokcli2api-go)：`/api/g2a/servers*`
- 文档：[docs/grok-pool.md](./docs/grok-pool.md)、[docs/g2a-bridge.md](./docs/g2a-bridge.md)

### GPT Free 批量注册

- 内置模块：`gpt_free_register/`（vendored ChatGPT 协议注册机 + Cloudflare D1 邮箱，无需外部 `/root/any-register-engines`）
- 协议路径对齐 yukkcat：auto-OTP 默认 **passwordless**、Sentinel dual-header / SO collect、每号浏览器画像
- **默认跳过 Codex 二次 OTP**（`skip_codex` / `OPENAI_SKIP_CODEX=1`），入库后后台刷新额度，缩短单号耗时
  - 取消勾选后需点 **保存配置** 再启动；API 模型已声明 `skip_codex` 等字段，避免旧版静默丢弃
  - 跳过 Codex 的号为 `session_only`：不参与生图候选、401 不自动删除
  - **已有 session 号补 refresh**：号池管理 → ChatGPT → 行上钥匙图标 / 工具栏「OAuth 补 refresh」（浏览器 OAuth 同一邮箱，`replace_access_token` 替换旧行）
- 设置页 **GPT注册**：数量 / 并发 / 间隔 / 邮箱 / 代理 / CFD1 域名等可填
- 管理 API：`/api/gpt-register/settings`、`/start`、`/jobs*`、`/cancel`
- 成功账号进入 **ChatGPT 号池**（不进 Grok）；默认 `push_mode=local` 进程内入库
- 密钥放 `data/gpt_register.env` 或环境变量；SOCKS 需 `PySocks`
- 文档：[docs/gpt-register.md](./docs/gpt-register.md)（§6.7 / §6.7.1 耗时优化与 OAuth 升级）· 运维：[docs/operations.md](./docs/operations.md)

### 实验性 / 规划中

- 详细状态说明见：[功能清单](./docs/feature-status.en.md)
- Build 通道生图以上游实际能力为准；若 `/images/generations` 不可用会返回明确错误，**不会**回落到 ChatGPT 号池

## 效果展示

<table width="100%">
  <tr>
    <td width="50%"><img src="https://i.ibb.co/Jj8nfwwP/image.png" alt="image" border="0"></td>
    <td width="50%"><img src="https://i.ibb.co/pqf235v/image-edit.png" alt="image edit" border="0"></td>
  </tr>
  <tr>
    <td width="50%"><img src="https://i.ibb.co/tPcqtVfd/chery-studio.png" alt="chery studio" border="0"></td>
    <td width="50%"><img src="https://i.ibb.co/PsT9YHBV/account-pool.png" alt="account pool" border="0"></td>
  </tr>
  <tr>
    <td width="50%"><img src="https://i.ibb.co/rRWLG08q/new-api.png" alt="new api" border="0"></td>
  </tr>
</table>

## API

所有 AI 接口都需要请求头：

```http
Authorization: Bearer <auth-key>
```

<details>
<summary><code>GET /v1/models</code></summary>
<br>

返回当前暴露的图片模型列表。

```bash
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer <auth-key>"
```

<details>
<summary>说明</summary>
<br>

| 字段   | 说明                                                                                                         |
|:-----|:-----------------------------------------------------------------------------------------------------------|
| 返回模型 | `gpt-image-2`、`codex-gpt-image-2`、`auto`、`gpt-5`、`gpt-5-1`、`gpt-5-2`、`gpt-5-3`、`gpt-5-3-mini`、`gpt-5-mini` |
| 接入场景 | 可接入 Cherry Studio、New API 等上游或客户端                                                                          |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/images/generations</code></summary>
<br>

OpenAI 兼容图片生成接口，用于文生图。

```bash
curl http://localhost:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-image-2",
    "prompt": "一只漂浮在太空里的猫",
    "n": 1,
    "response_format": "b64_json"
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段                | 说明                                                 |
|:------------------|:---------------------------------------------------|
| `model`           | 图片模型，当前可用值以 `/v1/models` 返回结果为准，推荐使用 `gpt-image-2` |
| `prompt`          | 图片生成提示词                                            |
| `n`               | 生成数量，当前后端限制为 `1-4`                                 |
| `response_format` | 当前请求模型中包含该字段，默认值为 `b64_json`                       |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/images/edits</code></summary>
<br>

OpenAI 兼容图片编辑接口，可上传图片文件，也可按官方 JSON 格式传入图片链接并生成编辑结果。

```bash
curl http://localhost:8000/v1/images/edits \
  -H "Authorization: Bearer <auth-key>" \
  -F "model=gpt-image-2" \
  -F "prompt=把这张图改成赛博朋克夜景风格" \
  -F "n=1" \
  -F "image=@./input.png"
```

也可以直接传图片 URL：

```bash
curl http://localhost:8000/v1/images/edits \
  -H "Authorization: Bearer <auth-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-image-2",
    "prompt": "把这张图改成赛博朋克夜景风格",
    "images": [
      {"image_url": "https://example.com/input.png"}
    ]
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段          | 说明                                            |
|:------------|:----------------------------------------------|
| `model`     | 图片模型， `gpt-image-2`                           |
| `prompt`    | 图片编辑提示词                                       |
| `n`         | 生成数量，当前后端限制为 `1-4`                            |
| `image`     | 需要编辑的图片文件，使用 multipart/form-data 上传           |
| `images`    | JSON 图片引用数组，支持 `{"image_url": "https://..."}` |
| `image_url` | 表单模式下也可直接传图片链接，支持重复字段传多张图                     |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/chat/completions</code></summary>
<br>

面向文本、网页搜索与图片场景的 Chat Completions 兼容接口，不是完整通用聊天代理。

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-image-2",
    "messages": [
      {
        "role": "user",
        "content": "生成一张雨夜东京街头的赛博朋克猫"
      }
    ],
    "n": 1
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段                   | 说明                                                                           |
|:---------------------|:-----------------------------------------------------------------------------|
| `model`              | 文本、搜索或图片模型；搜索模型会触发网页搜索兼容逻辑                                                   |
| `messages`           | 消息数组，支持文本、搜索和图片请求内容                                                          |
| `n`                  | 图片生成数量，按当前实现解析为图片数量                                                          |
| `stream`             | 文本、搜索和图片场景均支持，仍在测试                                                           |
| `tools`              | 文本场景支持 `web_search` / `web_search_preview` / `web_search_preview_2025_03_11` |
| `web_search_options` | 传入时会触发网页搜索兼容逻辑                                                               |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/responses</code></summary>
<br>

面向文本、网页搜索和图片生成工具调用的 Responses API 兼容接口，不是完整通用 Responses API 代理。

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-5",
    "input": "生成一张未来感城市天际线图片",
    "tools": [
      {
        "type": "image_generation"
      }
    ]
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段       | 说明                                                                                      |
|:---------|:----------------------------------------------------------------------------------------|
| `model`  | 响应中会回显该模型字段，搜索和图片生成会走对应兼容逻辑                                                             |
| `input`  | 输入内容；搜索使用最后一条用户文本，图片生成需能解析出提示词                                                          |
| `tools`  | 支持 `image_generation`、`web_search`、`web_search_preview`、`web_search_preview_2025_03_11` |
| `stream` | 已实现，但仍在测试                                                                               |

<br>
</details>
</details>

## 社区支持

学 AI , 上 L 站：[LinuxDO](https://linux.do)

## Contributors

感谢所有为本项目做出贡献的开发者：

<a href="https://github.com/basketikun/chatgpt2api/graphs/contributors">
  <img alt="Contributors" src="https://contrib.rocks/image?repo=basketikun/chatgpt2api" />
</a>

## Star History

[![Star History Chart](https://api.star-history.com/chart?repos=basketikun/chatgpt2api&type=date&legend=top-left)](https://www.star-history.com/?repos=basketikun%2Fchatgpt2api&type=date&legend=top-left)
