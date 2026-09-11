# Changelog

## 1.8.5 - 2026-09-11

### chatgpt2api-grok（本分支）

+ [发布] 版本 1.8.5：按现网前端 bundle 逆向校正生图链路（PoW FNV-1a、prepare 状态、finalize 键名），客户端环境画像随出口地理动态化，自动补号改目标水位+分批摊开，修复 `skipped_mainline` 泄漏，管理界面手机端适配。
+ [修正] 对照现网前端 bundle（`8b34dbc2` chunk 的 `bZt` 构造器）校正生图链路 `client_prepare_state`：prepare 请求带的是 prepare 前的状态（首轮 `none`），conversation 在 prepare 成功后带 `success` + `x-conduit-token`。此前两处写反（prepare 发 `success`、conversation 发 `sent`）。
+ [逆向] 对照现网前端 bundle（`4813494d` chunk 的 sentinel 实现与 `8b34dbc2` 的 `bZt` 构造器）全面校正生图链路：
  - PoW 校验哈希由 sha3_512 改为现网实际的 **FNV-1a-32**（`tFt`：2166136261 种子 + 终态混合，8 位 hex），解出的 proof token 补 `~S` 同步求解后缀；config[9] 由 `i>>1` 改为真实求解耗时 ms。均与注册引擎 `_SentinelTokenGenerator` 及现网 `_runCheck` 语义一致。
  - `chat-requirements/finalize` 请求体键名对齐现网：`proofofwork` / `turnstile`（原 `proof_token` / `turnstile_token`）。
  - 会话 payload 时区由硬编码 `Asia/Shanghai`(-480) 改为与出口地理一致的 `Asia/Tokyo`(-540)（`OAI_CLIENT_TIMEZONE` / `OAI_CLIENT_TIMEZONE_OFFSET_MIN` 环境变量可覆盖）；PoW config 的日期串同步改为 GMT+0900。此前 payload 写上海、PoW 日期写美东、出口在东京，三处时区互不一致。
  - 实测确认现网前端虽把 finalized requirements token 缓存约 9 分钟（`$It=1e3*60*9`），但**生图链路复用同一 requirements+proof 会被 conversation 端点 403**（A/B：新号首发全过，复用首发连拒；旧代码逐次 fresh 无此现象），故不做 requirements 缓存，保持逐次 prepare+finalize。
+ [说明] 本轮逆向基于登录态 app shell 的官方 JS bundle 静态分析 + 新注册 free 号实测交叉验证；浏览器抓包因代理网络持续抖动未能完成，相关结论均以 bundle 源码与实测为准。
+ [新增] 自动补号改为「目标水位 + 分批摊开」：新增 `auto_replenish_target_available`（默认 4）与 `auto_replenish_spacing_secs`（默认 600）。可用号低于 target 就小步补（每次 `batch` 个），不再等跌破 min 才抢救——同时死 2 个号时仍保有 min 缓冲；两次成功补号间隔 spacing 秒，各号注册时间错开，死亡时间跟着错开，避免同一时刻集体被吊销导致「全死完没号可用」；跌破 min_available 的紧急情况无视 spacing 立即补。设置页同步加「目标可用账号」「补号间隔」两项。
+ [修复] 注册网络错误分类器补回被 1.8.1 收窄时丢掉的瞬时错误标记（`开始 oauth 流程失败` / `invalid_state` / `oai_did_missing` / `cloudflare` / `csrf token` 等 1c327b4 设计内标记）。代理抖动期这些错误被当成业务硬失败，会拖累自动补号的恢复节奏。顺带校正两处过时测试（count 钳制上限 50→128 为现设计）。
+ [修复] 生图回合上游会下发一个隐藏编排 cell（assistant 消息 `content_type:"code"`，`text` 为 `{"skipped_mainline":true}`），被 code 回退取值路径当成正文透出到 `/v1/chat/completions` 的 delta 内容里。现已在 `assistant_message_text` 过滤该标记，真实上游流回放验证零泄漏（`test_conversation_markers`）。
+ [调整] 客户端环境画像全部改为随出口环境动态解析（新模块 `utils/egress_locale.py`）：会话 payload 时区、PoW config 的日期串（`""+new Date` 格式，含 GMT 偏移与时区显示名）、`navigator.language/languages` 均按出口 IP 地理（ip-api.com，经账号代理探测，按出口缓存 6h）解析；`OAI_CLIENT_TIMEZONE` / `OAI_CLIENT_COUNTRY` 可强制覆盖，探测失败回退东京默认。PoW config 的屏幕分辨率、`hardware_concurrency`、`sid` 改为账号级稳定值（屏幕/核数落在账号 `fp` 并随 `_persist_client_identity` 持久化，sid 用账号的 `oai-session-id`），不再每次求解随机漂移；`client_contextual_info` 的屏幕尺寸同步跟随账号 fp。
+ [优化] 管理界面手机端适配（390px 视口逐页验证）：号池/日志页表格在窄屏切换为卡片列表（信息完整、40px 触控目标、操作按钮齐全），统计卡 2 列网格，分页栏窄屏换行修复，图片管理工具栏断字修复，生图工作台上传/参数/结果操作按钮加大到触控尺寸。桌面端布局不变。

