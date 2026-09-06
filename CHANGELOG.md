# Changelog

## Unreleased

### chatgpt2api-grok（本分支）

+ [清理] 去掉 CPA/sub2api 前端残骸、备份项、引擎 `upload_cpa`、本地 `MERGE_REPORT.md`；接口文档与调试 Skill 改为生图。
+ [修复] 注册设置只保留 protocol + Cloudflare D1；Grok 模型禁用图生图；补齐并行生图/轮询间隔/先确认再取图开关。
+ [修复] tiktoken 拉 `o200k_base` 不再走进程 SOCKS 代理；下载失败时回退估算，避免生图 usage 统计把整次请求打挂。
+ [修复] Grok / D1 / WebDAV / R2 / FlareSolverr / 号池 HTTP 推送不再继承环境 SOCKS；curl_cffi 无代理时显式清空 `proxy`。
+ [移除] PPT/PSD 可编辑文件任务、搜索接口、Anthropic messages 实现、文本补全缓存；调试页仅保留 Skills。
+ [移除] 注册引擎非 ChatGPT / 非 Cloudflare D1 的邮箱、验证码、SMS、Playwright 执行器。
+ [移除] `openai_backend_api` 内 PPT/PSD/搜索实现；浏览器注册 `browser_register.py`（protocol 路径保留）。
+ [移除] CPA 远程号池与 sub2api 导入（设置页 Tab、管理 API、引擎 upload_cpa）；git 存储后端保留。
+ [调整] Grok 型号拆开：`grok-4.5` 是对话模型（[Grok 4.5](https://x.ai/news/grok-4-5)），不进生图目录；对外生图 id 为 `grok-imagine-image` / `grok-2-image`。免费 Build 仍内部用 grok-4.5 **chat agent** + `image_generation` 工具。
+ [调整] 对外只暴露生图模型：`/v1/models` 不再列出 gpt-5* / auto / grok-4.5；`/v1/chat/completions`、`/v1/responses` 纯文本、`/v1/messages`、`/v1/grok/chat/completions` 返回 400。
+ [优化] `_bootstrap` PoW 脚本缓存 12 分钟，同进程第二次生图不再 GET chatgpt.com 首页。
+ [优化] 生图选号改为最少在途、其次最高额度（不再纯 round-robin）。
+ [优化] 生图首轮等待默认 10s→6s；Grok 免费路径只打 grok-4.5，不再试 grok-4/grok-3。
+ [优化] `GET /v1/models` 改为本地生图目录，不再每次 TLS 打 chatgpt.com。
+ [优化] 生图取号：JWT 剩余 >5 分钟且本地额度/状态正常时跳过 `fetch_remote_info`（少一轮 /me+init+accounts）。
+ [优化] Grok 上游：线程内 `requests.Session` keep-alive；免费路径 429 立即失败不再连打 grok-4/grok-3/付费接口；付费 401/403/429 跳过 `/models` catalog。
+ [优化] 生图轮询：15–35s 窗口用 4s/7s 间隔（不超过配置上限）；循环内不再每次打 `/backend-api/tasks`（只在接近超时补一次）。默认 `image_poll_interval_secs` 5。
+ [移除] grokcli2api-go / G2A 桥：删除 `/api/g2a*`、设置页 Codex2API、号池远程只读标签；Grok 生图只走本地号池。
+ [优化] 生图 TLS/超时粘号短重试（不放槽、不记 fail、不换号）；入库号默认 chrome142 指纹；选号 `inflight < quota` 防超卖。
+ [调整] GPT 注册默认改回稳优先：`concurrency=1`、`interval_secs=3`、保留步骤抖动；auto-OTP 等 75s 再重发、最多 2 次（避免作废路上的码）。
+ [优化] GPT 注册按 2026-09-06 现网抓包收紧：auto-OTP 落到 `/email-verification` 后跳过 authorize_continue Sentinel；`create_account` SO collect 默认 0ms；`OPENAI_PREFER_PASSWORD_SIGNUP` 默认关闭（`user/register` 现网 400）。
+ [优化] GPT 注册对照 gpt-free-register：`account_deactivated` 不再 OTP 补发、Subject 唯一 6 位优先抽码、日韩验证码关键词、Sentinel `sid=oai-did`、PoW 失败走官方 unsolved 前缀、并发钳到代理池大小、GET 重试含 curl 7 / connection refused。
+ [优化] GPT 注册对照 [xiaoguzuiniu/gpt-free-register](https://github.com/xiaoguzuiniu/gpt-free-register) / [hyhang915/gptfree-register](https://github.com/hyhang915/gptfree-register) / [klsf/codex-register](https://github.com/klsf/codex-register)：GET 对 curl 52/56 同 session 重试、authorize 跟随网络重试、OTP `continueUrl` 即 session callback 时跳过 create_account、无 `code=` 时跟随 workspace/redirect。
+ [优化] GPT 注册再对照 [gpt-auto-register](https://github.com/Regert888/gpt-auto-register)：`oai-did` warmup 失败不建邮箱、document 导航头 / auth XHR 补 Origin+did+Datadog、auto-OTP 只 resend、OTP 401 补发新码、代理池 round-robin、熔断识别 `invalid_state`/CF 403、并发不再写全局 `REGISTER_PROXY`。
+ [优化] GPT 注册对照 [gpt-auto-register](https://github.com/Regert888/gpt-auto-register) 补齐稳定性：`socks5://` 规范化为 `socks5h://`、TLS 握手瞬断原 session 重试、passwordless/send-otp + email-otp/resend 发码顺序、session_token cookie/JSON 三路兜底、过滤 tm1 影子 OTP `493682`、批量任务连续网络错误熔断。
+ [调整] session_only **补 refresh 主路径**改为协议 **Codex OTP 补齐**（`POST /api/accounts/codex-upgrade` + 号池「Codex 补 refresh」），不再依赖浏览器粘贴 callback。
+ [新增] 注册入库 `session_only` 后默认后台 **自动 Codex 补 refresh**（`auto_codex_upgrade=true`）；`add_phone`/OTP 失败软保留 session 行。
+ [新增] `gpt_free_register/codex_upgrade.py` + `services/codex_upgrade_service.py`：绑定既有邮箱 + CFD1 收 OTP + 写入 refresh/id 并替换旧行。
+ [修复] GPT 注册「跳过 Codex」取消不生效：`GptRegisterSettingsUpdate` 补齐 `skip_codex` / `register_no_delay` / `so_collect_ms`，避免 Pydantic 静默丢字段。
+ [保留] 浏览器 OAuth `oauth/start|finish` + `replace_access_token` 仍可作为备用导入/升级路径。
+ [文档] `docs/gpt-register.md` / `docs/operations.md` / README 更新 Codex 自动升级与号池入口说明。

## 1.7.0 - 2026-07-05

+ [移除] 移除注册功能、防滥用机制导致封禁GitHub账号。

## 1.6.0 - 2026-07-04

+ [修复] 修复sub2api导入问题。
+ [修复] 修复前端404、405问题。
+ [新增] 新增出图后删除对话记录功能。
+ [调整] Pro号不再按无限额度处理、约每天1000张。

## 1.5.0 - 2026-06-13

+ [新增] 新增 WARP / Privoxy / FlareSolverr 清障方案，注册遇到 Cloudflare 拦截后可刷新 clearance 并重试。
+ [新增] 新增 `outlook_token` 邮箱池，支持 Outlook/Hotmail 注册验证码读取。
+ [新增] 新增网页搜索兼容接口、图片编辑 mask 和图片任务相关能力。
+ [优化] 更新 sentinel/PoW 获取方式，提高上游请求兼容性。
+ [优化] 调整代理优先级和注册请求重试逻辑。

## 1.4.1 - 2026-06-03

+ [新增] 账号刷新改为异步模式，支持前端轮询刷新/重新登录进度。
+ [新增] 号池管理页面新增重新登录功能，支持密码登录恢复异常账号。
+ [新增] 刷新后自动重新登录异常账号（可在设置页开启）。
+ [新增] 图片生成支持并行模式，多张图片使用独立线程和账号同时生成。
+ [新增] 图片轮询超时自动换账号重试（最多4次），连接超时同账号递增等待重试。
+ [新增] 图片二次确认机制与先check再hit可配置化，关闭后可跳过等待直接返回结果。
+ [新增] 图片任务进度追踪，显示当前生成步骤（上传/预热/获取token/生成中等）。
+ [新增] 图片超时后续轮询功能，前端显示"继续等待"按钮。
+ [新增] 设置页新增图片二次确认、超时等待时间、自动重新登录等配置项。
+ [优化] 优化生图页面滚动加载性能，图片懒加载、会话切换滚动位置保存与恢复。

## 1.4.0 - 2026-05-31

+ [新增] 新增AI生成可编辑PSD文件逆向。
+ [新增] 新增AI生成可编辑PPT文件逆向。

## 1.3.1 - 2026-05-30

+ [新增] 新增ChatGPT搜索调试、Skills。

## 1.3.0 - 2026-05-30

+ [新增] 新增ChatGPT搜索接口逆向。

## 1.2.4 - 2026-05-30

+ [新增] 添加聊天补全缓存与重复请求合并。
+ [新增] 新增无限画布一键跳转功能

## 1.2.3 - 2026-05-29

+ [新增] 新增账号级代理。
+ [修复] 修复503异常信息、前端邮箱换行问题。

## 1.2.2 - 2026-05-29

+ [新增] 新增Codex链路生图、支持2k,4k。
+ [新增] 支持RT刷新账号信息。

## 1.2.0 - 2026-05-28

+ [新增] 当前版本基线，包含 Web 面板、画图、号池管理、注册机、图片管理、日志管理和设置能力。
+ [新增] 前端版本号支持点击查看版本更新弹窗，展示当前版本、最新版本和更新日志。
+ [优化] 优化注册机效率，成功率大幅提高。
+ [优化] 优化生图页面配置选项。
