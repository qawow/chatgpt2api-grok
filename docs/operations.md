# 运维与维护（chatgpt2api-grok）

面向本二开仓库的日常使用、升级、备份与排障。当前版本 **1.8.2**（仓库根目录 `VERSION`，说明见 [CHANGELOG.md](../CHANGELOG.md)）。上游官方文档见原项目。部署机优先拉本仓库 GitHub Actions 构建的镜像，不要用上游官方 `ghcr.io/basketikun/chatgpt2api`。`docker-compose.yml` 默认 `:latest`，只在打 `v*` tag 时更新；分支 push 只出 `:sha-<commit>`。

## 1. 正确部署方式

```bash
git clone https://github.com/qawow/chatgpt2api-grok.git
cd chatgpt2api-grok
# 配置 config.json 中 auth-key
mkdir -p data
docker compose pull
docker compose up -d
```

本地改源码再构建：

```bash
docker compose -f docker-compose.local.yml up -d --build
```

| 项 | 值 |
| --- | --- |
| Web | `http://localhost:8000` |
| OpenAI 兼容 | `http://localhost:8000/v1` |
| 容器内监听 | `:80`（compose 映射 8000→80） |
| 数据卷 | `./data` → `/app/data` |

**禁止：**

- 拉 `ghcr.io/basketikun/chatgpt2api:latest`（上游官方镜像，无 Grok / GPT 注册）
- 空挂载 `./gpt_free_register` 盖掉镜像内 builtin engines

WARP 场景：

```bash
cp .env.example .env   # 改 CHATGPT2API_AUTH_KEY
docker compose -f docker-compose.warp.yml up -d --build
```

## 2. 模块与文档索引

| 模块 | 文档 | 管理入口 |
| --- | --- | --- |
| ChatGPT 号池 / 生图 | README、原功能说明 | 号池页、`/api/accounts*`、`/v1/*` |
| Grok 号池 | [grok-pool.md](./grok-pool.md) | `/api/grok/accounts*`、`/v1/grok/*` |
| GPT Free 注册 | [gpt-register.md](./gpt-register.md) | 设置 → GPT注册，`/api/gpt-register/*` |
| 部署升级 | [deployment.md](./deployment.md) | compose / 数据保留 |

隔离原则：**ChatGPT 与 Grok 不同存储、不同 API、不同选号**，禁止混池。

### 账号隔离（ChatGPT 号池内）

