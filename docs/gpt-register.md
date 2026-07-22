# GPT Free 批量注册（内置模块）

设置页 **设置 → GPT注册** 调用仓库内 **`gpt_free_register/`** 模块，对 ChatGPT free 做**纯协议**注册；成功后写入 **ChatGPT 号池**（`/api/accounts` / `data/accounts.json`），**不会**进入 Grok 号池。

> 不再依赖宿主机上的 `/root/any-register-engines`。引擎代码已 vendoring 进本仓库。

相关入口：

| 方式 | 入口 |
| --- | --- |
| Web | 设置 → **GPT注册** |
| HTTP API | `/api/gpt-register/*`（admin Bearer） |
| Python | `from gpt_free_register import register_chatgpt_once` |
| 可选 CLI | `gpt_free_register/engines/register_cli.py`（`run_mode=subprocess`） |

---

## 1. 架构与数据流

```
Web / API
   │
   ▼
services/gpt_register_service.py     # 批量任务、进度、入库
   │  run_mode=inprocess（默认）
   ▼
gpt_free_register/runner.py          # 单次注册入口、init_db、依赖预检
   │
   ▼
gpt_free_register/engines/           # vendored 注册机（仅 chatgpt）
   │  protocol + mailbox
   ▼
Cloudflare D1 自建邮箱（OTP） + OpenAI 协议注册
   │  成功拿到 access_token
   ▼
account_service.add_account_items    # push_mode=local（默认）
   │  无 refresh_token → session_only/fragile
   ▼
account_service.fetch_remote_info    # 后台线程刷新真实 quota/status/type（不阻塞注册）
   │
   ▼
data/accounts.json                   # ChatGPT 号池
```

隔离：

| | ChatGPT 号池 | Grok 号池 |
|---|---|---|
| 存储 | `data/accounts.json` | `data/grok_accounts.json` |
| 管理 | `/api/accounts*` | `/api/grok/accounts*` |
| 本功能写入 | ✅ | ❌ |

---

## 2. 目录结构

```
chatgpt2api/
  gpt_free_register/
    __init__.py
    runner.py                      # 进程内注册入口
    README.md
    engines/                       # vendored engines（chatgpt only）
      platforms/chatgpt/
      core/
      providers/mailbox/           # 含 cloudflare_d1
      infrastructure/
      bootstrap.py
      register_cli.py
  services/gpt_register_service.py
  api/gpt_register.py
  web/src/app/settings/components/gpt-register-card.tsx
  data/                            # 运行时（gitignore）
    gpt_register.env               # 推荐放密钥
    gpt_register_config.json
    gpt_register_jobs.json
    register_engines.db            # 注册机 provider/capability 表
    accounts.json
```

默认：

- `engines_dir` → 仓库内 `gpt_free_register/engines`（Docker 下 `/app/gpt_free_register/engines`）
- `run_mode` → `inprocess`
- `push_mode` → `local`（进程内入库，不走 HTTP）

旧配置里的 `/root/any-register-engines` 或不存在的路径会在 normalize 时自动回退到内置目录。

---

## 3. 快速开始

### 3.1 部署（二开必须本地构建）

```bash
git clone https://github.com/qawow/chatgpt2api-grok.git
cd chatgpt2api-grok
mkdir -p data

# 编辑 config.json 的 auth-key，或：
# export CHATGPT2API_AUTH_KEY='your_strong_secret'

docker compose -f docker-compose.local.yml up -d --build
```

- Web：`http://localhost:8000`
- 容器内服务端口：**80**（host 映射 8000→80）
- **不要**用 `ghcr.io/basketikun/chatgpt2api:latest` 或默认 `docker compose up`

### 3.2 配置 CFD1 / 代理密钥

推荐写 `data/gpt_register.env`（已被 `data/` gitignore，**不要提交**）：

```bash
# data/gpt_register.env
REGISTER_PROXY_DEFAULT=socks5h://127.0.0.1:40000

CFD1_API_TOKEN=...
CFD1_ACCOUNT_ID=...
CFD1_DATABASE_ID=...
CFD1_DOMAIN=mail.example.com
# 可选
# CFD1_LOCAL_PART_PREFIX=xai.
# CFD1_LOCAL_PART_LENGTH=12
```

也可用进程环境变量（同名）。加载顺序（**不覆盖**已存在的环境变量）：

