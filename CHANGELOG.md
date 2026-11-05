# Changelog

## 1.8.15 - 2026-11-06

+ [新增] 公开图片模型 ID `gpt-image-2.6`，与 `gpt-image-2.5` 并行对外列出。两者上游 slug 同为 `auto` + `system_hints: ["picture_v2"]`，新增 ID **只改变对外名称与目录项，不改变请求内容**，也不构成上游已启用 2.6 的证明。前端 `web/src/lib/models.ts` 同步新增 `WEB_IMAGE_MODEL_26`。旧 `gpt-image-2` 仍作别名接受但不对外列出；**2.5 予以保留**（无证据表明上游已淘汰它，删除会打断在用客户端）。
+ [修复] **网页图片传输对齐抓取到的 v1 协议**（全部离线复现，未用真实账号生图）：
  - 完整 SSE 帧解析：保留 `event`／`id`、多行 `data` 以 `\n` 连接、BOM／CRLF、严格 UTF-8，未终止的 EOF 帧按协议丢弃；对外保留旧字符串适配接口。
  - 按网页 decoder 实现通道状态机：短字段 `c`／`p`／`o` 继承全局 prior、`v` 从不继承、各通道独立根、`~1`→`~0` 指针解码、`add`／`remove`／`replace`／`append`／`truncate`／nested patch，失败时深拷贝原子回滚；拒绝 `__proto__`／`constructor`／`prototype` 不安全指针。
  - 收到新的 `delta_encoding: "v1"` 声明即重建 decoder 与基线；仅在没有新声明时才跨 resume 保留状态。
  - 最多一次 `/backend-api/f/conversation/resume`：JSON body **仅** `conversation_id` 与 `offset`，最新流 token 以原值放 `X-Conduit-Token`；`offset` 计已确认的完整 non-response 事件（含 encoding／token 控制帧），**不是**字节、行数或 SSE id；初始与恢复共享同一个 monotonic 时间预算。
  - 恢复候选使用统一开始时间、时间窗、排除集与原子 claim／checkpoint，不再盲猜「最新」或并列会话；关键词匹配仍只是启发式，不等于所有权证明或 exactly-once。
  - 已拿到可信会话／输出 ID 后，断线、下载失败或后期 poll 超时不再改成换账号重提；只有输出 ID 而缺 conversation ID 时同样禁止自动重新生成；pending 错误不再谎称已有 CID。恢复到的 CID 会同步回后端标记。
  - 输入参考图的 `input_message` 不再计入生成输出；取消、协议、终止性 HTTP、配额／策略与回调错误不伪装成传输恢复成功。`ToolCooldownError`／`ToolQuotaError` 在已有输出时不改判重试。
  - resume 的 HTTP 4xx（除 `408`／`409`／`425`）直接抛出并保留原始诊断，不再被笼统重试覆盖。
  - UTF-16 截断可能产生孤立代理项，最终 OpenAI／Anthropic SSE JSON 边界一律 `ensure_ascii=True` 转义。
+ [修复] 图片按 **PNG／JPEG／WebP／GIF** 真实格式嗅探：原字节保存、正确后缀与 WebDAV／HTTP `Content-Type`，保留 GIF 帧／duration／loop，兼容旧 PNG URL 内的其它格式，不迁移索引、不新增结果 JSON 字段。
+ [修复] **CI 会稳定失败导致镜像不构建**：[test/test_egress_locale.py](test/test_egress_locale.py) 硬断言洛杉矶九月 PDT `-07`，但 `_offset_parts` 取当前时间，冬令时返回 `-08`，于是 `test` job 失败、`docker` job 被跳过。改为季节无关断言（接受 `-0700`／`-0800`）。
+ [文档] 新增 [docs/chatgpt-images-2.6-reverse.md](docs/chatgpt-images-2.6-reverse.md)：记录 `POST /image-gen/gif/select_speed` 端点（`{conversation_id, message_id, asset_pointer, selected_speed}`，前端对 404／405 静默）及 GIF 判据（`mime_type == "image/gif"`、`metadata.generation.animation`、`gif_speed_variants`）。明确该端点语义是**生成后切换速度**而非生成 GIF 的参数。
+ [已知未验证] **GIF 是否可生成仍未验证**。2026-11-06 尝试真实生成：本地服务正常启动、请求进入上游，但 5 个注册免费账号 `access_token` 全部过期且刷新失败（`curl: (77)` 证书验证错误），接口返回 429，**未产出 GIF、未消费配额**。不要引用本次作为 GIF 或 2.6 可用性的证据。
+ [测试] 新增 `test/test_web_image_transport.py`（SSE 帧、v1 decoder、resume 契约、UTF-16 转义）与 `utils/image_media.py`；扩充 SSE 超时、图片存储与恢复回归用例。
+ [验证] 隔离 Python 3.14.4 + pytest 9.2.0、UTF-8 模式，全程离线（外部 socket 与 curl_cffi 传输被测试入口禁用，原生 HTTP 仅放行已核对的字面回环夹具，真实服务 v1 模块在 collection 前排除）。语法门禁通过；模型命名／路由专项 **122 passed，3 skipped**；扩展离线 suite **1380 passed，5 failed，274 subtests passed**。剩余 5 项均为 Windows 专有限制，在 Ubuntu CI 不适用：3 项 POSIX `0o600` 权限位断言、1 项临时 SQLite 清理 `WinError 32`、1 项替换打开文件 `WinError 5`。**未把整套 suite 记为通过**，未用伪造时间／权限或忽略断言掩盖。全程未刷新／注册真实账号、未消费生图配额、未调用真实 WebDAV。

## 1.8.14 - 2026-10-23