- 账号字段 `proxy` 有值时，刷新 / 生图 / 探活**只走该出口**，不再回落 runtime / 全局 / 直连（对齐 CPA / [codex2api](https://github.com/james-6-23/codex2api)）。
- 未绑定代理的号仍按 runtime → 全局 → 直连回退；死 SOCKS 会跳过已拉黑出口。
- 注册默认 `bind_register_proxy=true`：入库时把注册代理写到账号。多个号绑同一 SOCKS **仍是同一出口 IP**；真要一号一 IP，给注册机配多出口代理池。
- 设备指纹（`oai-device-id` / UA / impersonate）按号写入并复用；token 刷新锁、Cloudflare clearance 按号拆开。
- `session_only`（无 `refresh_token`）**可以生图**。`chat_requirements_prepare` 401 不当 hard revoke、不清零剩余额度、不自动删号。
- 入库后**不会**自动 Codex 二次登录。5 分钟巡检也不再打健康 session_only 的 `/me`。要补 `refresh_token` 用号池页手动「Codex 补 refresh」（会踢当前 web session）。
- **自动补号**（设置 → GPT注册，默认开）：可生图账号低于最少数量时，用当前注册配置自动开任务。已有任务会等待；连续入库 0 冷却 10 分钟。

## 3. 调用速查

### 3.1 鉴权

```bash
export KEY='config.json 中的 auth-key'
export BASE='http://127.0.0.1:8000'
# 所有管理 / AI 接口：
# Authorization: Bearer $KEY
```

### 3.2 ChatGPT 号池

```bash
curl -s "$BASE/api/accounts" -H "Authorization: Bearer $KEY"

# session_only → Codex 补 refresh（主路径；需 CFD1 + 注册代理配置）
curl -s -X POST "$BASE/api/accounts/codex-upgrade" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"email":"user@mail.example.com","access_token":"<session_access_token>"}'
```

### 3.3 Grok 号池

```bash
curl -s "$BASE/api/grok/accounts" -H "Authorization: Bearer $KEY"

# 导入 cliproxy type=xai
python scripts/import_grok_cliproxy_auth.py \
  --dir /path/to/cliproxyapi_auth \
  --base-url "$BASE" --auth-key "$KEY"
```

生图 / 文本：

```bash
# model 分流
curl -s "$BASE/v1/images/generations" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"prompt":"a red cube","model":"grok-2-image","n":1,"response_format":"b64_json"}'

# 强制 Grok 路径
curl -s "$BASE/v1/grok/images/generations" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"prompt":"a red cube","n":1}'
```

### 3.4 GPT Free 批量注册

```bash
# 密钥：data/gpt_register.env（CFD1_* / REGISTER_PROXY*）

curl -s -X POST "$BASE/api/gpt-register/start" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"count":1,"concurrency":1}'

curl -s "$BASE/api/gpt-register/jobs" -H "Authorization: Bearer $KEY"
```

Web：设置 → **GPT注册**。完整说明：[gpt-register.md](./gpt-register.md)。

## 4. 备份

至少备份：

| 路径 | 内容 |
| --- | --- |
| `config.json` | auth-key、代理、业务配置 |
| `data/accounts.json` 或 sqlite/postgres | ChatGPT 号池 |
| `data/grok_accounts.json` | Grok 号池 |
| `data/gpt_register.env` | 注册机密钥 |
| `data/gpt_register_config.json` | 注册表单 |
| `data/images/` 等 | 按需 |

升级前：

```bash
tar czf backup-$(date +%Y%m%d).tgz config.json data
```

## 5. 升级流程

```bash
cd chatgpt2api-grok
git pull
docker compose pull && docker compose up -d
# 本地改源码：docker compose -f docker-compose.local.yml up -d --build
# WARP：docker compose -f docker-compose.warp.yml up -d --build
docker logs -f chatgpt2api   # 容器名以 compose 为准（local 构建是 chatgpt2api-local）
```

检查：

```bash
curl -s "$BASE/api/grok/accounts" -H "Authorization: Bearer $KEY" | head
curl -s "$BASE/api/gpt-register/settings" -H "Authorization: Bearer $KEY" | head
```

## 6. 日志与排障

```bash
docker logs -f chatgpt2api
# 过滤注册机 stdout：grep gpt-register

# 设置页 GPT注册：logs / items / summary
# 任务索引：data/gpt_register_jobs.json
# 单任务完成日志：data/gpt_register_logs/<job_id>.json
# 系统日志：data/logs.jsonl（type=account，摘要「GPT注册任务结束」）
```

| 症状 | 方向 |
| --- | --- |
| 无 Grok / 注册 API | 是否拉了上游官方镜像 → 改用 `ghcr.io/qawow/chatgpt2api` 或 local compose 重建 |
| GPT 注册 engines 不存在 | 旧镜像或空 volume → rebuild；勿空挂 gpt_free_register |
| provider_definitions 缺表 | `data/register_engines.db` 权限/损坏 → 删除后重启 |
| SOCKS 报 Missing dependencies | 镜像缺 PySocks → rebuild |
| Grok 生图 502 / `no auth context` | 本地 access token 过期：号池会在选号时自动 refresh；也可号池管理点「刷新」 |
| Grok 生图 502（非 401） | Build 通道可能无 images / 额度；不会回落 ChatGPT 池 |
| GPT 注册 `account_creation_failed` + OTP 失效 | 勿强制 auto-OTP 密码路径；见 [gpt-register.md](gpt-register.md) §6.7 |
| GPT 注册成功但 Codex `add_phone` | 默认已跳过 Codex（`skip_codex`/`OPENAI_SKIP_CODEX=1`），入库为可生图的 `session_only`；手动关闭跳过才会走 Codex，失败则软保留 session 行 |
| 注册号无生图额度 / 秒死 | 入库后**后台** `fetch_remote_info`；free 上游 `image_gen.remaining` 常为 0，看号池本地 `quota`。`session_only` **可生图**。约 5 分钟被标异常：以前是入库后自动 Codex / 巡检 `/me`+密码重登二次登录；现已关掉。手动「Codex 补 refresh」仍会踢 session |
| session_only 要补 refresh | 号池管理 → ChatGPT → 行上钥匙图标 / 工具栏「Codex 补 refresh」。**不会**入库后自动补。手动补是二次登录，可能踢掉当前 web session。见 [gpt-register.md](gpt-register.md) §6.7.1 |
| 生图报模型追问 / `content_policy_violation` 但其实出过图 | 旧版取下载地址撞 OPENSSL 后把「你更喜欢…？」当成违规。1.8.1 会重试同出口，不再把追问当政策拦截 |
| `upstream session expired, please retry` | `chat_requirements_prepare` 401：不当废号、同请求换号。不是额度用尽。1.8.6 起轮询超时也会换号续传，且结果已到手后的断流不再判死 |
| `no available image quota ... none are image-selectable, tried=0` | 池子全部被判吊销/异常，选号器无可发。1.8.6 起过期探活（30 分钟）会发现死号并触发自动补号；老版本需手动「检测」全部账号把僵尸号标掉 |
| `no available codex image quota ... only 0 are codex-source` | `codex-gpt-image-2` 只接受导入的 Codex OAuth 号（Plus/Team/Pro）。免费注册号走 web 链路，客户端改用 `gpt-image-2.5`（以 `/v1/models` 目录为准） |
| `upstream image connection failed` | OPENSSL / curl 35 / 代理失败：同出口短重试后再换号。SOCKS 上不要切直连（本机直连 chatgpt.com 会超时） |
| OTP / OAuth 超时 | 换代理出口；CFD1 本身不走 OpenAI 代理 |

### 图片任务续传

图片页失败卡片提供「续传 / 换号继续」。接口仍为
`POST /api/image-tasks/{task_id}/resume-poll`，请求体
`{"extra_timeout_secs":30}`（5–120 秒）。

- 有可访问的上游会话：先继续轮询、下载已有图片，避免重新生成。
- 账号失效，或续轮询超时/连接失败：使用保存的原始请求换可用账号重新生成。不同账号不能读取彼此的上游会话；换号不是把旧会话直接移交。
- 提示词、尺寸、质量和检查点保存在 `data/image_tasks.json`，参考图/蒙版独立保存于 `data/image_tasks_inputs/`，元数据只存文件引用。旧内联 base64 会在加载时迁移，迁移失败保留原记录。请保护这两处及备份，任务查询接口不返回原始输入或凭证。
- 仅进度更新每秒最多写盘一次；会话检查点、结算和终态立即保存。任务保留期到达后，先提交元数据删除再清理过期输入；备份中的 `image_tasks` 选项也包含输入附件，不依赖 `images` 选项。
- 临时连接失败、未出图和轮询超时的凭证在该任务内冷却 60 秒，冷却到期可再次续传；确认吊销的凭证不会自动恢复。更新同邮箱的 token 不受旧凭证排除记录影响。
- 续轮询、下载及必要的换号重放共用 `image_task_timeout_secs` 总预算（默认 600 秒）；`extra_timeout_secs` 限制其中的续轮询阶段。超时会停止后续处理，但不能保证撤销已经提交给上游的生成任务。
- 生成与续传共用 `generation_id` 结算标识，重复取得同一次生成的结果不会再次扣本地额度；旧任务用上游 conversation_id 兼容去重。
- 重启后未完成任务仍标为中断，可手动续传；成功图片不会被重新提交。没有保存原始输入的历史任务仅能续轮询，无法换号重放时需点「重新生成这一张」。内容策略拒绝不自动换号重试。
- `revoked=10`、`tried=0` 表示没有 token 能进入选号器，`local_quota_sum` 不代表上游真实可用。续传不能复活已吊销 token：先导入/重新登录有效账号，再续传失败任务。账号池为空时不会无限重试。

## 7. 开发

```bash
uv sync
uv run main.py          # 后端
cd web && bun install && bun run dev
uv run python -m unittest \
  test.test_gpt_register \
  test.test_gpt_register_engine \
  test.test_codex_upgrade \
  test.test_grok_pool \
  test.test_proxy_service \
  test.test_connection_errors \
  test.test_account_image_capabilities \
  test.test_curl_tls \
  test.test_image_download_auth \
  test.test_web_fallback -v
```

## 8. 安全

- 强随机 `auth-key` / `CHATGPT2API_AUTH_KEY`  
- `data/*.env`、号池 token 勿提交 git  
- 管理端口勿裸奔公网；需要时反代 + HTTPS + 访问控制  
- 本项目仅供学习研究，遵守各平台服务条款与法律  

运行时密钥文件：`data/grok_accounts.json`、`data/accounts.json`。详见 [grok-pool.md](./grok-pool.md)。  