1. 进程环境  
2. `data/gpt_register.env`  
3. `gpt_free_register/engines/.env`（可选本地）

> SOCKS 代理必须安装 **PySocks**（镜像 `uv sync` 已带；缺依赖会在启动任务时预检失败）。

### 3.3 Web 操作

1. 登录管理后台（admin `auth-key`）  
2. **设置 → GPT注册**  
3. 填数量 / 并发 / 间隔 / 代理等（多数可留默认）  
4. **保存配置** → **开始注册**  
5. 看进度、日志；成功账号出现在 **ChatGPT 号池**

### 3.4 API 操作

所有接口需要：

```http
Authorization: Bearer <auth-key>
```

#### 读配置

```bash
export KEY='你的 auth-key'
export BASE='http://127.0.0.1:8000'

curl -s "$BASE/api/gpt-register/settings" \
  -H "Authorization: Bearer $KEY" | jq .
```

#### 写配置

```bash
curl -s -X POST "$BASE/api/gpt-register/settings" \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "count": 3,
    "concurrency": 1,
    "interval_secs": 5,
    "executor": "protocol",
    "mail_provider": "cloudflare_d1_api",
    "proxy": "socks5h://127.0.0.1:40000",
    "push_enabled": true,
    "push_mode": "local",
    "plan_type": "free"
  }' | jq .
```

#### 启动任务（可覆盖本次参数）

```bash
curl -s -X POST "$BASE/api/gpt-register/start" \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"count":1,"concurrency":1}' | jq .
```

返回里带 `job.job_id`。

#### 查进度

```bash
# 列表（最近任务）
curl -s "$BASE/api/gpt-register/jobs" \
  -H "Authorization: Bearer $KEY" | jq .

# 单个
JOB_ID='...'
curl -s "$BASE/api/gpt-register/jobs/$JOB_ID" \
  -H "Authorization: Bearer $KEY" | jq .
```

关注字段：

| 字段 | 含义 |
| --- | --- |
| `status` | `pending` / `running` / `done` / `failed` / `cancelled` |
| `completed` / `total` | 进度 |
| `success` / `failed` / `added` | 成功数 / 失败数 / 入库数 |
| `items[]` | 每号结果（email / error / added / `logs_tail`） |
| `logs[]` | 时间线日志（含引擎步骤；`level`: info/warn/error） |
| `summary` | 任务结束摘要（耗时、成功率、成功邮箱、失败明细、mode 等） |

#### 取消任务

```bash
curl -s -X POST "$BASE/api/gpt-register/jobs/$JOB_ID/cancel" \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{}' | jq .
```

约束：**同时只允许一个**运行中任务。

### 3.5 Python 进程内调用

```python
from gpt_free_register import register_chatgpt_once

result = register_chatgpt_once(
    settings={
        "mail_provider": "cloudflare_d1_api",
        "executor": "protocol",
        "proxy": "socks5h://127.0.0.1:40000",  # 可选
        "push_enabled": False,                   # 仅注册，不入库
    },
    log=print,
)
print(result["email"], bool(result.get("token")), result.get("error"))
```

成功大致形状：

```json
{
  "platform": "chatgpt",
  "email": "user@mail.example.com",
  "password": "...",
  "user_id": "...",
  "token": "<access_token>",
  "status": "registered",
  "extra": {
    "access_token": "...",
    "refresh_token": "...",
    "id_token": "...",
    "session_token": "..."
  }
}
```

---

