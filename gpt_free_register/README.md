# gpt_free_register

chatgpt2api 内置的 **ChatGPT free 纯协议注册机**（从 any-register-engines 裁剪 vendoring）。

- 进程内入口：`from gpt_free_register import register_chatgpt_once`
- 引擎代码：`engines/`（仅 `platforms/chatgpt` + core + mailbox providers）
- 密钥：请写 `data/gpt_register.env` 或环境变量，**不要**提交 `.env`

设置页：**设置 → GPT注册**。详见 `docs/gpt-register.md`。
