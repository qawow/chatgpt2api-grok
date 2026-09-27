ARG BUILDPLATFORM
ARG TARGETPLATFORM
ARG TARGETARCH

FROM --platform=$BUILDPLATFORM node:22-alpine AS web-build

WORKDIR /app/web

# npm 只认 package-lock.json（此前 COPY 的是 bun.lock，对 npm 完全无效 = 每次构建
# 都重新解析依赖、不可复现）。仓库里已提交 web/package-lock.json，改用 npm ci 锁版本。
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY VERSION /app/VERSION
COPY CHANGELOG.md /app/CHANGELOG.md
COPY web ./
RUN NEXT_PUBLIC_APP_VERSION="$(cat /app/VERSION)" npm run build


FROM --platform=$TARGETPLATFORM python:3.13-slim AS app

ARG TARGETPLATFORM
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    HOME=/app \
    REGISTER_ENGINES_DATABASE_URL=sqlite:////app/data/register_engines.db \
    TIKTOKEN_CACHE_DIR=/app/.tiktoken_cache
# curl_cffi ships BoringSSL. Debian OPENSSL_CONF / openssl.cnf makes it raise
# OPENSSL_internal:invalid library (curl 35) on Linux/WSL2 Docker hosts.
ENV OPENSSL_CONF=

WORKDIR /app

# 安装系统依赖
# - git: Git 存储后端需要
# 不再安装 gcc / libpq-dev：依赖是 psycopg2-binary，自带 manylinux wheel 且静态链接
# libpq；uv.lock 里全部运行期依赖在 cp313 的 x86_64 与 aarch64 上都有预编译 wheel
# （`uv sync --frozen --no-dev --no-install-project --no-build` 可通过），
# 编译工具链既用不到又留在运行镜像里，纯属体积与攻击面负担。
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

# 非 root 运行。先建账号再装依赖，让 .venv / 缓存直接以 app 属主生成，
# 避免最后再 chown -R 把整个 venv 复制到新层。
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /app/data /app/.tiktoken_cache \
    && chown -R app:app /app

# 默认仍以 root 运行：老部署的 ./data 由宿主机 root 持有且凭据文件是 0600，
# 直接切到非 root 会读不到号池 / 配置。想以非 root 运行，先在宿主机执行
#   sudo chown -R 10001:10001 ./data && sudo chown 10001:10001 config.json
# 再在 compose 里打开 `user: "10001:10001"`。账号 app(10001) 与属主已就位。

COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
RUN uv run python -c "import tiktoken; tiktoken.get_encoding('o200k_base'); tiktoken.get_encoding('cl100k_base')" \
    || echo "tiktoken encoding prefetch skipped"

COPY --chown=app:app main.py ./
# config.json 不打包进镜像：含密钥且被 gitignore，运行时由 compose 挂载
# （docker-compose.yml: ./config.json:/app/config.json）。缺失时 config.py 回退默认值。
COPY --chown=app:app VERSION ./
COPY --chown=app:app api ./api
COPY --chown=app:app services ./services
COPY --chown=app:app utils ./utils
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app gpt_free_register ./gpt_free_register
COPY --chown=app:app docs ./docs
COPY --from=web-build --chown=app:app /app/web/out ./web_dist

# 运行期入口：把原本硬编码在 CMD 里的 uvicorn 参数改成可用环境变量覆盖，
# 与 .env.example / docs/operations.md 的说明对齐（默认值保持不变）。
#   PORT                          容器内监听端口，默认 80
#   CHATGPT2API_ACCESS_LOG        访问日志开关，默认 true（0/false/no/off 关闭）
#   CHATGPT2API_LIMIT_CONCURRENCY 并发连接上限，默认 256，0 表示不限制
# 这里刻意读 PORT 而不是 CHATGPT2API_PORT：后者在 .env.example 里被当作"宿主机
# 访问端口 8000"，容器内若跟着改会和 compose 的 8000:80 端口映射打架。
# --limit-concurrency: cap simultaneous connections so slow / oversized clients
# cannot exhaust the event loop or the image thread pool (see also
# RequestBodyLimitMiddleware and max_request_body_mb in config.json).
# 用 printf 生成（不用 Dockerfile heredoc：heredoc 需要 BuildKit/dockerfile>=1.4，
# 经典 builder 下会直接语法报错）。
RUN printf '%s\n' \
    '#!/bin/sh' \
    'set -eu' \
    'LIMIT="${CHATGPT2API_LIMIT_CONCURRENCY:-256}"' \
    'ACCESS_LOG="${CHATGPT2API_ACCESS_LOG:-true}"' \
    'set -- uvicorn main:app --host 0.0.0.0 --port "${PORT:-80}"' \
    'case "$(printf %s "$ACCESS_LOG" | tr "[:upper:]" "[:lower:]")" in' \
    '  0|false|no|off) set -- "$@" --no-access-log ;;' \
    '  *) set -- "$@" --access-log ;;' \
    'esac' \
    'case "$LIMIT" in' \
    '  ""|0|*[!0-9]*) ;;' \
    '  *) set -- "$@" --limit-concurrency "$LIMIT" ;;' \
    'esac' \
    'exec uv run "$@"' \
    > /app/docker-entrypoint.sh \
    && chmod 0755 /app/docker-entrypoint.sh

EXPOSE 80

# /health 不需要鉴权，且号池为空时只是 status=degraded 而非 5xx，适合做存活探针。
# 用 stdlib urllib，免得为了 curl 再往运行镜像里塞一个包。
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/health?format=json' % (os.environ.get('PORT') or '80'), timeout=5).status == 200 else 1)"]

CMD ["/app/docker-entrypoint.sh"]