## 4. 设置字段说明

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `count` | 1 | 注册数量 1–50 |
| `concurrency` | 1 | 并发 1–5；邮箱/代理不稳时建议 1 |
| `interval_secs` | 2 | 每批间隔秒 |
| `timeout_secs` | 600 | 单号超时（subprocess 严格生效） |
| `executor` | `protocol` | `protocol` / `headless` / `headed`；推荐 protocol |
| `mail_provider` | `cloudflare_d1_api` | 邮箱 provider |
| `cfd1_domain` | 空 | 覆盖 `CFD1_DOMAIN` |
| `proxy` | 空 | 出站代理；空则读 `REGISTER_PROXY*` |
| `bind_register_proxy` | true | 入库时把代理绑到账号 |
| `plan_type` | `free` | 写入号池的 type |
| `source_type` | 空 | 空则自动（register / codex） |
| `push_enabled` | true | 成功后是否入库 |
| `push_mode` | `local` | `local` 进程内入库；`http` 再 POST `/api/accounts` |
| `chatgpt2api_base_url` | 自动 | 仅 `http` 模式；Docker 内默认 `:80`，宿主机开发 `:8000` |
| `chatgpt2api_auth_key` | 空 | 空则用本机 `auth-key` |
| `dry_run` | false | 干跑：注册但不入库 |
| `engines_dir` | 内置 | 一般留空 |
| `run_mode` | `inprocess` | `subprocess` 才需要外部 Python |
| `python_bin` | 空 | 仅 subprocess |
| `skip_codex` | **true** | 跳过注册流里的 Codex 二次 OTP；入库为 `session_only`（更快；见 §6.7.1） |
| `auto_codex_upgrade` | **true** | `session_only` 入库后后台再跑 Codex 补 refresh；`add_phone` 等软失败保留 session 行 |
| `register_no_delay` | false | 关闭步骤间随机延迟（调试） |
| `so_collect_ms` | 空 | create_account 前 SO 采集等待毫秒；空=默认 5000；`0`=关闭 |

### 号池侧相关 API（补 refresh）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/accounts/codex-upgrade` | **主路径**：对已有邮箱跑 Codex OTP，写入 refresh 并可选 `access_token` 替换旧行 |
| POST | `/api/accounts/oauth/start` | 备用：浏览器 OAuth 起始（PKCE） |
| POST | `/api/accounts/oauth/finish` | 备用：粘贴 callback；可带 `replace_access_token` |

`POST /api/accounts/codex-upgrade` 请求体：

```json
{
  "email": "user@mail.example.com",
  "access_token": "旧 session access_token（可选，用于替换）",
  "password": "可选；空则从号池按 access_token 解析"
}
```

成功：`ok=true`，返回 `added` / `replaced` / 新账号摘要。  
软失败（如 `add_phone`）：HTTP 422，`ok=false`，`reason` + `logs`；**不删除** session 行。

---

## 5. HTTP API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/gpt-register/settings` | 读配置（密钥脱敏） |
| POST | `/api/gpt-register/settings` | 写配置（含 `skip_codex` / `auto_codex_upgrade` 等） |
| POST | `/api/gpt-register/start` | 启动任务（可带覆盖） |
| GET | `/api/gpt-register/jobs` | 任务列表 |
| GET | `/api/gpt-register/jobs/{id}` | 任务详情 |
| POST | `/api/gpt-register/jobs/{id}/cancel` | 取消 |

鉴权：与其它管理接口相同的 admin Bearer。

---

## 6. 运维与维护

### 6.1 日常数据文件

| 文件 | 作用 | 备份 |
| --- | --- | --- |
| `data/gpt_register.env` | CFD1 / 代理密钥 | ✅ 机密备份 |
| `data/gpt_register_config.json` | 表单默认值 | 可选 |
| `data/gpt_register_jobs.json` | 最近任务（约 20 条，含 logs/summary） | 可选 |
| `data/gpt_register_logs/<job_id>.json` | **单任务完成日志**（settings 脱敏 + items + summary） | 排障保留 |
| `data/register_engines.db` | provider / capability 表 | 可重建 |
| `data/accounts.json` | ChatGPT 号池（注册成功写入） | ✅ 重要 |
| `data/logs.jsonl` | 系统日志（含 GPT 注册任务结束摘要） | 可选 |

### 6.2 升级

```bash
cd chatgpt2api-grok
git pull
docker compose -f docker-compose.local.yml up -d --build
```

保留：`config.json`、`data/`（含 `gpt_register.env`、号池）。  
**不要**把空的宿主机 `./gpt_free_register` 挂进容器盖掉镜像内文件。

### 6.3 重建注册机 DB

若出现 `no such table: provider_definitions` 且自动 init 仍失败：

```bash
# 容器内路径
rm -f data/register_engines.db
docker compose -f docker-compose.local.yml restart
# 下次注册会自动 init_db + seed
```

### 6.4 本地开发热改注册机

默认镜像已含 `gpt_free_register/`。若要热挂载源码：

```yaml
# docker-compose.local.yml 临时打开：
volumes:
  - ./gpt_free_register:/app/gpt_free_register:ro
```

要求宿主机目录完整（含 `engines/platforms/chatgpt`）。

### 6.5 依赖

`pyproject.toml` / `uv.lock`：

