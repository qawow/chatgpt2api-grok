# 运维与维护（chatgpt2api-grok）

面向本二开仓库的日常使用、升级、备份与排障。当前版本 **1.8.9**（仓库根目录 `VERSION`，说明见 [CHANGELOG.md](../CHANGELOG.md)）。上游官方文档见原项目。部署机优先拉本仓库 GitHub Actions 构建的镜像，不要用上游官方 `ghcr.io/basketikun/chatgpt2api`。`docker-compose.yml` 默认 `:latest`，只在打 `v*` tag 时更新；分支 push 只出 `:sha-<commit>`。

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

# waifu2x.net 超分（非官方网页协议，需 Turnstile / 打码密钥 / Patreon ses_id）
curl -s "$BASE/v1/waifu2x/status" -H "Authorization: Bearer $KEY"
curl -s "$BASE/v1/waifu2x" \
  -H "Authorization: Bearer $KEY" \
  -F "file=@input.png;type=image/png" \
  -F "style=art" -F "noise=medium" -F "scale=2x" \
  --output out.png
```

完整参数与验证码说明：[waifu2x.md](./waifu2x.md)。

```bash
# 豆包网页生图（登录 Cookie；打码外接 captcha / CAPTCHA_SOLVE_URL）
curl -s "$BASE/v1/doubao/status" -H "Authorization: Bearer $KEY"
curl -s "$BASE/v1/doubao" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"prompt":"一只橘猫","cookies":"sessionid=..."}'

