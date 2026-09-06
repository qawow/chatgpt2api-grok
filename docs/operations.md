# 运维与维护（chatgpt2api-grok）

面向本二开仓库的日常使用、升级、备份与排障。上游官方文档见原项目。部署机优先拉本仓库 GitHub Actions 构建的镜像，不要用上游官方 `ghcr.io/basketikun/chatgpt2api`。

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
| GPT 注册成功但 Codex `add_phone` | 默认已跳过 Codex（`skip_codex`/`OPENAI_SKIP_CODEX=1`）；若手动关闭跳过则回退 NextAuth session；号标 `session_only`，不进生图候选、不自动删 |
| 注册号无生图额度 / 秒死 | 入库后**后台** `fetch_remote_info`；无 refresh 号为 fragile，只标异常不剔除；默认跳过 Codex 的号为 `session_only` 不进生图 |
| session_only 要补 refresh | 号池管理 → ChatGPT → 行上钥匙图标 / 工具栏「Codex 补 refresh」（协议 OTP，`POST /api/accounts/codex-upgrade`）；注册成功默认后台 `auto_codex_upgrade`；`add_phone` 软失败保留 session 行；见 [gpt-register.md](gpt-register.md) §6.7.1 |
| OTP / OAuth 超时 | 换代理出口；CFD1 本身不走 OpenAI 代理 |

## 7. 开发

```bash
uv sync
uv run main.py          # 后端
cd web && bun install && bun run dev
uv run python -m unittest \
  test.test_gpt_register \
  test.test_gpt_register_engine \
  test.test_codex_upgrade \
  test.test_grok_pool -v
```

## 8. 安全

- 强随机 `auth-key` / `CHATGPT2API_AUTH_KEY`  
- `data/*.env`、号池 token 勿提交 git  
- 管理端口勿裸奔公网；需要时反代 + HTTPS + 访问控制  
- 本项目仅供学习研究，遵守各平台服务条款与法律  

运行时密钥文件：`data/grok_accounts.json`、`data/accounts.json`。详见 [grok-pool.md](./grok-pool.md)。  