+ [新增] Grok 网页协议完整逆向（`docs/grok-web-protocol.md` 3600+ 行）。从 1367 个 Turbopack chunk（60 MB）静态提取 **250 个 `/rest/` 端点**并全量探测（403×178 / 400×11 / 200×8 / 404×5 / 401×4）。打通：网页生图（`build` 模型，图片在 `cardAttachment.image_chunk.imageUrl`）、直连模型 `imagine-image-gen`（比 `build` 快一倍）、**图生图** `mediaGenInput.imageToImage`（实测产出 768×1152 JPEG）、视频生成（`imagine-video-gen` 出 id）、语音合成（`text-to-speech`，产物 24kHz 单声道 128kbps MP3）、代码执行（`run-code`，CPython 3.12.3 / Node 24 / gVisor 沙箱 / uid 0 / DNS 通 TCP 拒）、模板库（20 个全 `creditCost: 0`）、模型目录（`POST /rest/models` 33 个模型）、提示词增强（`imagine-video-prompt-enhance`，**不耗媒体配额**）。
+ [新增] 文件上传 v2 三段式（`upload-file-v2/init` → `PUT <预签名 GCS URL>` → `complete`）。**v1 `upload-file` 已不可靠** —— 同一张图同一账号 00:11 返回 200、05:20 起稳定 400 `Cannot open given image [WKE=file:parse-failed]`，与格式/尺寸/大小无关。PUT 必须原样带上 `singlePut.requiredHeaders`（签名头为 `content-type;host;x-amz-meta-upload-id`），漏掉会被 GCS 拒为 `MalformedSecurityHeader`。
+ [新增] 媒体配额端点 `POST /rest/media/imagine/quota_info`（**必须用 SSO cookie**，Bearer 一律 403）。实测 `imagePro: {remainingQueries, windowSizeSeconds: 86400}` —— **窗口是 24 小时**。桶划分 `image` / `imagePro` / `imageEdit` / `video` / `video720p`。用每次换 IP 的轮换住宅代理做对照实验证明：**配额按账号计量，与出口 IP 无关**（10 个不同 IP 连发 10 次 → 0 张图）。
+ [新增] 生图失败的**五种**形态与自动识别：① 请求层 429；② 流内 `renderToolRateLimited` + `systemErrCode 1006`；③ **请求体不完整**（缺 `disableSearch` / `isReasoning` / `modelMode` 或 `x-xai-request-id` 时返回 200 + 无图 + 正文声称"已生成"）；④ **agent 幻觉**（`imagine-agent-mode` 配额耗尽时用自然语言描述一张并不存在的图，170 帧流里 `cardAttachment` 出现 0 次）；⑤ 模型报错。新增 `claims_success_without_image()` / `model_broken()` / `quota_exhausted_hint()`。
+ [新增] 完整错误分类法（bundle 模块 962117 / 4150825）：**20 种 error kind** 按固定优先级判定，只有 `beyondContextLength` 是 fatal。以及前端自己的空响应判据 `B(e)`（`!text.trim() && 卡片 0 && !error && 无 usageLimitKind && 无 concurrency/rate limited`）。配额耗尽区分 `usage_pool_exhausted`（共享池）与 `usage_limit_reached`（个人上限）。
+ [新增] 认证协议逆向：登录 body 为 `{"credentials":{"emailAndPassword":{"email","password"}}}`（40+ 次爆破得出；服务端对未知字段静默忽略，只能靠错误变化定位）。风控是**两层**：Cloudflare Turnstile（sitekey `0x4AAAAAABSXp6OOe7EVGPnR`，`POST /api/verify-turnstile {"token"}`，400=缺字段 / 403=校验失败）+ **Castle** 第三方反欺诈引擎（令牌 TTL 60 秒，`method ∈ {auth, chat, commerce}`，**发消息也在被打分**）。完整 **66 个认证错误码 / 9 个命名空间**，真正的"拉黑"是 `unauthorized:blocked-user`（`"Your account has been blocked."`），另有 `form-invalid:disposable-email`（一次性邮箱硬封）。
+ [新增] JWT 全量实测（10 个账号）：`{"typ":"at+jwt","alg":"ES256","kid":"oauth2-production-2026-02-19"}`，`aud == client_id == b1a00492-...`，`exp = iat + 21600`，scope 恒为 8 项。**Grok 只有 1 个 client_id**（GPT 有 3 个），bundle 里搜不到任何 client_id —— 网页走 SSO cookie 不走 OAuth。凭证能力边界：只读+生成认 Bearer；文件写入、模板执行、配额、音色列表认 SSO cookie。
+ [修复] **域名轮换改为按根域名计数**（修「注册太多封根域名」）。远端实测所有账号都落在**一个根域名**下 —— `5122331.xyz` 出现 511 次、含 **391 个不同子域**。原代码只有 `random.choice(domain_pool)`：扁平随机、无根域名计数、无冷却、无拒绝记忆。封禁发生在 eTLD+1 层面，**轮换子域完全无效**。新增 `root_domain()`（含 `co.uk`/`com.cn`/`com.au` 两段式后缀）、`DomainRotator`（每根上限 + 整根冷却 + 拒绝记忆 + 可落盘快照）；域名池只有一个根域名时打 warn。
+ [修复] **「信发了但收不到」也反馈到域名轮换**。远端 69 条注册任务报 `获取验证码失败`、`email: null`、`failure_kind: unknown`，耗时 209 秒 —— 服务端 `send_otp` 返回 200（接受发送），但邮件被接收方 MTA **静默丢弃**，无退信无报错。原有代码只有 `login_challenge` 会快速失败，这一支没有任何反馈。新增 `looks_like_silent_drop()` 与 `DomainRotator.mark_undelivered()`：同一根域名**连续**收不到信达阈值则整根冷却，收到一次信即清零。新增可调项 `mail_max_per_root_domain`（默认 20）、`mail_domain_cooldown_minutes`（默认 360）、`mail_silent_drop_limit`（默认 2）。
+ [修复] 风控链路归因（远端 8000 条日志实证）：账号不是"被拉黑"，是**三层递进吊销** —— `session_refresh_stale_token_revoked` 989 次 → `token invalidated (/backend-api/me)` 867 次 → `password_verify_failed_401` 23 次 → `password_reset_required`（真正的锁定）。触发链是 refresh 失效后**自动 fallback 到密码登录**，而密码登录从非注册环境发起会被判可疑。`remote_probe` 是 refresh 的第一大触发源（1147 次、覆盖 928 个账号，单账号最多 112 次），25% 的吊销前 5 分钟内有 TLS/SOCKS 失败。新增 `password_reset_required()` / `account_revoked()` / `should_fallback_to_password()` —— **鉴权类错误永远不该 fallback 到密码登录**。
+ [测试] 新增 `test/test_gpt_register_domains.py`（17 项）与 `test_grok_web_api.py` 大量用例；unittest 全量 942→1278 通过。

## 1.8.13 - 2026-10-16

