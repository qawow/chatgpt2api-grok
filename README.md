<h1 align="center">ChatGPT2API-Grok</h1>

<p align="center">
  基于 <a href="https://github.com/basketikun/chatgpt2api">basketikun/chatgpt2api</a> 的二次开发分支：<br/>
  保留原版 ChatGPT 号池 / 生图能力，并新增<strong>独立 Grok 号池</strong>。
</p>

<p align="center">
  <a href="https://github.com/qawow/chatgpt2api-grok">GitHub（本仓库）</a> ·
  <a href="./CHANGELOG.md">v1.8.1</a> ·
  <a href="./docs/grok-pool.md">Grok 号池</a> ·
  <a href="./docs/gpt-register.md">GPT 批量注册</a> ·
  <a href="./docs/operations.md">运维与调用</a> ·
  <a href="./docs/deployment.md">部署说明</a>
</p>

> [!IMPORTANT]
> **这是二开仓库，不是官方镜像。**
> 部署时直接拉本仓库 GitHub Actions 构建的镜像：`ghcr.io/qawow/chatgpt2api:latest`。
> 不要使用 `ghcr.io/basketikun/chatgpt2api:latest`（上游官方镜像，丢掉 Grok / GPT 注册改动）。
> 本地改源码构建用 `docker-compose.local.yml`。

## 本分支相对上游新增

| 能力 | 说明 |
| --- | --- |
| 独立 Grok 号池 | `data/grok_accounts.json`，管理接口 `/api/grok/accounts*`，与 ChatGPT 号池完全隔离 |
| Grok 上游 | 默认 `cli-chat-proxy.grok.com`（Build/CLI），刷新走 `auth.x.ai` |
| 生图分流 | `model=grok-imagine-image` / `grok-2-image` 走 Grok 池；`grok-4.5` 是对话模型，不走生图 |
| 文本接口 | 已关闭：`/v1/grok/chat/completions`、纯文本 `/v1/chat/completions` 返回 400；只保留生图兼容接口 |
| GPT Free 批量注册 | 设置页「GPT注册」：内置 `gpt_free_register` 纯协议注册 free 号并入库 ChatGPT 号池；入库后自动刷新额度；无 refresh 的号标 `session_only`（**可生图**） |
| 账号出口隔离 | 绑定了 `proxy` 的号只走该出口，不回落 runtime / 全局 / 直连；注册浏览器指纹写入号池。一号一 IP 需要注册代理池 |
| 导入脚本 | `scripts/import_grok_cliproxy_auth.py` 批量导入 `type=xai` cliproxy JSON |

隔离原则：ChatGPT 与 Grok **不同存储、不同管理 API、不同选号**，禁止混池。ChatGPT 号池内，账号若绑定了 `proxy`，出站只走该代理；多个号绑同一 SOCKS 仍共享出口 IP。

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

### 3. 拉取镜像并启动

部署机无需编译，直接拉取 GitHub Actions 构建的镜像：

```bash
docker compose up -d
```

镜像由 GitHub Actions 在每次 push 到 `publish-root`/`main` 分支时构建并推送 `:sha-<commit>` 标签；打 `v*` tag（例如 `v1.8.1`）时才推送 `:latest` 与版本号。`docker-compose.yml` 默认拉 `:latest`。要吃未打 tag 的分支构建，把 `image` 改成 `ghcr.io/qawow/chatgpt2api:sha-<commit>`。首次部署或升级：`docker compose pull && docker compose up -d`。

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

### 5. GPT Free 批量注册（可选）

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

部署机无需 git pull 也无需本地构建，直接拉新镜像：

```bash
docker compose pull
docker compose up -d
```

`docker-compose.yml` 默认拉 `ghcr.io/qawow/chatgpt2api:latest`（GitHub Actions 在打 `v*` tag 时推送）。

**不要**拉官方上游镜像：

```bash
docker pull ghcr.io/basketikun/chatgpt2api:latest   # 官方镜像，无本分支改动
```

本地改源码构建仍可用：

