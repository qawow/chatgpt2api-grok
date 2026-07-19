# GPT Free 批量注册（设置页）

在 chatgpt2api **设置 → GPT注册** 里批量跑 [any-register-engines](https://github.com/) 的 ChatGPT 纯协议注册，成功后写入 **ChatGPT 号池**（`/api/accounts`，不进 Grok 池）。

## 前置

1. 本机已有 any-register-engines，且能命令行注册：

```bash
/root/any-register-engines/.venv/bin/python \
  /root/any-register-engines/register_cli.py register chatgpt \
  --executor protocol \
  --mail-provider cloudflare_d1_api \
  --push-chatgpt2api
```

2. 邮箱 / 代理 / CFD1 等仍在 **注册机目录** 的 `.env` 配置（`CFD1_*`、`REGISTER_PROXY_DEFAULT` 等）。设置页只覆盖本任务参数。

3. 推送默认走本机：

- Base URL：`http://127.0.0.1:8000`
- Auth Key：留空则用 chatgpt2api 的 `auth-key` / `CHATGPT2API_AUTH_KEY`

## 设置页字段

| 字段 | 说明 |
| --- | --- |
| 注册数量 | 1–50 |
| 并发 | 1–5，默认 1 |
| 间隔 | 每批之间秒数 |
| 单号超时 | 子进程超时 |
| 执行器 | `protocol`（推荐）/ headless / headed |
| 邮箱 Provider | 默认 `cloudflare_d1_api` |
| CFD1 域名覆盖 | 可选，写入子进程 `CFD1_DOMAIN` |
| 出站代理 | 可选，传给 `register_cli --proxy` |
| 绑定注册代理 | 入库时把代理写到账号 |
| 成功后推送号池 | 默认开；干跑可关入库 |
| 注册机目录 / Python | 默认 `/root/any-register-engines` 与其 `.venv` |

## API（admin Bearer）

```bash
export KEY='你的 auth-key'

# 读/写默认配置
curl -s http://127.0.0.1:8000/api/gpt-register/settings \
  -H "Authorization: Bearer $KEY"

curl -s -X POST http://127.0.0.1:8000/api/gpt-register/settings \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"count":3,"concurrency":1,"interval_secs":5,"proxy":"socks5h://127.0.0.1:40000"}'

# 启动任务（可带覆盖）
curl -s -X POST http://127.0.0.1:8000/api/gpt-register/start \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"count":2}'

# 进度
curl -s http://127.0.0.1:8000/api/gpt-register/jobs \
  -H "Authorization: Bearer $KEY"

curl -s http://127.0.0.1:8000/api/gpt-register/jobs/<job_id> \
  -H "Authorization: Bearer $KEY"

# 取消
curl -s -X POST http://127.0.0.1:8000/api/gpt-register/jobs/<job_id>/cancel \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{}'
```

同时只允许 **一个** 运行中任务；重启后未完成任务标为 `failed`。

## 数据文件

| 文件 | 作用 |
| --- | --- |
| `data/gpt_register_config.json` | 默认表单配置 |
| `data/gpt_register_jobs.json` | 最近任务（最多 20） |

## 代码入口

| 路径 | 作用 |
| --- | --- |
| `services/gpt_register_service.py` | 配置 + 后台任务 + 调 register_cli |
| `api/gpt_register.py` | 管理 API |
| `web/.../gpt-register-card.tsx` | 设置页 |
| `test/test_gpt_register.py` | 单测 |

## 注意

- 只进 **ChatGPT** 号池，不会写入 `grok_accounts.json`。
- 注册成功率依赖邮箱域名信誉、代理出口、OpenAI 风控；失败日志在任务 `logs` / `items`。
- Docker 部署需把 any-register-engines 挂进容器，或把 `engines_dir` 指到容器内路径，并保证 Python 依赖可用。
