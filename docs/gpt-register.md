# GPT Free 批量注册（内置模块）

设置页 **设置 → GPT注册** 调用仓库内 **`gpt_free_register/`** 模块做 ChatGPT free 纯协议注册，成功后写入 **ChatGPT 号池**（`/api/accounts`，不进 Grok 池）。

不再依赖宿主机上的 `/root/any-register-engines`。

## 内置位置

```
chatgpt2api/
  gpt_free_register/
    __init__.py
    runner.py                 # 进程内注册入口
    engines/                  # vendored 注册机（仅 chatgpt 平台 + core + providers）
      platforms/chatgpt/
      core/
      providers/
      infrastructure/
      bootstrap.py
      register_cli.py         # 可选 CLI / subprocess 模式
```

默认：

- `engines_dir` = 仓库内 `gpt_free_register/engines`
- `run_mode` = `inprocess`（同进程调用，不 spawn 外部 Python）

旧配置里的 `/root/any-register-engines` 会在 normalize 时自动迁移到内置路径。

## 邮箱 / 代理密钥（不要进 git）

把 CFD1 与代理写到下面任一位置（`load_dotenv` 不覆盖已有环境变量）：

1. 进程环境变量  
2. `data/gpt_register.env`（推荐，已被 `data/` gitignore）  
3. `gpt_free_register/engines/.env`（本地可选，仓库已 ignore `.env`）

示例 `data/gpt_register.env`：

```bash
REGISTER_PROXY_DEFAULT=socks5h://127.0.0.1:40000
CFD1_API_TOKEN=...
CFD1_ACCOUNT_ID=...
CFD1_DATABASE_ID=...
CFD1_DOMAIN=mail.example.com
```

设置页也可临时覆盖：代理、CFD1 域名等。

## 设置页字段

| 字段 | 说明 |
| --- | --- |
| 注册数量 | 1–50 |
| 并发 | 1–5，默认 1 |
| 间隔 | 每批之间秒数 |
| 单号超时 | 单次注册超时（subprocess 用；inprocess 为逻辑超时参考） |
| 执行器 | `protocol`（推荐） |
| 邮箱 Provider | 默认 `cloudflare_d1_api` |
| CFD1 域名覆盖 | 可选 |
| 出站代理 | 可选；留空读 `REGISTER_PROXY*` |
| 注册机目录 | 留空=内置；一般不用改 |
| Python 路径 | 仅 `run_mode=subprocess` 需要 |

## API（admin Bearer）

```bash
export KEY='你的 auth-key'

curl -s http://127.0.0.1:8000/api/gpt-register/settings \
  -H "Authorization: Bearer $KEY"

curl -s -X POST http://127.0.0.1:8000/api/gpt-register/start \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"count":1,"concurrency":1}'
```

## Docker

镜像构建会 `COPY gpt_free_register ./gpt_free_register`。  
若仍看到 `/app/gpt_free_register/engines` 不存在，说明当前容器是**旧镜像**，需要重新本地构建：

```bash
docker compose -f docker-compose.local.yml up -d --build
```

不要用官方 `ghcr.io/basketikun/chatgpt2api` 镜像，也不要用默认 `docker-compose.yml` 拉上游镜像。

密钥挂载在 `./data` 下写 `data/gpt_register.env` 即可（容器内 `/app/data/gpt_register.env`）。

注意：

- **不要**把空的宿主机 `./gpt_free_register` 挂进容器，否则会盖掉镜像内 builtin engines。  
  本地改注册机时再临时加：`- ./gpt_free_register:/app/gpt_free_register:ro`
- 注册机内部 sqlite 默认写 `REGISTER_ENGINES_DATABASE_URL=sqlite:////app/data/register_engines.db`（可写 data 卷）
- 首次进程内注册会自动 `init_db()` 并 seed `provider_definitions`；若仍报 `no such table: provider_definitions`，检查该环境变量是否指向只读路径，或删除损坏的 db 后重启
- 默认 `push_mode=local`：成功账号直接写本进程号池，不依赖 `127.0.0.1:8000`
- 容器内服务监听 **:80**（host 映射常见 8000/3000 → 80）

## 数据文件

| 文件 | 作用 |
| --- | --- |
| `data/gpt_register_config.json` | 默认表单 |
| `data/gpt_register_jobs.json` | 最近任务 |
| `data/gpt_register.env` | 密钥（推荐） |

## 代码入口

| 路径 | 作用 |
| --- | --- |
| `gpt_free_register/runner.py` | 进程内单次注册 |
| `services/gpt_register_service.py` | 批量任务 + 入库 |
| `api/gpt_register.py` | 管理 API |
| `web/.../gpt-register-card.tsx` | 设置页 |

## 依赖

主项目需：`curl-cffi`、`sqlmodel`、`requests`、`PySocks`（已写入 `pyproject.toml` / `uv.lock`）。

SOCKS 代理（`socks5h://...`）**必须**有 `PySocks`，否则 CFD1 读信会报：

`Missing dependencies for SOCKS support.`

```bash
# 开发环境
uv sync
# 或
python -m pip install sqlmodel requests PySocks
```

Docker 重建镜像后依赖会随 `uv sync --frozen` 装进镜像。