```bash
git pull
docker compose -f docker-compose.local.yml up -d --build
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
- `GET /v1/models` 只返回生图模型：`gpt-image-2`、`codex-gpt-image-2`（及 plus/team/pro 前缀）；本地 Grok 号池非空时注入 `grok-2-image` / `grok-imagine-image` / `grok-imagine`。**不暴露对话模型**（gpt-5* / auto / grok-4.5）
- 支持通过 `n` 返回多张生成结果
- 支持 Codex 中的画图接口逆向，仅 `Plus` / `Team` / `Pro` 订阅可用，模型别名为 `codex-gpt-image-2`，如有需要可自行在其他场景映射回
  `gpt-image-2`，用于和官网画图区分；也就意味着同一账号会同时有官网和 Codex 两份生图额度

### 在线画图功能

- 内置在线画图工作台，支持生成、图片编辑与多图组图编辑
- 支持 `gpt-image-2`、`codex-gpt-image-2`；Grok 号池非空时还有 `grok-*-image*` / `grok-imagine`
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
- 遇到 Token 失效类错误时自动剔除无效 Token（`session_only` 只标异常、保留剩余额度，不自动删）
- 定时检查限流账号并自动刷新
- 支持密码重新登录恢复异常账号，刷新后可自动重登
- 支持网页端配置全局 HTTP / HTTPS / SOCKS5 / SOCKS5H 代理
- 支持 WARP / FlareSolverr 稳定代理运行时
- 支持搜索、筛选、批量刷新、导出、手动编辑和清理账号
- 支持 `access_token` / 本地 JSON 导入，以及内置 GPT Free 协议注册入库

### Grok 号池（独立）

- 存储：`data/grok_accounts.json`（不进 `accounts.json`）
- 管理：`GET/POST/DELETE /api/grok/accounts`、`/refresh`、`/update`、`/import-files`
- 上游：`cli-chat-proxy.grok.com` + cliproxy 兼容 headers
- 生图：
  - `POST /v1/images/generations` + `model=grok-2-image|grok-imagine`（model 分流）
  - `POST /v1/grok/images/generations`（强制 Grok 池）
  - 本地免费 Build 路径；**永不**落入 ChatGPT 号池
- 文本：已关闭（`POST /v1/grok/chat/completions` 返回 400）
- 模型列表：`GET /v1/grok/models` 仅生图 id；本地池非空时注入总 `GET /v1/models`
- 文档：[docs/grok-pool.md](./docs/grok-pool.md)

### GPT Free 批量注册

- 内置模块：`gpt_free_register/`（vendored ChatGPT 协议注册机 + Cloudflare D1 邮箱，无需外部 `/root/any-register-engines`）
- 协议路径对齐 yukkcat：auto-OTP 默认 **passwordless**、Sentinel dual-header / SO collect、每号浏览器画像
- **默认跳过 Codex 二次 OTP**（`skip_codex` / `OPENAI_SKIP_CODEX=1`），入库后后台刷新额度，缩短单号耗时
  - 取消勾选后需点 **保存配置** 再启动；API 模型已声明 `skip_codex` 等字段，避免旧版静默丢弃
  - 跳过 Codex 的号为 `session_only`：可生图，但无 `refresh_token`；默认不再后台二次登录补 refresh（会踢掉 session）
  - **Codex 补 refresh**：入库后不再自动跑；号池行上钥匙图标可手动补（`POST /api/accounts/codex-upgrade`）。手动补是二次登录，可能踢掉当前 session
  - **已有 session 号补 refresh**：号池管理 → ChatGPT → 行上钥匙图标 / 工具栏「Codex 补 refresh」（`POST /api/accounts/codex-upgrade`，无需浏览器粘贴 callback）
- 设置页 **GPT注册**：数量 / 并发 / 间隔 / 邮箱 / 代理 / CFD1 域名等可填；默认自动补号，保持号池至少 1 个可生图账号
- 管理 API：`/api/gpt-register/settings`、`/start`、`/jobs*`、`/cancel`
- 成功账号进入 **ChatGPT 号池**（不进 Grok）；默认 `push_mode=local` 进程内入库
- 密钥放 `data/gpt_register.env` 或环境变量；SOCKS 需 `PySocks`
- 文档：[docs/gpt-register.md](./docs/gpt-register.md)（§6.7 / §6.7.1 耗时优化与 Codex 补 refresh）· 运维：[docs/operations.md](./docs/operations.md)

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
| 返回模型 | 仅生图：`gpt-image-2`、`codex-gpt-image-2`（及订阅前缀）、Grok 池非空时 `grok-imagine-image` / `grok-2-image` |
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

仅面向**生图**的 Chat Completions 兼容接口。文本模型已关闭。

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
| `model`              | 生图模型：`gpt-image-2` / `codex-gpt-image-2` / `grok-2-image` |
| `messages`           | 消息数组，从中解析生图提示词 |
| `n`                  | 图片生成数量 |
| `stream`             | 可选 |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/responses</code></summary>
<br>

仅面向**生图工具**的 Responses 兼容接口。纯文本请求返回 400。

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-image-2",
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
| `model`  | 生图模型 |
| `input`  | 提示词 |
| `tools`  | 需含 `image_generation` |
| `stream` | 已实现，但仍在测试 |

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