## 1.8.4 - 2026-09-11

### chatgpt2api-grok（本分支）

+ [发布] 版本 1.8.4：应对官网生图链路强制 PoW——conversation 403 时刷新 PoW bootstrap 缓存并重解一次。
+ [加固] 官网生图链路 PoW 现已在 conversation 端点强制校验：实测 `chat-requirements/prepare` 返回 `proofofwork.required=true`，不带 proof token finalize 出的 token 调 `/backend-api/f/conversation` 直接 403（空 body）。本项目的 prepare→finalize 流程本就会解 PoW 并带 `OpenAI-Sentinel-Proof-Token`，实测新注册 free 号全流程出图正常。本次补齐容错：conversation 403（proof 被拒）时丢弃 PoW bootstrap 缓存（sdk.js URL / data-build 可能过期），重取 requirements 重新解 PoW 后再试一次（`is_sentinel_proof_rejected` + `_stream_picture_conversation` 单次重试）。

## 1.8.3 - 2026-09-11

### chatgpt2api-grok（本分支）

+ [发布] 版本 1.8.3：生图模型对外改名 `gpt-image-2.5`（老 id 兼容），修复被吊销免费号反复发出导致 `upstream session expired` 的选号缺陷，补号阈值提为 2。
+ [调整] 生图模型对外正名 `gpt-image-2` → **`gpt-image-2.5`**（官网 2026-09-08 起出的就是 Images 2.5 的图）。`/v1/models`、前端画图工作台、各接口缺省 model 全部改用新 id；老 id `gpt-image-2` 继续接受，只是不再对外列出，两者上游请求完全一致（仍发 `gpt-5-3` + `system_hints:["picture_v2"]`）。Codex 链路的 `codex-gpt-image-2` 不改名——它的 `tools[0].model` 是上游显式的画图模型 id。
+ [修复] `chat_requirements_prepare` 401 后补一次 `/backend-api/me` 确认，`/me` 也 401 才 `remove_invalid_token`（新增 `account_service.confirm_token_revoked`）。此前 prepare-401 一律当软失败：被上游吊销的号 `status` 仍是「正常」、`quota` 还有余、JWT 的 `exp` 还有约 10 天，于是 `_can_skip_image_remote_probe` 跳过探活把死号发出去，连试 4 个号后对外报 `upstream session expired, please retry`，且**没有任何号被标记**，下个请求原样重来。网络错误（探活返回 None）仍算未确认，不会误杀好号，1.8.1 防 TLS/device-id 抖动误杀的保证不变。
+ [调整] `auto_replenish_min_available` 默认 1 → 2。免费号被上游吊销得很快（实测约 2 小时），阈值 1 时经常「补一个死一个」，请求侧直接撞上空池。
+ [说明] 免费号 NextAuth session 活着（`/api/auth/session` 200、`expires` 三个月后）但 oauth token 已被服务端吊销时，该接口会一直返回**同一个已死的 JWT** 直到它自然过期，所以 `session_refresh_stale_token_revoked` 无法自愈——只能靠补号。已验证 `/api/auth/session` **不会**轮换 session cookie，重复调用不会自伤。

## 1.8.2 - 2026-09-10

### chatgpt2api-grok（本分支）

+ [发布] 版本 1.8.2：`/v1/images/edits` 支持 `image_url` 远程参考图（保留 SSRF 防护），文档/测试对齐 1.8.1 实况，并补充 ChatGPT Images 2.5 与模型 slug 变更说明。
+ [新增] `/v1/images/edits` 支持 JSON 提交 `image_url` / `images[]: {image_url}` 远程参考图（http/https），自动下载并复用既有的 SSRF 防护：拒绝 `127.0.0.1` / `192.168.x` / `10.x` / `169.254.x` / `[::1]` / 链路本地 / 多播地址，避免被当作内网跳板。下载走 `proxy_settings.get_with_egress_fallback(resource=True)`，50 MB 上限与 `image/*` MIME 校验同本地文件。`file_id` 仍明确拒绝并提示改用 `image_url`。
+ [说明] ChatGPT Images 2.5（2026-09-08 全档位含 free 上线）在本项目**不需要新增模型 id**：官网生图链路的画图档位完全由服务端决定，`gpt-image-2` 出的已经是 2.5 图。free 号已实测可用。
  - 官网链路 payload 里没有画图模型字段：`/backend-api/f/conversation` 只发对话 slug（`model: gpt-5-3`）+ `system_hints: ["picture_v2"]`，画图模型由服务端挑。
  - 2026-09-09 用新注册 free 号实测：`/backend-api/me` 200、`limits_progress.image_gen.remaining=25`、生图成功出图 1448x1086。SSE 里搜不到 `2.5` / `flare` / `sunburst` / `gpt-image-*` 任何画图版本号，图片 metadata 仍是 `dalle` + `generation`（`serialization_title: "DALL-E generation metadata"`）——即官网侧无法选档、也读不到档位。
  - 官方 API 的 `gpt-image-2.5-flare` / `gpt-image-2.5-sunburst` 是 `api.openai.com` 的模型 id，需要组织实名 + Tier 1 付费，和本项目走的官网逆向链路无关；只有 Codex 链路（`tools[0].model`，仅 Plus/Team/Pro）才显式带画图模型 id。