+ [新增] 协议逆向与对齐（静态逆向 2026-10-16 bundle）：`/f/conversation/prepare` 的真实客户端新增 `X-Web-Mobile-Conversation-Renderer: octane`、`x-web-mobile-prepare-state`、`x-web-mobile-prepare-reason` 三个头；sentinel 改为由 `/backend-api/sentinel/sdk.js`（1474 字节桩）加载 `/sentinel/20260219f9f6/sdk.js`（30864 字节真实现）。头名映射表（`a-BiuthBl1.js`）新增两个本仓库从未发送的头：`OAI-Echo-Logs`（输入事件时序，`focusin`/`focusout`/`copy`/`paste`/`touchstart`/`touchend` 前 10 条的 `type,timestamp` 对）与 `OAI-Telemetry`（SDK 未产出 timing 时回退字面量 `[1,null]`），两个 sentinel 头构造器均已补上。**PoW 算法未变**，真 SDK 的 `check()` 与本仓库实现逐字一致；真 SDK 无 `WebAssembly`/`wasm`/`eval`/`Function(`/`Worker`/`blob`，是字符串数组混淆而非虚拟化保护，算法仍可完整复刻。
+ [改进] 浏览器指纹自洽性：UA、客户端提示、TLS 目标此前来自三个互相独立的来源，实测产出真实浏览器不可能产生的组合（UA 说 `Chrome/142`、`Sec-Ch-Ua` 说 `Microsoft Edge 143`、`Sec-Ch-Ua-Full-Version` 是 Edge 的 `143.0.3650.96`、TLS 是 `chrome142`）。新增 `browser_identity(impersonate)`，从 **TLS 目标**派生唯一的 UA + 客户端提示集合，`_build_fp` 不再硬编码任何浏览器版本；存量 UA 按家族+主版本校验，同家族同版本保留，跨品牌/跨版本才替换；Firefox/Safari 不发客户端提示。
+ [改进] 注册吞吐：单代理不再把并发钳成 1。旧逻辑把并发钳到**代理池大小**，于是单代理部署下 `concurrency` 设置完全失效（批次日志恒为 `size=1`）。实测同一出口 3 并发注册 70.5 秒完成 3/3，串行需 251 秒。改为：`max_per_proxy` 是显式的「每出口并发数」开关（0=不限），钳制按 `pool_size × max_per_proxy`；并把此前定义了但从未被调用的 `_proxy_semaphore` 接进 `_register_once`，让该开关真正生效。修复后服务层 3 并发 132.8 秒（44.3 秒/号，3/3 成功），对比串行 250.9 秒（83.6 秒/号），提速 1.89 倍。
+ [修复] 号池「封得快」的根因：`_validate_access_token_alive` 把 **401 与 403 一并**当作「确认已死」，但 chatgpt.com 对**被挑战或被限流**的客户端同样回 403（`{"detail":"Unusual activity has been detected from your device. ..."}`）。整个号池共用一个出口 IP，批量探测 `/backend-api/me` 必然触发限流，于是健康账号被永久判定为吊销 —— 实测 50 个异常账号中 **19 个从未发出过任何请求**（`last_used_at` 空、`success=0`、`fail=0`）。新增 `_auth_dead_verdict()`：401 → 吊销；403 → 交由既有的 `response_diagnostics` 分类，`http_forbidden` → 吊销，`challenge`/`rate_limit` → 不吊销，其它 → 无判定；诊断自身抛异常时返回无判定（绝不因诊断出错而判死账号）。`_CHALLENGE_MARKERS` 补入 `"unusual activity"`（它以 JSON 体返回，仅凭 content-type 会被误判成普通 403）。`utils/network_diagnostics.py` 的 `error_details` docstring 本就写着 *never infer revocation from a 403*，本次是把该原则接入验证路径。
+ [修复] 注册代理的 sticky 用户名回退：`apply_sticky_session` 对 `socks5h://` 默认启用，把用户名从 `ob` 改成 `ob.<session_id>`，而该池**只接受裸用户名** —— 实测 `ob` 每次连通、`ob.<任意后缀>` 一律 `curl: (97) User was rejected by the SOCKS5 server`。于是 sticky 后缀把可用代理变成死代理，warmup 拿不到 `oai-did`，注册在花邮箱前就报 `初始化会话失败: oai_did_missing`。新增 `strip_sticky_session()` 与 `is_socks5_user_rejected()`（只匹配 `(97) … rejected by the SOCKS5 server`，不匹配 `(35) TLS`，后者是被挑战的出口、应当继续旋转），warmup 捕获该错误后去掉后缀、重建会话并重试；换出口循环同样回退并在回退后停止旋转。进一步改为**通用修复**：`note_sticky_rejected()` 记录被拒网关（`apply_sticky_session` 对其不再加后缀，所有调用点自动受益），`sticky_suffix_accepted()` 冷启动时用真实 SOCKS5 握手探测凭据（按 `user@host:port` 缓存，探测不确定时返回 True，网络抖动绝不触发降级）。
+ [修复] Codex 补 refresh 链路：`codex_upgrade.py` 的 `authorize/continue` 体误用了 `"screen_hint": "signup"`，但该路径针对**已存在**账号（注册流程自己的 Codex 步骤用的是 `"login"`），对已存在地址请求注册会让 auth session 看起来失效并返回 `409 invalid_state`；改为 `"login"` 后同一调用变成 `200 + page_type: email_otp_verification`，OTP 正常到达。同时该路径的 `resolve_proxy()` 也会加 sticky 后缀而死在 `curl: (97)`，已由上述通用修复覆盖。
+ [修复] tempmail 单次读超时不再烧掉整个 OTP 窗口：并发轮询下该服务偶发 `Read timed out (read timeout=15.0)`，而 `_request` 一次失败就抛、直接打断等待循环（实测 3 并发中 1 个因此失败，同账号串行时成功）。改为传输层错误重试 3 次（0.4/0.8 秒退避），4xx 不重试（那是真实应答）。
+ [修复] 邮箱域名池生效：`_create_tempmail` 此前只读单数的 `tempmail_domain`，**完全忽略** `tempmail_domains`（服务层按次注册轮换的池字段），于是 UI 里配的域名池无效，服务端随机挑域名并可能挑到不投递的域。改为单数为空时从池里随机取一个。
+ [改进] 邮箱域名与深度的实测结论（修正一处先前的错误归因）：投递失败是**域名级**问题，与子域深度无关。三组并行对照隔离了变量 —— `5122331.xyz` 在 depth 1 / 10 / 12 / 17 全部成功；`233159.xyz` 在 depth 1 与 12 全部超时；`awa91.cyou` 在 depth 10 连续 3 次超时（确定性）；`cyt233.dpdns.org` 命中 DBL 黑名单。直接查 tempmail 服务端邮箱确认：`awa91.cyou` 的 3 个地址全部 0 封，`5122331.xyz` 成功那次 1 封。两个域名的 DNS 都正常（DoH 查得通配符 MX `10 mail.<domain>.`），失败发生在服务端的域名投递配置。`tempmail_subdomain_depth` 默认值由 12 调整为 10：`_random_label` 每层约 32,112 个标签，depth=10 时单域名子域组合约 10^45，配合 local-part 的 36^12 ≈ 10^18.7，可用地址数达 10^63 量级，地址空间不再是瓶颈。
+ [修复] 会话模型 404 判定：上游 `POST /backend-api/f/conversation` 返回 `404 {"detail":"Model not found"}` 时，不再一律按「账号级终态」把凭据停放 24 小时。匿名链路 `/backend-anon/conversation` 在完全不带凭据时返回同一个 404，说明这是 `-convo` 后端的全局条件而非账号问题；据此加入熔断器：单个凭据命中按局部故障长停，300 秒窗口内出现 2 个及以上不同凭据则判定为全局故障，改走 60 秒短停并立即返回 `upstream_model_not_found_global`；判定为全局时窗口内先前被长停的凭据会回溯改短。仍不摘除凭据（token 是活的，文本能力未受影响）。该 404 已于 2026-10-16 12:26 (+08) 由服务端自行修复，实测通过率从 0/330 恢复到 12/12 与 15/18（~90%）；归因实验证明不是客户端改动 —— 把修掉的三重矛盾指纹原样恢复后**依然 5/6 通过**。
+ [修复] 图片模型 slug：`_image_model_slug` 不再把 web 图片模型映射到 `gpt-5-3`（该 slug 已从 `/backend-api/models` 目录下架），改用 `auto` —— 它既是账号默认值，也是真实客户端抓包里的默认值。Codex 链路仍透传自己的上游 id。
+ [改进] 协议字段对齐：`/f/conversation/prepare` 补上真实客户端必发的 `client_prepare_dispatch`（`immediate`）与 `client_prepare_source`（`composer_editor_state`）；会话默认头补上真实客户端使用的 `oai-did`（此前只发 `oai-device-id`）。
+ [修复] 号池管理：修复 `GET /api/accounts` 在图片任务运行期间抛出 `int() argument must be a string, a bytes-like object or a real number, not 'list'`。图片在途状态已改为租约到期时间列表，列表接口仍按整数读取；现在调用统一计数函数，只统计有效租约，兼容旧整数计数，过期租约显示为零。
+ [修复] 图像输入与数量校验：参考图上限 16 张、单图 50MB、image 与 mask 累计 200MB；内联 Base64 延后解码并在解码前检查引用数量和编码长度；上传文件按可用字节预算读取，拒绝请求时关闭尚未读取的文件；远程图片按流读取并限制响应大小。协议层统一校验 `n`，单元素数组可归一化，含多项、嵌套数组、非整数及越界数量返回明确的 400。
+ [修复] JSON 兼容字段：按 `images`、`image`、`image_url` 优先级选择首个非空来源；空数组、空字符串和 null 可回退，多个非空兼容字段不重复累计。
+ [修复] 代理与出口一致性：环境代理隔离（本地复现 curl_cffi 0.15.0 在 `proxy=""` 或仅 `trust_env=False` 时仍使用环境代理）；绑定账户不再自行创建直连候选；OpenAI 客户端记录会话真正选中的代理，地域探测、身份保存与 clearance 匹配沿用该出口；Grok 客户端沿用 runtime 的 SSL 校验设置，多出口重试共享剩余时间预算。
+ [修复] Cloudflare 清障：统一会话在指定状态码上识别挑战响应，FlareSolverr 按原账户、域名和实际出口刷新凭据，保留登录身份重建连接并最多重试一次；普通鉴权、代理认证、限流及 JSON 错误不触发；并发挑战合并刷新；不可重放的文件流不自动重试。`warm_up_on_start` 接入应用生命周期，在可停止的后台线程预热默认出口及非禁用账户。
+ [修复] 资源与清理：资源下载的连接失败纳入既有出口失败计数，逐个重定向使用剩余时间预算；本地图片清理保留 WebDAV 副本、索引和标签，仅本地图片的标签批量删除与并发标签修改使用同一把锁；清理扫描按结果节流，空目录也会清理。
+ [改进] 配置与并发：`ConfigStore` 的完整读取、更新、配置 getter 和存储后端初始化使用 `RLock`，配置快照及更新参数复制嵌套数据；账户、用户密钥、注册任务、配置、图片及存储的阻塞操作移到线程池。WebDAV 一次性客户端在保存、读取、删除、批量同步和连接测试后统一关闭。
+ [测试] 新增回归：号池租约与并发、配置快照与存储单例、WebDAV 连接释放、清理与保留策略、图像输入边界、代理诊断与出口匹配、清障挑战与并发合并、会话模型 404 分类器与熔断器、浏览器指纹自洽性、sentinel 头名与 `OAI-Echo-Logs` 形态、403 吊销判定三态、sticky 网关记忆与探测、Codex `authorize/continue` 的 `screen_hint`、并发钳制与 `max_per_proxy`、邮箱域名池、tempmail 请求重试（unittest 全量 863→942 通过）。

