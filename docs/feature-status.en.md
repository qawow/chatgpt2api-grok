# 功能状态

本文基于当前仓库当前实现整理，用于帮助用户快速了解哪些功能已经可用、哪些仍在完善、哪些待实现。

| 功能 | 状态 | 说明 |
|:----------------------------------------|:--:|:--------------------------------------------------------------|
| OpenAI 兼容 `POST /v1/images/generations` | ✅  | 已支持，用于图片生成，并可通过 `n` 返回多张图片。 |
| OpenAI 兼容 `POST /v1/images/edits` | ✅  | 已支持，可上传图片进行编辑。 |
| 面向图片工作流的 `POST /v1/chat/completions` | ✅  | 仅生图；纯文本返回 400。 |
| 面向图片工作流的 `POST /v1/responses` | ✅  | 仅 `image_generation` 工具；纯文本返回 400。 |
| `GET /v1/models` 接口 | ✅  | 仅生图：`gpt-image-2.5`、`codex-gpt-image-2`（及 plus/team/pro 前缀）、本地 Grok 池非空时 `grok-2-image` / `grok-imagine-image`。`grok-4.5` 是对话模型，不列出。 |
| 画图档位追随官网（Images 2.5） | ✅  | `gpt-image-2.5` 出的已是 [ChatGPT Images 2.5](https://openai.com/index/introducing-chatgpt-images-2-5/)（2026-09-08 全档位含 free 上线）。官网链路画图模型由服务端决定，payload 只发对话 slug + `system_hints:["picture_v2"]`，客户端无法选档。官方 API 的 `gpt-image-2.5-flare` / `gpt-image-2.5-sunburst` 是 `api.openai.com` 模型，与本项目逆向链路无关；仅 Codex 链路（Plus/Team/Pro）显式带 `tools[0].model`。 |
| 同时生成多张图片 | ✅  | 已支持，后端与前端都可进行多图生成。 |
| 图片并行生成 | ✅  | 多张图片使用独立线程和账号同时生成，设置页可关闭 `image_parallel_generation`。 |
| 图片生成进度追踪 | ✅  | 任务显示当前步骤（上传/预热/获取token/生成中等），支持耗时统计。 |
| 图片超时续轮询 | ✅  | 超时任务可继续等待，前端显示"继续等待"按钮，后端 resume-poll API。 |
| 图片二次确认与先check再hit | ✅  | 可通过 `image_settle_enabled` 和 `image_check_before_hit_enabled` 配置，关闭后跳过等待直接返回。 |
| 前端图片工作台 | ✅  | 已支持图片生成、图片编辑、模型选择、历史记录与查看大图。 |
| 前端图片懒加载与滚动优化 | ✅  | LazyImage 懒加载、会话切换滚动位置保存与恢复、bfcache 页面恢复同步。 |
| 前端图片输入 / 参考图交互 | ✅  | 已支持参考图上传、预览、移除和编辑模式工作流。 |
| Codex 画图接口逆向 | ✅  | 已支持，仅 `Plus` / `Team` / `Pro` 订阅可用，模型别名为 `codex-gpt-image-2`；如有需要可自行在其他场景映射回 `gpt-image-2.5`。这是 Codex 逆向链路，用于和官网画图区分，同一账号通常会同时支持官网和 Codex 两份生图额度。 |
| Cherry Studio 接入 | ✅  | 已支持作为绘图接口接入 Cherry Studio。 |
| New API 接入 | ✅  | 已支持接入 New API。 |
| 账号池管理 | ✅  | 已支持列表、筛选、批量操作、导出、手动编辑、刷新和删除。 |
| 账号刷新异步进度追踪 | ✅  | 刷新和重新登录改为异步模式，前端轮询显示进度。 |
| 密码重新登录恢复异常账号 | ✅  | 号池页「重新登录」会走密码重登。健康 `session_only` 的定时巡检不再 `/me`、也不再自动密码重登（二次登录会踢 session）。 |
| 账号额度刷新与恢复时间同步 | ✅  | 已支持账号信息刷新，限流账号也会自动继续检查。 |
| GPT 号池自动补号 | ✅  | 可生图账号低于阈值时用当前注册配置自动开任务；设置页可开关。已有任务等待，连续入库 0 冷却 10 分钟。 |
| 失效 Token 自动清理 | ✅  | 有 `refresh_token` 的号自动移除失效 Token；`session_only` 只标异常、保留剩余额度，不自动删。 |
| CPA / sub2api 导入 | ❌  | 已移除；号池走本地 JSON / access_token / GPT Free 注册。 |
| Docker 自托管部署 | ✅  | 已支持 Docker Compose 部署，并提供多架构镜像。 |
| 兼容接口中的多参考图能力 | ✅  | 已实现，支持在兼容接口中传入多参考图。 |
| 更高级的 Token 调度策略 | ✅ | 生图选号：最少在途，其次最高额度。 |
| Render / Vercel 等部署表述 | ⚠️ | 当前主要以 Docker 部署为主，其他平台部署方式暂未重点说明。 |
| `/v1/complete` 文本补全与流式输出 | ❌  | 文本模型已关闭。 |
| 流式输出支持 | ✅  | 生图兼容接口支持。 |
| 文本补全缓存与重复请求合并 | ❌  | 文本链路已关闭。 |
| Anthropic 协议支持 | ❌  | 已移除实现，`/v1/messages` 返回 400。 |
| PPT / PSD 可编辑文件 | ❌  | 已移除。 |
| 图片尺寸参数 | ⚠️ | 网页会把 `WxH` 写入提示词；Codex 工具会带 `size`。不是严格按像素出图。 |
| 服务端图片 URL 缓存 | ✅  | 已实现。 |
| `rt_token` 刷新 | ❌  | 待实现。 |
| 代理配置功能 | ✅  | 网页端配置全局 HTTP / HTTPS / SOCKS5 / SOCKS5H；账号绑定 `proxy` 后不再回落 runtime / 全局 / 直连。 |
| session_only 免费号生图 | ✅  | 无 `refresh_token` 也可生图；复用 `oai-device-id` / session cookie；`chat_requirements_prepare` 401 不 hard revoke，同请求换号，对外 `upstream session expired`。 |
| 账号出口隔离 | ✅  | 绑定代理的号只走该出口；刷新锁与 Cloudflare clearance 按号；注册指纹写入号池。一号一 IP 需注册代理池。 |
| 生图取图连接重试 | ✅  | SSE 已有 `file_id` 时 OPENSSL/curl 35 不再吞成空 URL；同出口重试，不把模型追问当成内容政策违规。 |