- `curl-cffi`
- `sqlmodel`
- `requests`
- `PySocks`（**SOCKS 代理必需**）

```bash
uv sync
# 或
python -m pip install sqlmodel requests PySocks curl-cffi
```

### 6.6 健康检查清单

```bash
export KEY=...
export BASE=http://127.0.0.1:8000

# 1) 配置可读
curl -s "$BASE/api/gpt-register/settings" -H "Authorization: Bearer $KEY" | jq '.settings.engines_dir,.settings.run_mode'

# 2) 容器内 engines 存在（Docker）
docker exec chatgpt2api-local ls /app/gpt_free_register/engines/platforms/chatgpt/plugin.py

# 3) 密钥文件
test -f data/gpt_register.env && echo 'env ok'

# 4) 号池接口
curl -s "$BASE/api/accounts" -H "Authorization: Bearer $KEY" | jq 'keys'
```

### 6.7 注册机稳定性优化（相对 any-auto-register 基线）

本仓库 `gpt_free_register` 在 [lxf746/any-auto-register](https://github.com/lxf746/any-auto-register) 协议流基础上，并融合了
[yukkcat/chatgpt2api](https://github.com/yukkcat/chatgpt2api) 当前公开注册链路的关键行为，做了这些加固：

| 点 | 说明 |
| --- | --- |
| passwordless 优先 | authorize 若直接落到 `/email-verification`（auto-OTP），默认走 passwordless，**不再强行** `user/register` 设密码 |
| 跳过二次 continue | auto-OTP 会话上再 `authorize/continue` 会开新 session 并废码；默认跳过 continue |
| OTP 发送 | 优先 `POST /email-otp/send`，失败再 GET；429/5xx 短重试 |
| OTP 收信 | 透传 `otp_sent_at` + `before_ids`；CFD1 过滤旧邮件 Date，加快 poll |
| Sentinel dual-header | `openai-sentinel-token` + `openai-sentinel-so-token`；`oai-sc=0{c}` cookie |
| SO collect | `oauth_create_account` / `create_account` 前默认等待 ~5s（对齐官方 SDK 采集） |
| VM so-token | 优先 VM 求解 `t`/`so`（日志 `src=vm_t`），失败再回退 |
| 浏览器画像 | 每号独立 profile：TLS impersonate / UA / sec-ch-ua / 硬件一致；Windows 权重默认 70% |
| CFD1 邮箱 | CFD1 建号/收信**不走** OpenAI 代理，避免 SOCKS 超时拖垮 OTP |
| TLS 指纹 | 默认 `curl_cffi` `chrome142` + 匹配 Client Hints |
| HTTP 重试 | 5xx/429 指数退避 + jitter，尊重 `Retry-After` |
| create_account | 5xx/429 重试并刷新 sentinel；密码路径失败可回退 passwordless（清旧 auto-OTP 标记） |

#### 推荐协议流（当前默认）

```
chatgpt.com NextAuth signin
  → auth.openai.com authorize (login_or_signup)
  → 若 final_url=/email-verification：跳过 continue，passwordless
  → 信任 auto-OTP（或显式 email-otp/send）
  → email-otp/validate (+ sentinel flow=email_otp_validate)
  → about_you → create_account (+ dual sentinel, SO collect 5s)
  → chatgpt.com callback → /api/auth/session
```

日志中应能看到类似：

```text
passwordless 注册 OTP 流程: mode=passwordless_signup (auto-otp skip continue; yukkcat-aligned)
Sentinel so-token ready: flow=authorize_continue so_len=... src=vm_t
Sentinel SO collect wait: 5.0s flow=oauth_create_account
create_account so-token attached: len=... src=full
验证码校验状态: 200
NextAuth session 获取成功
```

#### 可选环境变量

写在 `data/gpt_register.env` 或进程环境均可（**勿提交密钥**）：

```bash
# ---- 路径策略（推荐默认）----
# 跳过 Codex 二次 OTP（默认 1）。free 号几乎总是 add_phone 失败，白耗 ~15–30s
OPENAI_SKIP_CODEX=1
# auto-OTP 时跳过 authorize/continue（默认 1）
OPENAI_SKIP_CONTINUE_ON_AUTO_OTP=1
# 信任 authorize 已触发的 OTP，跳过显式 send（默认 1）
OPENAI_TRUST_AUTO_OTP=1
# auto-OTP 时是否强制密码路径（默认 0；yukkcat 对齐为 0）
OPENAI_FORCE_PASSWORD_ON_AUTO_OTP=0
# 非 auto-OTP / 明确 passwordless_signup 时是否优先密码创建（默认 1）
OPENAI_PREFER_PASSWORD_SIGNUP=1

# ---- Sentinel / SO ----
# create_account 前 SO 采集等待毫秒；空=默认 5000；0=关闭
# OPENAI_SO_COLLECT_MS=5000
# 优先 VM 解 so-token（默认 1）
OPENAI_SO_PREFER_VM=1
# create_account 把 so 镜像进 t 字段（默认 1）
OPENAI_SO_MIRROR_INTO_T=1
# create_account so 最大长度截断（默认 4096）
# OPENAI_CREATE_ACCOUNT_SO_MAX=4096
# Sentinel SDK/frame 版本（默认 20260219f9f6）
# SENTINEL_SDK_VERSION=20260219f9f6
# SENTINEL_FRAME_VERSION=20260219f9f6

# ---- 浏览器画像 / TLS ----
# 固定 TLS 指纹（默认随机 chrome142/136/131/124，偏 142）
# HTTP_IMPERSONATE=chrome142
# 平台：windows|mac|auto（默认 auto，Windows 权重 70）
# OPENAI_BROWSER_PLATFORM=auto
# OPENAI_BROWSER_WINDOWS_WEIGHT=70
# 发送 DNT/Sec-GPC（默认 1）
OPENAI_SEND_GPC=1

# ---- OAuth / 地区 / 调试 ----
# NextAuth screen_hint（默认 login_or_signup）
# OPENAI_SCREEN_HINT=login_or_signup
# 拦截地区列表（默认 CN）
# OPENAI_BLOCK_REGIONS=CN
# login_challenge 短超时快速失败（默认 1 / 35s）
OPENAI_OTP_LOGIN_CHALLENGE_FAST_FAIL=1
# OPENAI_OTP_LOGIN_CHALLENGE_PROBE_SECS=35
# 关闭步骤间随机延迟（调试用）
# OPENAI_REGISTER_NO_DELAY=1
```

> 实测：auto-OTP 后若 `OPENAI_FORCE_PASSWORD_ON_AUTO_OTP=1`（或旧逻辑强制密码），
> 常见 `account_creation_failed` → OTP `invalid_auth_step` / `invalid_state`。
> 保持默认 passwordless 即可。

### 6.7.1 耗时优化（默认开启）

实测单号成功约 47–63s，主要浪费在 **Codex 二次 OTP**（free 号几乎必 `add_phone` 失败）以及入库后同步 `fetch_remote_info`。

| 优化项 | 默认 | 说明 |
| --- | --- | --- |
| 跳过 Codex 二次 OTP | **开**（`skip_codex=true` / `OPENAI_SKIP_CODEX=1`） | NextAuth session 成功后直接入库，标 `session_only`；可省约 15–30s |
| 入库后额度刷新 | 后台线程 | 不再阻塞注册 worker；默认仍写 bootstrap quota |
| 任务日志落盘 | 节流（约 8 行 / 2s） | 减少 `gpt_register_jobs.json` 频繁重写 |
| CFD1 OTP 早期轮询 | 前 12s ~0.8–1.2s | 验证码通常很快到达 |
| 关闭步骤随机延迟 | 关（可选 `register_no_delay` / `OPENAI_REGISTER_NO_DELAY=1`） | 调试用；默认保留轻微抖动 |
| SO collect | 默认 5s create_account | 可用 `so_collect_ms` / `OPENAI_SO_COLLECT_MS` 覆盖；`0` 关闭 |

Web：设置 → GPT注册 →「跳过 Codex 二次 OTP（推荐）」/「关闭步骤间随机延迟」。  
保存/启动时字段经 `POST /api/gpt-register/settings` 与 `start` 的 Pydantic 模型（含 `skip_codex` / `register_no_delay` / `so_collect_ms`）；未声明字段会被丢弃，旧版因此无法取消「跳过 Codex」。

> 跳过 Codex 后拿到的是 **session_only** 号：可入库排查，**不参与生图候选**、不因 401 自动删除。
>
> **默认会在入库后后台再跑 Codex 补 refresh**（`auto_codex_upgrade=true`）：对同一邮箱走 Codex client_id + CFD1 OTP。
> 成功则写入 `refresh_token` 并替换旧 session 行；遇到 `add_phone` / OTP 失败则**软失败**，保留 session_only 行。
>
> **已有 session_only 号怎么补 refresh？** 在 **号池管理 → ChatGPT**：
> 1. 行操作点钥匙图标 **Codex 补 refresh**（或勾选后点工具栏同名按钮）  
> 2. 后端对同一邮箱再跑 Codex OTP（`POST /api/accounts/codex-upgrade`），无需浏览器粘贴 callback  
> 3. 成功写入带 `refresh_token` 的新凭证并删除旧 session 行；`add_phone` 等失败会提示原因，号仍保留  
> 4. 备用：导入对话框里的浏览器 OAuth 粘贴 callback（`/api/accounts/oauth/*`）仍可用

### 6.8 完成日志与排障输出


任务结束时会同时写入：

1. **UI / API**：`job.logs` 时间线 + `job.summary` 摘要 + 每号 `items[].logs_tail`
2. **Docker stdout**：`[gpt-register:<job前8位>] ...`（`docker logs -f chatgpt2api-local`）
3. **持久文件**：`data/gpt_register_logs/<job_id>.json`（volume 可保留，含脱敏 settings）
4. **系统日志**：`data/logs.jsonl` 一条 `type=account` 的「GPT注册任务结束 …」

查看示例：

```bash
# 最近一次任务摘要
JOB=$(ls -t data/gpt_register_logs/*.json 2>/dev/null | head -1)
jq '.summary,.items[]? | {ok,email,error,logs_tail}' "$JOB"

# 容器日志过滤
docker logs --tail 200 chatgpt2api-local 2>&1 | grep gpt-register

# API
curl -s "$BASE/api/gpt-register/jobs/$JOB_ID" -H "Authorization: Bearer $KEY" \
  | jq '{status,summary,logs:(.logs[-20:]),items}'
```

`summary` 字段示例：`duration_secs` / `success_rate` / `success_emails` / `failed_items` /
`run_mode` / `mail_provider` / `engines_dir` / `push_mode`。密钥不会写入日志文件。

### 6.9 常见故障


| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `注册机目录不存在: /app/gpt_free_register/engines` | 旧镜像 / 空 volume 盖掉 | `docker compose -f docker-compose.local.yml up -d --build`；勿空挂 `./gpt_free_register` |
| `no such table: provider_definitions` | 未 init_db 或 DB 只读 | 确认 `REGISTER_ENGINES_DATABASE_URL` 指向 `/app/data/...`；删坏库重启 |
| `Missing dependencies for SOCKS support` | 无 PySocks | 重建镜像或 `pip install PySocks` |
| `Cloudflare D1 邮箱缺少配置` | 未配 CFD1 | 写 `data/gpt_register.env` |
| 注册成功但 `added=0` | dry_run / push 关 / token 空 | 查 `push_enabled`、`dry_run`、任务 `items` |
| HTTP 推送连不上 | 容器内用了 host 的 `:8000` | 默认用 `push_mode=local`；http 模式 Docker 内用 `:80` |
| OTP 超时 / 风控 | 域名信誉、代理出口、OpenAI 策略 | 换域名/代理，降并发，看 `logs`/`items`/`summary` 与 `data/gpt_register_logs/*.json` |
| `account_creation_failed` 后 OTP `invalid_auth_step` | auto-OTP 会话被强制密码路径打坏 | 保持 `OPENAI_FORCE_PASSWORD_ON_AUTO_OTP=0`；确认日志有 `yukkcat-aligned` passwordless |
| OTP `invalid_state` / session no longer valid | continue 二次提交或会话过期 | 保持 `OPENAI_SKIP_CONTINUE_ON_AUTO_OTP=1`；换干净代理重开流程 |
| `IP 地理位置不支持` / OAuth reset | 出口被拦或代理不稳 | 换 TW 等可用出口；检查 `OPENAI_BLOCK_REGIONS` |
| Codex CLI `add_phone required` | Codex 路径额外要手机 | **默认已跳过 Codex**（`skip_codex`/`OPENAI_SKIP_CODEX=1`）。若手动关闭跳过，会回退 NextAuth session token，任务仍可 `registered` |
| 注册成功但生图额度 0 / 选不到号 | 入库默认 bootstrap quota；free 上游 `image_gen.remaining` 常为 0；无 refresh 的 session 号不参与生图 | 入库后**后台** `fetch_remote_info`；看号池 `quota/status/session_only`；session 号需 Codex refresh 才可生图 |
| 注册号「秒死」被自动删 | NextAuth-only access 无 refresh，401 后 `auto_remove_invalid_accounts` 剔除 | 现已标 `session_only/fragile`：排除生图候选且**不自动删除**，只标异常保留排查 |

---

## 7. Docker 要点

镜像构建会：

```dockerfile
COPY gpt_free_register ./gpt_free_register
ENV REGISTER_ENGINES_DATABASE_URL=sqlite:////app/data/register_engines.db
```

`docker-compose.local.yml`：

- 挂载 `./data:/app/data`、`./config.json`
- **默认不挂** `./gpt_free_register`（防止空目录遮盖镜像）
- 设置 `REGISTER_ENGINES_DATABASE_URL`

密钥：`data/gpt_register.env` → 容器 `/app/data/gpt_register.env`。

---

## 8. 代码入口

| 路径 | 作用 |
| --- | --- |
| `gpt_free_register/runner.py` | 单次注册、bootstrap、依赖预检；CFD1 邮箱不绑 OpenAI 代理 |
| `gpt_free_register/engines/platforms/chatgpt/register.py` | 协议注册主流程（passwordless / Sentinel dual-header） |
| `gpt_free_register/engines/platforms/chatgpt/browser_profile.py` | 每号浏览器画像（TLS/UA/CH 一致） |
| `gpt_free_register/engines/platforms/chatgpt/constants.py` | Sentinel SDK 版本、OAuth 端点 |
| `services/gpt_register_service.py` | 批量任务 + 本地/HTTP 入库（session_only + 后台 fetch_remote_info + 日志节流）+ 完成摘要 |
| `services/account_service.py` | 号池：`session_only`/`fragile` 门禁、生图选号、invalid 自动移除策略 |
| `services/oauth_login_service.py` | 浏览器 OAuth PKCE；换 token 三件套 |
| `api/gpt_register.py` | 管理 API（含 `skip_codex` 等 latency 字段） |
| `api/accounts.py` | `codex-upgrade`（主路径）+ `oauth/start|finish`（备用，`replace_access_token`） |
| `gpt_free_register/codex_upgrade.py` | 协议 Codex OTP 补齐既有邮箱 |
| `services/codex_upgrade_service.py` | 入库后自动调度 + 写号池 |
| `web/.../gpt-register-card.tsx` | 设置页 UI |
| `web/.../accounts/page.tsx` | 号池「Codex 补 refresh」按钮 / session 徽章 |
| `web/.../account-import-dialog.tsx` | OAuth 导入 + 升级模式（备用；主路径为 Codex） |
| `web/.../gpt-register-card.tsx` | `auto_codex_upgrade` 开关 |
| `test/test_gpt_register.py` | 服务层单测 |
| `test/test_gpt_register_engine.py` | 引擎路径单测 |
| `test/test_oauth_login_api.py` | OAuth finish 模型与 replace 逻辑（备用） |
| `test/test_codex_upgrade.py` | Codex 补 refresh + 入库自动调度 |

运行测试：

```bash
uv run python -m unittest \
  test.test_gpt_register \
  test.test_gpt_register_engine \
  test.test_codex_upgrade \
  test.test_oauth_login_api -v
```

---

## 9. 安全与合规

- 密钥只放 `data/gpt_register.env` / 环境变量，**禁止**提交 git  
- 注册与号池仅供个人学习研究；遵守 OpenAI 条款与当地法律  
- 勿用重要邮箱域名做大规模注册；失败多为风控/OTP 环境问题  

---

## 10. 与其它模块关系

| 模块 | 关系 |
| --- | --- |
| ChatGPT 号池 `/api/accounts` | 注册成功默认写入；session_only 可在号池页 Codex 补 refresh（OAuth 备用） |
| 号池 Codex `/api/accounts/codex-upgrade` | 协议 OTP 补 `refresh_token`（主路径） |
| 号池 OAuth `/api/accounts/oauth/*` | 浏览器登录补 `refresh_token`（备用）；finish 可 `replace_access_token` |
| Grok 号池 `/api/grok/*` | **无关**，不写入 |
| G2A `/api/g2a/*` | 只推 Grok 号，与 GPT 注册无关 |
| CPA / Sub2API | 其它导入通道，互不替代 |