## 1.8.12 - 2026-10-06

+ [新增] 生图调度与背压全面加固：槽位计数改为带截止时间的租约表（`image_task_timeout_secs` 的两倍，夹在 120–7200 秒，账号轮换时租约随迁），异常路径漏释放的槽位到期自动回收；槽位等待预算 30–120 秒，超时返回 429 `upstream_busy` 并附带 `Retry-After: 30`（可重试），不再是 502。并发选号按到达顺序 FIFO 排队：先到者锁定其当前可用号，后到者避让，防止后来的请求反复抢走前面排队者刚等到的号。
+ [新增] 生图逐号 skip 诊断与出口分摊：`image_pick_skip_diag` 给出空候选时每个号的具体跳过原因（禁用/吊销冷却/门控 park/额度耗尽/计划过滤/来源过滤/在途并发满/在途额度占用八类）；多账号选号按 `proxy_id` 分摊出口负载（最少在途优先、同出口次之），仅作偏好不做硬性约束。
+ [改进] 账号探测共享缓存与文本轮转：同一号 15 秒内共享一次 `/me` 探测、失败 5 秒（慢 SOCKS 下排队选号不再各自打一遍）；文本轮转对软网络错误设 60 秒惩罚窗、软错误不再触发每次请求的强制 token 刷新、刷新成功后错误标记立即落盘，并优先避开正在跑生图的账号（缓解同号文本+生图并发的上游 429）。
+ [改进] 空池即时补号与注册出口分散：账号池打空时压力事件立即唤醒补号 watcher（60 秒节流；节流基线改为 -inf，修复新启动主机 `monotonic` 小于 60 秒时首次唤醒被吞）；单账号补号随机选取代理网关、批量注册按序号轮转但每批随机起点，不再固定集中第一条网关。
+ [修复] 传输层挂死与超时预算：实证确认 curl_cffi 标量超时对流式请求只覆盖建连+响应头——SSE 正文此前完全无读取保护，黑洞出口会永久挂死读取线程。流式调用现设置 libcurl 低速中止（低于 1 字节/秒持续 30–180 秒即断开）；出口回退在全部候选间共享一个超时预算（最坏延迟不随候选数放大）；codex 响应（1200 秒）与图片/文本流 TTFB（300 秒）改为任务剩余预算感知。
+ [修复] 注册流水线：内置注册机（inprocess）此前没有任何时限（子进程模式有 `timeout_secs` 的不对称），单次注册挂在死出口上会楔死任务线程。批次等待现以 `timeout_secs` 为墙钟，超时计入失败并停止后续批次；`data/gpt_register_logs/` 完成日志按 `register_log_keep`（默认 200，20–2000）保留最新 N 份，不再无界增长。
+ [修复] 接入 New API / one-api 等网关时链接生成：网关以局域网渠道地址重新发起请求，未配置 `base_url` 时返回的图片 `url`/任务链接全是局域网地址。URL 生成现支持 `X-Forwarded-Host`/`X-Forwarded-Proto`（真反代场景自动正确），`config.json` 顶层 `base_url` 仍最优先，未配置时启动日志打印提醒；`docs/operations.md` 新增网关接入章节（含 `b64_json` 默认与 WebDAV `public_base_url` 替代方案）。
+ [测试] 新增调度背压、传输、注册、URL 生成回归（unittest 全量 735→752 通过），任务进度/检查点回调补注「不可抛异常」契约并合并 start 步骤双次更新。