# 360智图文生图（QHPass Cookie；未登录 401，欠费 402）
curl -s "$BASE/v1/zhitu360/status" -H "Authorization: Bearer $KEY"
curl -s "$BASE/v1/zhitu360" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"prompt":"一只橘猫","model":"jimeng","ratio":"1:1"}'
```

文档：[doubao.md](./doubao.md)、[zhitu360.md](./zhitu360.md)。

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

日志保留：`log_retention_days`（默认 30，`0` 保留全部）在启动时与每 6 小时流式裁剪。列表按文件尾部分块读取；删除和保留期裁剪仍需扫描历史，坏行保留。日志文件由同一个 `LogService` 实例协调写入、删除和裁剪；多进程部署应交给单一写者或外部日志系统，当前锁仅覆盖实例内线程。

### Resin 与代理检查

`POST /api/proxy/test` 检查选中的网关，保留完整用户名及会话标签，沿用运行配置中的 TLS 选项；显式输入覆盖全局代理。SOCKS 使用 `socks5h` 远端 DNS。结果中的 `gateway` 和 `proxy_id` 可用于关联同一代理配置，后者由协议、网关和完整凭据等信息的哈希组成，凭据变化会产生新标识。

- `reachable` 只表示收到了 HTTP 响应；`ok` 还要求 CSRF 检查端点返回有效的 200 JSON 结构。连通检查通过与账号/API 可用性是两回事。
- `failure_kind` 区分配置、代理认证（407 或 SOCKS 认证错误）、握手、DNS、TLS、超时、HTTP 401/403、挑战页、429、上游 5xx 和响应结构异常；支持时附带 `http_status`、`curl_code`、`retry_after_seconds`。
- 默认请求预算为 15 秒，上限 60 秒；标量预算在本地 TLS 库回退重试之间共享。检查沿用明确选中的网关，无隐式出口轮换。
- 可选的出口观测缓存仅记录显式诊断。一次 trace 请求仅关联该次返回的 IP；缓存有效期 15 分钟、最多 1024 项、每项 32 条样本。成功和失败都会过期，损坏文件会报错并保留原件。该缓存独立于账号选号和注册调度。

### 日志完整性与脱敏

`GET /api/logs` 保留原有 `items`，新增 `has_more` 和 `next_cursor`；默认 200 条，`limit` 范围 1–1000。下一页携带相同筛选和返回的游标。游标锁定追加前的快照：新增记录在刷新后出现；删除或裁剪导致快照失效时返回 409 / `log_cursor_expired`，前端刷新当前筛选。历史缺少时区的记录保留 `local_timezone_unknown`，带时区的筛选仅比较有明确时间基准的记录。

日志页每批加载 100 条，可继续“加载更早日志”；“已加载”与“全选已加载”描述当前已取得范围，末页显示“已到最早记录”。旧服务端缺少分页字段时显示总量未知。

控制台 Logger、系统日志写入/读取、注册任务历史和完成报告共用凭据脱敏，覆盖代理 userinfo、密码、会话 token、Authorization、Cookie 和 OTP。错误中的 HTML 省略，保留故障分类；历史文件读取时同样脱敏，既有磁盘副本仍按原有权限管理。配置和账号凭据存储保持业务用途，脱敏对象仅是日志和任务历史输出。账号周期观测略过重复时间戳，额度、状态和图片门控变化仍留事件；匿名 `token:<hash>` 在更新与删除记录之间保持可关联。

验证命令（全部使用本地 fixture）：

```bash
CHATGPT2API_AUTH_KEY=chatgpt2api .venv/bin/python -m unittest discover -s test -t .
.venv/bin/python -m pytest -q test/test_log_pagination.py test/test_log_retention.py test/test_call_log_image_urls.py
npm --prefix web run build
```

TypeScript 独立检查在 `web` 目录运行 `node_modules/.bin/tsc --noEmit --incremental false`。Next.js 当前配置会跳过构建内类型校验，两项都应验证。本地源码与 `web/out` 构建产物需要随应用镜像部署；编译通过并不会更新其他主机正在运行的容器。

### 出口（代理）熔断

连接类错误（代理拒连 / 超时 / TLS）会按出口记失败：

- `egress_blacklist_failure_threshold`（默认 3）：窗口内达到该次数才把出口拉黑 10 分钟。单次瞬断不再拉黑唯一出口，避免「一次抖动 → 10 分钟全量回落直连」。
- `egress_blacklist_window_secs`（默认 60）：失败计数窗口。
- `OPENSSL_internal:invalid library` 是本地 curl_cffi/HTTP2 问题，不计失败、也不拉黑（见 1.8.0 说明）。

排障：代理偶发连接失败时先看是否命中熔断；`POST /api/proxy/test` 通过选定代理检查目标端点，结果按具体故障分类处理。

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

### 自动补号容灾

设置 → GPT 注册中可调：最少/目标可用账号、每批数量、检查间隔、成功补号间隔、失败冷却和网络熔断阈值。`circuit_break=0` 关闭连续网络故障熔断，但单任务仍受注册 `timeout_secs` 限制；建议保留默认值 3。自动补号只统计当前真正可生图的账号，异常/吊销账号不会因为本地残余额度被当作库存。补号任务完成后会等待下一次调度，避免失败时持续消耗代理和邮箱额度。除「按数量」外还可开**按额度补号**（`auto_replenish_min_total_quota`，默认 `0` 关闭）：可用账号剩余生图额度总和低于阈值即补号，与数量阈值是 OR 关系，适合「号够多但额度将耗尽」的场景；详见 [gpt-register.md](gpt-register.md) §自动补号。

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
  test.test_web_fallback \
  test.test_ssrf \
  test.test_body_limit \
  test.test_waifu2x_api \
  test.test_cn_images_api -v
```

## 8. 安全

- 强随机 `auth-key` / `CHATGPT2API_AUTH_KEY`  
- `data/*.env`、号池 token 勿提交 git  
- 管理端口勿裸奔公网；需要时反代 + HTTPS + 访问控制  
- **SSRF**：所有用户提供的图片 URL（`/v1/images/edits` 的 `image_url`、`/v1/chat/completions` 与 `/v1/responses` 消息内的 `image_url`、`/v1/waifu2x` 的 `url`）都经 `utils/ssrf.py` 校验——只允许 `http/https`，拒绝私网 / 回环 / 链路本地 / 云元数据地址（含 IPv6 与 IPv4-mapped），无法证明为公网的域名 fail-closed 拒绝；重定向逐跳校验，不安全目标不会回落到其他出口。
- **请求体上限**：`max_request_body_mb`（默认 256 MB，`0` 不限制）拦截超大 JSON / multipart 上传，超限返回 413；`CHATGPT2API_LIMIT_CONCURRENCY`（默认 256）限制并发连接数。
- 本项目仅供学习研究，遵守各平台服务条款与法律  

运行时密钥文件：`data/grok_accounts.json`、`data/accounts.json`。详见 [grok-pool.md](./grok-pool.md)。  
