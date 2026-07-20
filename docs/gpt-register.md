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

---

## 5. HTTP API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/gpt-register/settings` | 读配置（密钥脱敏） |
| POST | `/api/gpt-register/settings` | 写配置 |
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

本仓库 `gpt_free_register` 在 [lxf746/any-auto-register](https://github.com/lxf746/any-auto-register) 协议流基础上做了这些加固：

| 点 | 说明 |
| --- | --- |
| passwordless 识别 | `authorize/continue` 返回 `email_otp_verification` 时区分新号 passwordless vs 已注册账号 |
| OTP 发送 | 优先 `POST /email-otp/send`，失败再 GET；429/5xx 短重试 |
| OTP 收信 | 透传 `otp_sent_at` + `before_ids`；CFD1 过滤旧邮件 Date，加快 poll |
| Sentinel | PoW + turnstile VM；失败最多 3 次；Codex 路径补齐 `decrypt_turnstile` |
| TLS 指纹 | 默认 `curl_cffi` `chrome142` + 匹配 UA/`sec-ch-ua`；可用 `HTTP_IMPERSONATE` 覆盖 |
| HTTP 重试 | 5xx/429 指数退避 + jitter，尊重 `Retry-After` |
| 密码 | 协议流强制大小写+数字+符号（与浏览器流 plugin 一致） |
| create_account | 5xx/429 重试并刷新单次 sentinel |

可选环境变量：

```bash
# 覆盖 TLS 指纹（默认 chrome142）
export HTTP_IMPERSONATE=chrome136
```

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
| `gpt_free_register/runner.py` | 单次注册、bootstrap、依赖预检 |
| `services/gpt_register_service.py` | 批量任务 + 本地/HTTP 入库 |
| `api/gpt_register.py` | 管理 API |
| `web/.../gpt-register-card.tsx` | 设置页 UI |
| `test/test_gpt_register.py` | 单测 |

运行测试：

```bash
uv run python -m unittest test.test_gpt_register -v
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
| ChatGPT 号池 `/api/accounts` | 注册成功默认写入 |
| Grok 号池 `/api/grok/*` | **无关**，不写入 |
| G2A `/api/g2a/*` | 只推 Grok 号，与 GPT 注册无关 |
| CPA / Sub2API | 其它导入通道，互不替代 |