## 1.8.11 - 2026-10-06

+ [新增] Resin / 代理连通诊断：`POST /api/proxy/test` 精确检查选中的网关（显式输入不再被全局代理静默替换），区分代理认证（407 / SOCKS 拒认）、握手（curl 97）、DNS、TLS、超时、HTTP 401/403、Cloudflare 挑战页、429（含 `Retry-After`）、上游 5xx、重定向与响应结构异常；200 还必须返回有效的 CSRF JSON 才算通过。返回 `proxy_id`（协议+网关+完整凭据哈希，凭据变更即失效旧观测）与 `gateway`，供跨日志关联，不泄露凭据。TLS 本地库回退重试与其他重试共用同一超时预算。
+ [新增] 系统日志完整分页：`GET /api/logs` 保留原 `items`，新增 `has_more` / `next_cursor`（默认 200 条，1–1000 可调）。游标锁定追加前快照，新增记录刷新后可见；删除或保留期裁剪使快照失效时返回 409 `log_cursor_expired`，日志页自动从最新记录重载。日志页新增「加载更早日志」，如实显示「已加载 N 条 / 已到最早记录」，并兼容不返回分页字段的旧服务端。
+ [修复] 日志凭据泄露面：控制台 Logger、系统日志读写、注册任务历史与完成报告统一脱敏——代理 userinfo、密码（含带空格的引号值与「生成密码」）、access/refresh/session token、Authorization / Cookie（含分块 session-token）、OAuth 回调 URL 的 `code` / `state`、`code_verifier` / `totp_secret`、OTP 与 JWT 全部遮盖且重复脱敏幂等；匿名 `token:<hash>` 关联标识保留。注册引擎不再把验证码打印到 stdout。
+ [改进] 账号日志降噪与证据分层：周期性检测只记录额度 / 状态 / 图片门控等实质变化，略过纯时间戳刷新；403 登录失败（`authorize_failed_403` 等）不再被当作 token 吊销证据，自动密码重登仅在确认吊销标记或手动操作时触发；更新与删除记录携带可关联的匿名引用与变更字段清单。
+ [新增] 可选出口观测缓存（`data/egress_reputation.json`，独立于账号选号）：单次 `/cdn-cgi/trace` 探测、复合身份键、TTL 15 分钟、上限 1024 项，成功与失败同样过期、损坏文件报错保留原件，不做任何自动轮换或路由判定。
+ [测试] 新增代理诊断 / 脱敏 / 分页 / 观测缓存回归（unittest 全量 713 通过、分页 pytest 68 通过），CI 在 unittest 之外独立执行分页用例。

## 1.8.10 - 2026-09-28