+ [观察] 实测发现我们发的 `model: gpt-5-3` 已不在 `/backend-api/models` 目录里，服务端会把它 `resolved_model_slug` 成 `gpt-5-6`（GPT-5.6 Luna）。目前照旧可用，但 `gpt-5-3` 属于将来可能被拒的过期 slug，值得后续跟进。
+ [文档] 对齐 1.8.1：去掉「条件自动 Codex」残留，补 `settings.pool`、自动补号功能状态与出图排障（`upstream session expired` / `upstream image connection failed` / 追问误判）。
+ [测试] `test_v1_images_edits.py` 在 `assets/` 缺失时回退到 PIL 生成的 512x512 占位图，CI 缺二进制资源时整组测试不再失败。`test_v1_images_edits_json.py` 跟进新行为：JSON `image_url` 现在受支持（仍拦私网/回环），空图片体与多图字段命名同步更新。

## 1.8.1 - 2026-09-08

### chatgpt2api-grok（本分支）

+ [发布] 版本 1.8.1：自动补号、避免入库/巡检二次登录踢 session，并修好取图 OPENSSL 与模型追问误判。
+ [新增] 自动保持 ChatGPT 号池有可用账号：低于最少数量时用 GPT 注册配置补号；已有任务等待，连续失败冷却 10 分钟。设置页可开关。
+ [修复] 注册入库后不再自动 Codex 补 refresh：关「跳过 Codex」时 Codex 常 add_phone 失败，后台再跑 authorize/continue 会在约 5 分钟巡检里把刚生过图的 NextAuth session 踢死。
+ [修复] 巡检不再每 5 分钟打健康 session_only 的 `/me`，也不再对 session_only 走密码重登（都会变成二次登录）。JWT 快过期时仍可用 session cookie 续期；号池手动检测 / 重新登录不受影响。
+ [修复] SSE 已给出 file_id 时，取下载地址撞 OPENSSL/curl 35 不再吞成空 URL，再把模型追问当成 content_policy_violation。连接错误会重试同出口。
+ [修复] curl_cffi 会话 OPENSSL 重试成功后计数不复位，后续取图会立刻失败；重建会话会丢掉 session cookie。
+ [修复] `chat_requirements_prepare` 401 不再直接对外 `image generation failed`，同请求换号；对外改成 session expired。SSE 进度事件不再挡住连接重试。
+ [修复] 模型追问（「你更喜欢…？」）不再标成内容政策违规。
+ [优化] 已有 file_id 时跳过 poll 首轮等待；轮询前 15s 用 2s 间隔；tasks 预检查不再空等 1s；首轮等待默认 6s→4s。

## 1.8.0 - 2026-09-08

### chatgpt2api-grok（本分支）

