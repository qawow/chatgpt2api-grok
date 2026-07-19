# gpt_free_register

chatgpt2api 内置的 **ChatGPT free 纯协议注册机**（从 any-register-engines 裁剪 vendoring，含 Cloudflare D1 自建邮箱支持）。

## 入口

```python
from gpt_free_register import register_chatgpt_once

result = register_chatgpt_once(
    settings={
        "mail_provider": "cloudflare_d1_api",
        "executor": "protocol",
        # "proxy": "socks5h://127.0.0.1:40000",
    },
    log=print,
)
```

- 引擎代码：`engines/`（仅 `platforms/chatgpt` + core + mailbox providers）
- 密钥：`data/gpt_register.env` 或环境变量（**不要**提交 `.env`）
- Web：设置 → **GPT注册**
- HTTP：`/api/gpt-register/*`

## 完整文档

- 调用 / API / 运维 / 排障：**[docs/gpt-register.md](../docs/gpt-register.md)**
- 总运维手册：**[docs/operations.md](../docs/operations.md)**

## 注意

- 默认 `inprocess`，成功账号写入 **ChatGPT 号池**，不进 Grok。
- Docker 镜像必须 `COPY` 本目录；勿用空 volume 覆盖。
- SOCKS 代理需要 `PySocks`。