+ [修复] 全局代理保存后「测试代理」必定失败：1.8.9 起设置页只拿得到脱敏后的 `socks5h://[REDACTED]@主机:端口`，测试时把它原样当成账号密码去连，Python 3.13 的 URL 解析还会因方括号直接抛错（接口 500）。现在测试会把脱敏占位换回已保存的真实凭据；保存时若改了主机或端口却仍保留 `[REDACTED]`，返回明确的 400 提示，不再悄悄沿用旧地址。设置页 `主机:端口:账号:密码` 简写此前绕过了脱敏、明文回传，一并修正。已保存的凭据一直完好，运行时读取的也始终是真实值。
+ [修复] 上游对高风险账号 / 出口要求 Arkose 验证时抛裸 `RuntimeError`，第一个号就整单失败、不换号。现视为该号本次不可用：按普通失败结算（不标异常、不删号）并换下一个号，全部账号都被要求时返回 503 `upstream_arkose_required`。
+ [修复] 巡检每小时白探一次「异常 + 有密码」的 session_only 死号：未开 `auto_relogin_after_refresh` 时巡检根本不会用密码重登这类号，却把密码算作可恢复手段。手动「检测」不受影响。
+ [修复] 备份包补上 `docs/operations.md`「至少备份」里列出却一直漏掉的 Grok 号池（`data/grok_accounts.json`）与注册机密钥 / 表单（`data/gpt_register.env`、`data/gpt_register_config.json`），设置页可分别关闭。定时备份失败后不再像成功一样等满整个周期（默认 6 小时），改为 5 分钟起按连续失败次数翻倍重试，封顶为备份周期。
+ [新增] `CHATGPT2API_DATA_DIR` / `CHATGPT2API_CONFIG_FILE` 可把数据目录与配置文件指到别处（默认不变）。
+ [调整] 账号页「刷新」后不再用假的进度动画冒充重新登录进度（重登在服务端后台线程执行，并无进度可查；该动画的定时器还会泄漏），改为提示已在后台发起的重登数量。
+ [清理] 删除 `openai_v1_response.py` 里 1.8.0 移除搜索 / 文本接口后残留的孤儿函数（引用了已不存在的符号，误调即 `NameError`）与 `openai_backend_api.py` 中不可达的 `_build_requirements`；`openai_v1_chat_complete.py` 补上缺失的 `count_message_text_tokens` 导入。
+ [测试] 测试套件不再写入仓库里正在使用的 `data/`：此前每跑一次都会往真实图库存一张测试图片、往日志追加约 110 条假记录，并虚增「累计入库」计数。`test/__init__.py` 现在把数据目录和配置文件指向临时目录。

## 1.8.9 - 2026-09-28