+ [发布] 版本 1.8.0：对外仅生图；独立 Grok 号池与 GPT Free 注册；`session_only` 可生图；绑定代理的号按账号隔离出口。
+ [文档] 更正 session_only「不参与生图」过时说明；补充账号隔离、`skip_codex` 门禁与镜像标签（`:latest` 仅 `v*` tag）。
+ [清理] 去掉 CPA/sub2api 前端残骸、备份项、引擎 `upload_cpa`、本地 `MERGE_REPORT.md`；接口文档与调试 Skill 改为生图。
+ [修复] 注册设置只保留 protocol + Cloudflare D1；Grok 模型禁用图生图；补齐并行生图/轮询间隔/先确认再取图开关。
+ [修复] tiktoken 拉 `o200k_base` 不再走进程 SOCKS 代理；下载失败时回退估算，避免生图 usage 统计把整次请求打挂。
+ [修复] Grok / D1 / WebDAV / R2 / FlareSolverr / 号池 HTTP 推送不再继承环境 SOCKS；curl_cffi 无代理时显式清空 `proxy`。
+ [修复] `utils/atomic` 写入遇 Docker 单文件 bind mount 的 EBUSY 时改为就地覆盖，设置面板不再因 rename 失败报 500；图片索引/任务文件写入统一走 atomic。
+ [修复] ChatGPT 生图/刷新/登录主链路接上 proxy_runtime 单代理（WARP/privoxy），不再因直连被拒报 `upstream image connection failed`；图片下载走独立资源会话（可配 `resource_proxy_url`）。
+ [修复] estuary / files 下载地址仍要带 Bearer 和 `ChatGPT-Account-Id`，匿名资源会话会 403 `File stream access denied`；这类 URL 改走主会话取图。
+ [优化] 连接类错误（ProxyError/socks 隧道失败/connection refused/TLS/超时）同账号先换出口（runtime/全局/直连），再换健康账号；代理拒连不再空耗同出口重试。
+ [修复] 图片续轮询不再传已失效的 `proxy_url`；按任务账号换出口取图。参考图/CDN 下载与注册后 `fetch_remote_info` 同样走出口回退。
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
+ [修复] 死 SOCKS 超时（curl 28）后 10 分钟内不再让每个账号重复空等；刷新/生图改走下一条出口。session 探活超时从 45s 降到 12s。
+ [修复] `curl_cffi` 在 Linux/WSL2/Docker 上撞系统 OpenSSL 配置会报 `OPENSSL_internal:invalid library`（对外就是代理测试失败 / `upstream image connection failed`）。SOCKS 出站默认 HTTP/1.1；握手再失败时同指纹切 HTTP/1.1。该错误不再把代理拉黑、也不再写成号池废 token。Grok 出站同样走出口回退，残留 grokcli2api-go / G2A 地址改回 `cli-chat-proxy.grok.com`。
+ [修复] 账号检测对齐原项目：面板刷新强制打 `/me` 探活；定时巡检不再跳过正常 session_only；`/me` 成功会清掉废号标记。生图取号在本地额度>0 且状态正常时不再强打 `/me`（死 SOCKS 探活会把界面卡在「确认可用账号」）。
+ [修复] 本地设置页打不开：`web_dist` 仍是旧包，打开设置会请求已删除的 `/api/cpa/pools`；未知 `/api/*` 却回首页 HTML，前端把 `pools` 写成 `undefined` 后整页崩掉。已重编前端，未知 API/auth 路径改为 404。
+ [修复] 生图 `chat_requirements_prepare` 401 `Could not parse your authentication token`：过期 session JWT 在代理超时后仍被直接选用。过期号先走 session 续期（出口回退），401 会换号，不再把过期 Bearer 交给 ChatGPT。
+ [修复] SOCKS 上 `OPENSSL_internal:invalid library` 不再切直连（本机直连 chatgpt.com 会 30s 超时）。同代理重建 HTTP/1.1 会话重试；该错误仍不把代理拉黑。
+ [修复] 免费 session_only 号第一次生图成功、第二次立刻废号：TLS 重试会新建 `OpenAIBackendAPI` 并换 `OAI-Device-Id`，session 探活 `/me` 又不带 cookie，一次 `chat_requirements_prepare` 401 就把额度清零。设备指纹写入号池并复用；`/me` 带上 session cookie + `oai-did`；prepare 401 不再当 hard revoke、不再清零剩余额度。注册入库带上 `oai-did`。
+ [修复] `skip_codex` 入库后再后台 Codex 补 refresh：会对同一邮箱二次 OAuth/`authorize/continue`，把刚用来生图的 NextAuth session 踢掉。默认跳过 Codex 时不再自动补齐；OTP 回退也不再拿空 `session` 打认证。
+ [优化] 账号隔离对齐 CPA / [codex2api](https://github.com/james-6-23/codex2api)：绑定了 `proxy` 的号不再回落到 runtime/全局/直连；注册浏览器指纹写入号池；刷新锁按号拆开；密码重登复用 `oai-device-id`；Cloudflare clearance 按号缓存。
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
+ [新增] `auto_codex_upgrade`：仅当关掉「跳过 Codex」时，入库后后台 Codex 补 refresh；`skip_codex` 默认开启时不二次登录（会踢掉生图 session）。`add_phone`/OTP 失败软保留 session 行。
+ [新增] `gpt_free_register/codex_upgrade.py` + `services/codex_upgrade_service.py`：绑定既有邮箱 + CFD1 收 OTP + 写入 refresh/id 并替换旧行。
+ [修复] GPT 注册「跳过 Codex」取消不生效：`GptRegisterSettingsUpdate` 补齐 `skip_codex` / `register_no_delay` / `so_collect_ms`，避免 Pydantic 静默丢字段。
+ [保留] 浏览器 OAuth `oauth/start|finish` + `replace_access_token` 仍可作为备用导入/升级路径。
+ [文档] `docs/gpt-register.md` / `docs/operations.md` / README 更新 Codex 门禁、session_only 生图与账号隔离说明。

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