+ [新增] 非官方豆包网页生图：`POST /v1/doubao`、`GET /v1/doubao/status`。上游 `POST www.doubao.com/chat/completion`（`aid=497858`，`skill_type=4` ImageGeneration）。需要登录 Cookie；人机验证走请求 `captcha`、`solve_url` 或 Capsolver/2Captcha/YesCaptcha。可选透传 `a_bogus`。`POST /v1/images/generations` 且 `model=doubao*` 走同一后端。文档 `docs/doubao.md`。
+ [新增] 非官方 360智图文生图：`POST /v1/zhitu360`、`POST /v1/zhitu360/query`、`GET /v1/zhitu360/status|models`。上游 `image.360.com/api/v1/zhitu/text/to/image/{create,query,v2/config}`。模型 `jimeng` / `jimeng40` / `jimeng45` / `hunyuan` / `tongyi` / `wanx21plus`。未登录 20601→401，欠费 20603→402，风控 hit_risk→403。打码同样外接。`model=zhitu360|jimeng*|hunyuan|tongyi|wanx21plus` 可走 `/v1/images/generations`。文档 `docs/zhitu360.md`。
+ [新增] 共享 `services/captcha_solver.py`：显式 token、`CAPTCHA_SOLVE_URL` webhook、SaaS 打码。新增 `test/test_cn_images_api.py`。
+ [新增] GPT 注册收信渠道可选自建 [123nhh/tempmail](https://github.com/123nhh/tempmail)（设置 → GPT 注册 → 收信渠道）：REST 直连、Postfix 收信即落库，不再绕 Cloudflare Email Routing → Worker → D1 → Cloudflare API。验证码优先从邮件主题提取（每轮只一个列表请求）；带「测试」按钮列出实例可用域名；API Key 脱敏存储。tempmail 邮箱有 TTL，Codex 补 OTP 时按原地址自动重建；切换渠道后旧号按域名自动走原渠道。适配 tempmail 限流（窗口会被每个请求续期）：撞 429 后本进程全部 tempmail 请求静默一整个窗口。
+ [新增] tempmail 多级域名：`multi` 模式默认由注册机生成 2 级随机子域名（`abc@k3x9q.m2a8.example.com`，层数可配，0 = 交给 tempmail 生成 10~14 级单词），每个号一个独立主机名；域名池支持 `*.example.com`（其下随机子域名）与 `a.example.com`（固定子域名），主域经 `/api/domains` 反查并缓存。
+ [修复] 一个请求回传两张一样的图片：一个 `sediment://file_00000000…` 指针会同时进入 file / sediment 两个 id 列表，同一 id 分别走 `/files/{id}/download` 与 `/conversation/{cid}/attachment/{id}/download`，两个接口签出的 URL 不同，字符串去重挡不住。现按 id 去重，并在下载后按字节再去重一次。另修两个次要来源：丢失 `conversation_id` 后按 prompt 恢复会话时，并发生成会认领同一会话（新增进程内会话认领表，且改用真实请求开始时间）；豆包分流把每张图拆成 `url` 与 `b64_json` 两条。
+ [修复] 日志管理看不到 `/v1/chat/completions`、`/v1/responses`、`/v1/doubao` 生成的图片：前两者以 base64 内联返回、无 `url` 字段可收集，后者只记了张数。现附带内部字段 `_image_urls`（发给客户端前剥离）记录已落盘 URL。
+ [修复] Grok：免费路径 200 但模型没调图片工具时，付费路径的 403（无额度）被当成鉴权失败，账号被标异常——一个被拒的 prompt 能逐个打死整个 Grok 池。现返回 422「未生成图片」并附模型原话，且不记失败、不再换号重试。另修：`ensure_fresh_account` 抛错时 `new_token` UnboundLocalError；刷新后 token 未变仍整轮重试；失败分类改按 HTTP 状态（原先对错误文本做 "401/403/429" 子串匹配）；限流号 15 分钟后自动回池；免费路径不再静默丢弃 `size`；抽图以 `image_generation_call` 为准，url 副本不再算第二张。
+ [调整] `/v1/models` 只列规范的生图模型 id：别名与旧名（`gpt-image-2`、`grok-imagine`、`grok-2-image-1212`、`即梦`…）仍可调用但不再列出；豆包 / 360智图在配置了 cookies 时列出。网页端生图也能使用这些模型（任务接口此前只认 ChatGPT / Grok）。
+ [重构] 模型 id 统一到 `utils/image_models.py`（后端唯一定义处）与 `web/src/lib/models.ts`（前端镜像，有测试校验一致）；路由、`/v1/models`、报错文案、各后端不再各自写字符串。报错文案里残留的 `gpt-image-2` 随之更正。
+ [安全] `/health` 未鉴权时不再返回存储路径 / 数据库连接串 / 代理拓扑，存储健康检查的异常信息脱敏；`/docs`、`/openapi.json` 默认关闭（`CHATGPT2API_ENABLE_DOCS=1` 开启）；`GET /api/settings` 补齐顶层 `proxy` 与 `ai_review.api_key` 脱敏；保存设置不再把 `.env` 里的密钥写进 `config.json`；启动时把 `data/` 下凭据文件收紧为 0600；管理员密钥改为常量时间比较。
+ [修复] 存储文件损坏时不再当作空列表并在下次保存时覆盖（`auth_keys.json` 损坏后新建一把密钥会清空其余全部）；结算前的超时检查不再丢弃已付费的结果（本地额度与上游不同步）；补号任务收尾异常不再让任务永远卡在 running、导致自动补号停摆；`auto_replenish_min_total_quota` 此前被 API 静默丢弃；手动「禁用」的账号不会再被刷新 / 检测洗回「正常」。
+ [测试] 修复测试套件（此前 25 失败 / 18 错误，注册引擎 805 行测试因导入路径错误从未运行）；CI 增加测试任务，并覆盖 `fork/**` 分支。

## 1.8.8 - 2026-09-20

+ [新增] 非官方 [www.waifu2x.net](https://www.waifu2x.net/) 超分 API：`POST /v1/waifu2x`（别名 `POST /v1/images/upscale`）、`GET /v1/waifu2x/status`。协议对齐公开网页表单（`style` / `noise` / `scale` / `format` + Turnstile）。Turnstile 支持请求透传 token、Capsolver / 2Captcha / YesCaptcha、以及 Patreon `ses_id` 跳过。设置项密钥在 `/api/settings` 中掩码。文档见 `docs/waifu2x.md`，CLI：`python scripts/waifu2x_upscale.py`。新增 `test/test_waifu2x_api.py`。

## 1.8.7 - 2026-09-15

+ [安全] 网络层加固（SSRF / 熔断 / 请求体上限 / 探测兜底）：
  - [修复] `/v1/chat/completions` 与 `/v1/responses` 消息内 `image_url` 此前**完全没有** SSRF 防护（`utils/helper._decode_message_image_url` 直连抓取），与 `/v1/images/edits` 的校验标准不一致。新增共享守卫 `utils/ssrf.py`，两条路径统一拦截私网 / 回环 / 链路本地 / 云元数据地址。
  - [修复] 重定向跟随导致 SSRF 绕过：`get_with_egress_fallback` 原先 `allow_redirects=True`，公网 URL 可 `302 → 169.254.169.254`。新增 `ssrf_guard=True` 逐跳校验（含相对 Location 解析、重定向环与跳数上限），且不安全 URL **绝不**回落到其他出口（不会试着用直连去够内网地址）。
  - [修复] 代理出口下的 DNS 语义不一致：原校验用本机 `getaddrinfo` 解析，但请求走 `socks5h`（远端 DNS），本地解析失败的域名会被放行。现改为 fail-closed——无法证明为公网的域名一律拒绝。
  - [修复] 出口熔断过激：`mark_egress_unusable` 原先任何一次连接错误（含单次 `timed out`）即拉黑 10 分钟，单代理部署下一次抖动等于 10 分钟全损。改为窗口内失败计数（`egress_blacklist_failure_threshold` 默认 3 / `egress_blacklist_window_secs` 默认 60）达标才拉黑；`OPENSSL_internal` 仍不计入。
  - [新增] 请求体上限：`max_request_body_mb`（默认 256，`0` 不限制）+ 纯 ASGI `RequestBodyLimitMiddleware`，同时拦 `Content-Length` 与分块 / multipart 流式 body（超限返回 413）；uvicorn 与 `main.py` 统一 `--limit-concurrency 256`（`CHATGPT2API_LIMIT_CONCURRENCY` 可覆盖）。
  - [优化] 出口地理探测（`utils/egress_locale`）加 User-Agent 与备用 provider（ipwho.is，HTTPS 免费档），主 provider（ip-api 明文免费档）失败时兜底，降低单点失败导致时区画像回退东京的概率。
  - [优化] WebDAV 上传超时不再固定 30s：按 body 大小缩放（30s 起 + 每 MB 10s，上限 300s），慢链路大图不再误超时。
  - [修复] `main.py` 的 `access_log=False` 与 Dockerfile 的 `--access-log` 不一致：本地 `uv run main.py` 现在也记录访问日志（`CHATGPT2API_ACCESS_LOG=false` 可关）。
  - [测试] 新增 `test/test_ssrf.py`（14）与 `test/test_body_limit.py`（4），`test_proxy_service` 补熔断计数与 SSRF 不回落直连的回归；顺带修复 1.8.3 改名后未同步的 `gpt-image-2` 旧断言。
+ [新增] 自动补号和生图换号容灾参数可配置：补号网络熔断阈值、失败冷却，以及生图账号/轮询/文字回复换号次数和临时失败冷却；`0` 的关闭语义不会被前端默认值覆盖。
+ [修复] 临时网络/轮询失败不再永久排除账号：按任务记录 60 秒冷却；确认吊销的凭证继续排除，同账号更新凭证后可用。
+ [修复] 图片账号槽使用统一租约释放，覆盖检查点写盘、出口初始化及异常处理失败；结算写盘失败不伪装为成功。
+ [修复] 原生成和续轮询共用持久化 generation_id，额度与结算 ID 一次提交；重复/并发续轮询与重启恢复不重复扣减，续轮询不释放其他请求的并发槽。
+ [修复] 续轮询、下载与换号重放共用 image_task_timeout_secs 总预算；上游请求按剩余时间设置超时，超时后取消继续下载/结算，迟到结果不能覆盖任务状态。
+ [优化] 参考图/蒙版独立保存于 data/image_tasks_inputs，校验内容摘要；旧 base64 任务自动迁移（失败保留原数据），进度写盘节流为每秒最多一次。任务备份包含输入附件，过期附件在元数据删除落盘后清理。
+ [修复] 认证失败（包括保留在池内的 session_only 失效账号）及模型只回文字时，排除失败 token 后换号；修复 soft prepare 401 重复记失败、刷新 token 提前释放槽位，以及收到图片后断流漏记成功/漏释放槽位。
+ [新增] 图片失败任务支持「续传 / 换号继续」：持久化提示词、尺寸、质量、参考图/蒙版和上游会话检查点。可访问旧会话则续轮询；会话失效或续轮询超时/连接失败时，用原始输入换可用账号重新生成，保留任务 ID。
+ [修复] 续传按任务所有者隔离，重复提交受状态保护，旧执行进度不能覆盖新执行；失败凭证使用哈希识别，同邮箱重新登录得到的新 token 不被永久排除。
+ [修复] 前端续传按 task ID 定位本地会话，不再混用上游 conversation ID；空池返回结构化 429 insufficient_quota，不把本地剩余额度当作已吊销账号可用的依据。
+ [修复] 数据库后端（postgres/mysql）`accounts.access_token` 原为 `varchar(2048)`：实测 token 约 1.9k 字符、余量仅约 100，超长 JWT 会触发 `value too long` 使**整次保存失败**（SQLite 不校验长度所以本地无感）。改为不限长的 `TEXT`，并对已有库在启动时幂等 `ALTER` 升级（DDL 失败不阻断启动）。新增 `test/test_database_storage.py`。
+ [新增] `logs.jsonl` 日志保留：`log_retention_days`（默认 30，`0` 不清理）。日志只追加不清理、且 `list()`/`delete()` 每次读全文件，长期运行后管理端日志页会变慢占内存；现启动时与每 6 小时流式裁剪（内存不随文件体积增长，坏行保留不误删）。新增 `test/test_log_retention.py`。
+ [新增] 注册补号支持**按额度触发**：`auto_replenish_min_total_quota`（默认 `0`=关闭）。原补号只按可用账号数量（min/target available），「号够多但额度将耗尽」无法自动补；开启后可用账号剩余生图额度总和低于阈值即开任务，与数量阈值是 OR 关系，spacing/失败冷却/批次/熔断全部共用。额度口径与生图请求一致（`account_service.total_image_available_quota`，只算选号器当前可用账号）。新增 `test/test_replenish_quota.py`（8）。

## 1.8.6 - 2026-09-12

### chatgpt2api-grok（本分支）

+ [发布] 版本 1.8.6：生图请求内「换号续传」补强 + 死号探活/补号闭环修复 + 注册域名池 + codex 报错指引。
+ [新增] 生图请求内续传增强（`_generate_single_image`）：
  - 轮询超时不再扎同一个号：超时号即时排除出候选集，下一棒**必然换号续传**（原逻辑可能反复选中同一个已挂死的号直到重试耗尽）。
  - 断流保结果：图片结果已到手后流再报错（如 curl(56) 连接重置），直接返回已生成的图片，不再把成功结果判死成 `upstream session expired`。
  - 轮询超时会写入 `last_connection_error`，多次超时耗尽候选池时报错指向真实原因而非笼统的空池文案。
+ [分析] 账号死亡模式定性：重建 29 个号的完整生死时间线，确认死亡呈**上游批量封禁波**特征而非自然过期——09-09 注册的号活了 45 小时、09-10 的 17~19 小时，全部死于 09-11 12:49 UTC 同一分钟内的单一波次；1.8.5 时代注册的号躲过该波但也在 13~24 小时内死亡。当前注册路径（单域名 + 单数据中心出口 IP 45.192.199.151，实测代理无会话语法、无轮换）关联度过强，是波次团灭的主因。
+ [新增] 注册邮箱域名池 `cfd1_domains`：多条按行/逗号分隔，每次注册随机取一个作为 `cfd1_domain`（不能用 per-job 索引轮询——自动补号任务恒为 count=1/index=1，会永远钉死在第一个域名），对抗上游按邮箱域名的批量封禁。所有域名须配置 Cloudflare Email Routing catch-all 到同一个 Worker/D1（`wait_for_code` 按完整地址过滤，天然支持多域）。设置页加「域名池（随机轮换）」输入框，任务日志记录每次选中的域名。
+ [修复] `auto_replenish_target_available` / `auto_replenish_spacing_secs` 昨天漏加进 `api/gpt_register.py` 的 Pydantic 模型——设置页这两项的保存被 FastAPI 静默丢弃（模型注释里早有此坑的警告）。已补上并加回归测试。
+ [修复] 健康 session_only 号的巡检跳过逻辑导致死号装死：1.8.1 为避免探活踢 session，JWT 不到期就不探活，但免费号 JWT 有效期约 9 天而封禁波几小时就把号打死——死号挂着「正常」状态最长 8 天，自动补号误判池子已满（stocked）不再补号。现改为：`last_probed_at` 超过 30 分钟（`_SESSION_ONLY_PROBE_STALE_SECONDS`）的健康 session_only 号重新纳入巡检，探活只是带现有 token 的 GET /me（普通页面加载级行为），session 续期只会在 /me 已 401 后才触发。实测重启后 26 个死号当周期全部正确标记，补号闭环恢复。
+ [说明] 出站代理池轮询注册早已支持（`parse_proxy_pool` + `bind_register_proxy`），本次实测当前代理为固定单出口不支持会话语法；如需按号隔离出口 IP 只需在注册配置里填多条代理。
+ [观察] 个别死号的 /me 返回 Cloudflare challenge 页（403 HTML）而非 401——服务按设计只在 401 确认时标记，此类「僵尸号」会暂时占住 1 个可用额度位。补号按 target 水位运行会自动补偿，暂不改动（CF 抖动期误标的代价更大）。
+ [修复] `codex-gpt-image-2` 空池报错文案误导：原消息建议「re-register a live free account」，但 codex 链路（`_ensure_codex_source_account`）只接受 source_type=codex 的 OAuth 号，自动注册的免费 web 号永远填不进 codex 池。现按 scope 给出可行指引：codex 模型需要导入 Codex OAuth 号（Plus/Team/Pro），否则改用 `gpt-image-2.5`（web 链路）。注意 `/v1/models` 目录只在池内有 codex 号时才列出 codex 模型——客户端应从目录取模型而不是写死。

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
