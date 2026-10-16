# 未发布改动

基于 1.8.12，本批次保留现有用户设置和数据。

## 修复与优化

- 号池管理：修复 `GET /api/accounts` 在图片任务运行期间抛出 `int() argument must be a string, a bytes-like object or a real number, not 'list'`。图片在途状态已改为租约到期时间列表，列表接口仍按整数读取。现在调用统一计数函数，只统计有效租约，兼容旧整数计数，过期租约显示为零。
- WebDAV 会话：一次性客户端在图片保存、读取、删除、批量同步和连接测试后统一关闭；成功、失败、索引写入失败及无效连接地址均释放连接。连接测试由调用方统一关闭会话，避免重复关闭。
- 配置并发：`ConfigStore` 的完整读取、更新、配置 getter 和存储后端初始化使用 `RLock`。配置快照及更新参数复制嵌套数据，避免调用方修改返回值或请求对象时影响内部配置。掩码凭据仍沿用原有保留规则。
- 管理接口：账户、用户密钥、注册任务、配置、图片及存储的阻塞操作移到线程池。补充账户读取等待期间其他接口仍可响应的回归测试。
- 本地图片清理：保留期清理和磁盘空间回收只删除本地副本及缩略图，及时更新索引；保留 WebDAV 副本、索引和标签。仅本地图片的标签批量删除，并与并发标签修改使用同一把锁。磁盘回收支持 PNG、JPG、JPEG 和 WebP。
- 清理扫描：普通请求按扫描结果节流；后台任务可强制扫描；修改保留天数立即使节流失效。空目录在没有过期文件时也会清理。后台每轮读取 `image_min_free_mb`（默认 500MB），失败写入日志。清理使用独立锁，避免与配置、索引锁形成相反的嵌套顺序。
- 图像输入：参考图上限 16 张，单图 50MB，image 与 mask 累计 200MB。内联 Base64 延后解码，在解码前检查引用数量和编码长度；上传文件按可用字节预算读取，拒绝请求时关闭尚未读取的文件；远程图片按流读取并限制响应大小，保留代理回退和 SSRF 校验。请求体仍受 `max_request_body_mb` 限制。
- JSON 兼容字段：按 `images`、`image`、`image_url` 优先级选择首个非空来源；空数组、空字符串和 null 可回退，多个非空兼容字段不重复累计。已用本地 JSON/multipart 测试验证；没有真实 New API 网关转发样本，不能将该兼容处理认定为网关多图问题的唯一原因。
- 图像数量：协议层统一校验 `n`，单元素数组可归一化，含多项、嵌套数组、非整数及越界数量返回明确的 400。此处是另一个输入校验问题；本次号池报错源于在途租约列表。
- 内容审核代理：请求使用 `upstream=True`，支持仅配置 runtime 代理的场景；legacy 全局代理原本就可以作为兜底。首选顺序为账户代理、runtime 代理、显式代理、legacy 全局代理、直连；资源请求可使用 runtime 资源代理。
- 环境代理隔离：本地复现 curl_cffi 0.15.0 在 `proxy=""` 或仅 `trust_env=False` 时仍使用环境代理。统一会话显式指定直连，并让配置的代理优先于环境 `NO_PROXY`；内容审核与出口地域探测改用同一会话工厂，成功及失败后关闭会话。没有修改进程或用户保存的代理配置。
- 绑定代理：图片任务切换到另一个使用同一失败代理的账户时，不再自行创建直连候选；绑定账户只使用自己的出口，未绑定账户仍按原规则回退。
- 实际出口：OpenAI 客户端记录会话真正选中的代理，地域探测、身份保存与 clearance 匹配沿用该出口，避免回退时使用首选代理的 Cookie。clearance 刷新和失效支持指定实际出口；过期缓存不再被返回或计入可用状态。
- Grok 代理：沿用 runtime 的 SSL 校验设置，多个出口重试共享剩余时间预算，避免每个出口重新等待完整超时。
- 资源下载：连接失败纳入既有出口失败计数；逐个重定向使用剩余时间预算；SSRF 拒绝、重定向循环或超限时关闭响应。
- Cloudflare 清障：统一会话在指定状态码上识别挑战响应，FlareSolverr 按原账户、域名和实际出口刷新凭据，保留登录身份重建连接并最多重试一次。普通鉴权、代理认证、限流及 JSON 错误不触发。并发挑战合并刷新；刷新与重试使用剩余时间预算；不可重放的文件流不自动重试。
- 启动预热：`warm_up_on_start` 接入应用生命周期，在可停止的后台线程预热默认出口及非禁用账户，按账户身份和实际出口建立缓存。身份键优先使用稳定的账户标识、邮箱或令牌摘要，补齐设备 ID 不会导致预热缓存失配；账户之间仍隔离。
- 浏览器身份：清障请求使用与 Cookie 配套的 User-Agent，并抑制冲突的旧客户端提示头；OpenAI 客户端同步请求/PoW 使用的 UA。重建连接保留 CookieJar 中的安全属性和鉴权头，替换过期的同域 clearance Cookie。
- 资源代理：未绑定账户的专用 `resource_proxy_url` 不再被图片任务的 API 出口覆盖；绑定账户仍使用绑定出口。CDN 和远程引用下载也能获取各自域名/出口的 clearance，且不转发 ChatGPT 的手动 Cookie。
- 清障入口：覆盖 OpenAI、账户探活/刷新/登录、内容审核及现有 curl 上游会话；Grok 的 requests 客户端继续沿用代理/TLS/预算策略。界面说明自动恢复、后台预热和缓存有效期，保留现有开关及值。
- 会话模型 404：上游 `POST /backend-api/f/conversation` 返回 `404 {"detail":"Model not found"}` 时，不再一律按「账号级终态」把凭据停放 24 小时。匿名链路 `/backend-anon/conversation` 在完全不带凭据时返回同一个 404，说明这是 `-convo` 后端的全局条件而非账号问题；据此加入熔断器：单个凭据命中按局部故障长停，300 秒窗口内出现 2 个及以上不同凭据则判定为全局故障，改走 60 秒短停并立即返回 `upstream_model_not_found_global`，不再逐个账号重试；判定为全局时，窗口内先前被长停的凭据会回溯改短；熔断期内新请求在入口直接失败，不再走一遍号池。仍不摘除凭据（token 是活的，文本能力未受影响）。
- 图片模型 slug：`_image_model_slug` 不再把 web 图片模型映射到 `gpt-5-3`（该 slug 已从 `/backend-api/models` 目录下架，目录现为 `gpt-5-6`、`gpt-5-3-mini`、`gpt-5-6-mini`、`gpt-5-6-t-mini`、`gpt-5-6-t-mini-mini`、`research`、`gpt-6`、`gpt-6-t-mini`、`gpt-6-mini`、`gpt-6-t-mini-mini`、`auto`），改用 `auto`——它既是账号默认值，也是真实客户端抓包里的默认值。Codex 链路仍透传自己的上游 id。
- 协议对齐：`/f/conversation/prepare` 补上真实客户端必发的 `client_prepare_dispatch`（`immediate`）与 `client_prepare_source`（`composer_editor_state`）；会话默认头补上真实客户端使用的 `oai-did`（此前只发 `oai-device-id`，名名不符）。两者均为静态逆向 bundle 得出的字段，不影响既有请求形态。
- 404 已消失（服务端修复，2026-10-16 12:26 +08 实测）：`/backend-api/f/conversation` 通过率从 0/330 恢复到 12/12 与 15/18（~90%）。**归因实验证明不是客户端改动**：把上轮修掉的三重矛盾指纹（`Sec-Ch-Ua: "Microsoft Edge";v="143"` + `Sec-Ch-Ua-Full-Version: "143.0.3650.96"` + Chrome 142 UA + chrome142 TLS）原样恢复后**依然 5/6 通过**。`browser_identity()` 是独立的指纹缺陷修复，不是 404 的解。仓库自身链路恢复：`_stream_picture_conversation("a red apple on a table", "gpt-image-2.5", [])` → 26 事件 / 22.6 秒 → `sediment://file_00000000877882068955f39f5af4bb64`。
- 协议侧新形态（2026-10-16 bundle，`data-build` → `prod-77d3f10033b6f4343e288ea3ce19c2a23660bce3`）：`/f/conversation/prepare` 的真实客户端新增 `X-Web-Mobile-Conversation-Renderer: octane`、`x-web-mobile-prepare-state`、`x-web-mobile-prepare-reason` 三个头；sentinel 改为从 `/backend-api/sentinel/sdk.js`（1474B 桩）加载 `/sentinel/20260219f9f6/sdk.js`（30864B 真实现）；头名映射表新增 `telemetryToken: "OAI-Echo-Logs"`（输入事件时序 `focusin/focusout/copy/paste/touchstart/touchend` 的 `type,timestamp` 前 10 条）与 `timingToken: "OAI-Telemetry"`。**PoW 算法未变**，真 SDK 的 `check()` 与本仓库实现逐字一致。**未上 VMP**：真 SDK 无 `WebAssembly`/`wasm`/`eval`/`Function(`/`Worker`/`blob`，是老式字符串数组混淆，算法仍可完整复刻。
- 号池「封得快」的根因修复：`_validate_access_token_alive` 把 **401 与 403 一并**当作「确认已死」，但 chatgpt.com 对**被挑战或被限流**的客户端同样回 403（`{"detail":"Unusual activity has been detected from your device. ..."}`）。整个号池共用一个出口 IP，批量探测 `/backend-api/me` 必然触发限流，于是健康账号被永久判定为吊销 —— 实测 50 个异常账号中 **19 个从未发出过任何请求**（`last_used_at` 空、`success=0`、`fail=0`）。新增 `_auth_dead_verdict()`：401 → 吊销；403 → 交由既有的 `response_diagnostics` 分类，`http_forbidden` → 吊销，`challenge`/`rate_limit` → 不吊销，其它 → 无判定；诊断自身抛异常时返回无判定（绝不因诊断出错而判死账号）。`_CHALLENGE_MARKERS` 补入 `"unusual activity"`（它以 JSON 体返回，仅凭 content-type 会被误判成普通 403），补在共享定义里所有调用方受益。`utils/network_diagnostics.py` 的 `error_details` docstring 本就写着 *never infer revocation from a 403*，本次是把该原则接入验证路径。
- 号池数据面结论（供后续排查参考）：51 个账号全部 `session_only`/`fragile`（无 `refresh_token`），`access_token` 寿命恰好 10.0 天，49/51 的失效发生在 token `exp` **之后**（自然过期而非封禁）；失效时间聚集在两个批量刷新点（`09-22 16:38`、`09-27 16:34~16:35`），那是本地刷新循环**发现**的时间。`invalid_count` 是探测失败计数（与账号年龄正相关），不是风控信号。仓库已有 `codex_upgrade_service`（Codex client_id 重跑 OAuth 拿 refresh_token）可解 `session_only`，但当前 `skip_codex: true`、`auto_codex_upgrade: false` 未启用。
- Sentinel 头补全（2026-10-16 bundle 逆向）：真实客户端的头名映射表（`a-BiuthBl1.js`）里有两个本仓库从未发送的头 —— `OAI-Echo-Logs`（`telemetryToken`）与 `OAI-Telemetry`（`timingToken`）。前者是输入事件时序：把 `focusin`/`focusout`/`copy`/`paste`/`touchstart`/`touchend` 的前 10 条事件编码成 `"<type>,<timestamp>"` 对、逗号连接（`a--tOLhrKL.js`）；后者在 SDK 未产出 timing 时回退字面量 `"[1,null]"`。新增 `SentinelEchoLogs`（进程级 tracker，时间戳随进程存活增长，与浏览器 `event.timeStamp` 语义一致）与七个头名常量，两个 sentinel 头构造器都补上这两个头。头名常量同时替换了原先散落的字符串字面量。
- 注册链路本地实跑验证通过：修复后 `register_cli.py register chatgpt` 完整走通 —— `oai-did` 种上 → CSRF 200 → OAuth → `/email-verification` → OTP 收到 → 校验 200 → `about_you` → `create_account`（SO token 1840 字符） → NextAuth session → **注册成功**。新账号 `GET /backend-api/me` 200，生图拿到 `sediment://file_00000000a3988209aaa0537d3c712b41`。
- 邮箱域名与深度的实测结论（修正一处先前的错误归因）：投递失败是**域名级**问题，与子域深度无关。三组并行对照隔离了变量 —— `5122331.xyz` 在 depth 1 / **10** / 12 / 17 全部成功；`233159.xyz` 在 depth 1 与 12 全部超时；`awa91.cyou` 在 depth 10 连续 3 次超时（确定性）；`cyt233.dpdns.org` 命中 DBL 黑名单。直接查 tempmail 服务端邮箱确认：`awa91.cyou` 的 3 个地址全部 0 封，`5122331.xyz` 成功那次 1 封。两个域名的 DNS 都正常（DoH 查得通配符 MX `10 mail.<domain>.`），失败发生在服务端的域名投递配置。**唯一可投递的域名是 `5122331.xyz`**。
- `tempmail_subdomain_depth` 由 12 调整为 10：`_random_label` 每层约 32,112 个标签，depth=10 时单域名子域组合约 10^45，配合 local-part 的 36^12 ≈ 10^18.7，可用地址数达 10^63 量级，地址空间不再是瓶颈（depth=1 时仅约 10^4.5，几百个号后必然重复）。`tempmail_domains` 收敛为 `5122331.xyz`，`tempmail_domain` 置空以走域名池。
- 注册 warmup 的 sticky 用户名回退：`apply_sticky_session` 对 `socks5h://` 默认启用，把用户名从 `ob` 改成 `ob.<session_id>`，而该池**只接受裸用户名** —— 实测 `ob` 每次连通、`ob.<任意后缀>` 一律 `curl: (97) User was rejected by the SOCKS5 server`。于是 sticky 后缀把可用代理变成死代理，warmup 拿不到 `oai-did`，注册在花邮箱前就报 `初始化会话失败: oai_did_missing`（远程日志：03:04–19:01 UTC 每个任务、每个条目都是这一个错误）。更糟的是「换出口」只会铸出另一个同样被拒的后缀。新增 `strip_sticky_session()` 与 `is_socks5_user_rejected()`（只匹配 `(97) … rejected by the SOCKS5 server`，不匹配 `(35) TLS`，后者是被挑战的出口、应当继续旋转），warmup 捕获该错误后去掉后缀、重建会话并重试；换出口循环同样回退并在回退后停止旋转。抽出 `_rebind_session()` 供两处共用。
- 浏览器指纹自洽性：此前 UA、客户端提示、TLS 目标来自三个互相独立的来源，实测产出了真实浏览器不可能产生的组合（UA 说 `Chrome/142`、`Sec-Ch-Ua` 说 `Microsoft Edge 143`、`Sec-Ch-Ua-Full-Version` 是 Edge 的 `143.0.3650.96`、TLS 是 `chrome142`）。新增 `browser_identity(impersonate)`，从 **TLS 目标**派生唯一的 UA + 客户端提示集合，`_build_fp` 不再硬编码任何浏览器版本；`__init__` 里硬编码的 `Sec-Ch-Ua-Full-Version` / `-List` 改读 `self.fp`。存量 UA 按家族+主版本校验，同家族同版本保留，跨品牌/跨版本才替换；Firefox/Safari 不发客户端提示。

## 回归测试

- 号池：真实本地槽位申请/释放、过期租约、旧整数计数、阻塞 I/O 时的接口响应性。
- 配置与标签：并发部分更新、磁盘与内存一致、嵌套快照隔离、存储单例及并发标签修改。
- WebDAV：成功和异常路径的连接释放、批量同步索引写入失败、无效测试地址、会话只关闭一次。
- 清理：保留远端归档、索引与标签同步、缩略图、全部支持格式、空目录、扫描节流、设置更新、dry run 和后台阈值更新。
- 图像：16/17 张边界、单图和 mask 超限、累计超限、恰好达到单图/累计限制、同步与任务接口、上传有限读取、提前拒绝 Base64、远程分块响应上限。
- 大小测试缩小字节阈值以验证真实路径，并独立核验生产默认值，避免构造数百 MB 测试请求。修正旧 image+mask 测试的 120MB 单图先触发单图限制的问题。
- 代理专项：本地 HTTP 目标和模拟代理验证环境变量隔离与显式代理；覆盖绑定出口、实际出口与 Cookie 匹配、过期缓存、Grok TLS/超时、资源失败记录、重定向预算及异常时连接释放。
- 清障专项：本地真实 HTTP 挑战/重放、一次重试上限、状态码与 HTML 识别、普通错误不误重试、失败/超时停止、流式挑战、并发合并、后台预热与关闭、真实 CookieJar 保留、跨域 Cookie 隔离和独立 CDN 出口。
- 会话模型 404：分类器（结构化错误、格式化字符串、`conversation_not_found` 兄弟错误、7 类无关失败）、熔断器（单凭据不触发全局、同凭据重复命中保持局部、2 个不同凭据触发、窗口过期、reset）、失败转移（局部长停、全局短停并回溯纠正、熔断期内零凭据访问、全池失败抛 503）、slug 映射（目录内 slug、不再指向已下架 slug、Codex 透传、未知回退）及 prepare 体字段。
- 浏览器指纹：`browser_identity` 的家族/主版本/UA/客户端提示自洽性、未知 impersonate 回退、`matches_ua` 的跨品牌与跨版本拒绝；会话头与 TLS 目标一致、无跨品牌混用、存量跨品牌 UA 被替换而同家族同版本被保留。
- 403 判定：401 与普通 JSON 403 判为吊销；`Unusual activity`、`cf-mitigated`、HTML 挑战体不判吊销；429 与 5xx 无判定；诊断抛异常时无判定（不判死账号）；`_validate_access_token_alive` 的 `True`/`False`/`None` 三态语义。
- Sentinel 头：头名与真实客户端映射表逐字一致、`[1,null]` 回退字面量、`OAI-Echo-Logs` 的 `type,timestamp` 对形态与事件类型序列 `[0,3,4,5]`、时间戳单调不减且随进程存活增长、事件数不超过前端的 10 条上限、两个构造器都发出这两个头、无 proof/turnstile 时不凭空造头。
- 注册 warmup 的 sticky 回退：`strip_sticky_session` 去后缀/无后缀 noop/无凭据 noop/与 `apply_sticky_session`+`rotate_sticky_session` 往返；`is_socks5_user_rejected` 只认 97 凭据拒绝、不认 35 TLS；warmup 在 97 时回退到裸凭据并成功种到 `oai-did`、在 35 时继续旋转、无后缀时无可回退。

## 清障行为与验证范围

- 功能沿用现有配置开关，默认关闭，没有自动改写设置。手动模式使用用户提供的 Cookie/UA，只能重建连接，不能自动取得新 Cookie；全局手动 Cookie 仅用于 `chatgpt.com`。
- 管理端测试是匿名缓存；账户请求使用各自缓存，按需自动刷新或启动预热填充，不把匿名浏览器会话直接共享给所有账户。
- 实际 Cloudflare 放行由上游决定。本轮使用本地 HTTP 和 FlareSolverr 替身验证完整恢复流程，没有连接真实清障服务或外部模型；TLS 指纹仍由已安装的 curl_cffi 支持范围决定。
- legacy 全局代理针对上游调用，WebDAV、R2 备份和注册/邮箱链路保留各自的直连或独立代理策略。

## 本地验证

测试包将 `CHATGPT2API_DATA_DIR` 和 `CHATGPT2API_CONFIG_FILE` 指向临时目录；网络命名空间仅启用回环接口，供本地模拟服务使用，外部网络不可达。

```bash
unshare --net bash -c 'ip link set lo up && exec .venv/bin/python -m pytest -q --tb=short'
```

全套结果：922 passed、17 skipped；另有 101 项 subtests 通过。跳过项依赖真实服务。两项已有警告涉及 SQLAlchemy 弃用接口及安全 Cookie 属性。前端类型检查 `web/node_modules/.bin/tsc --noEmit --incremental false -p web/tsconfig.json` 通过。

验证前后，现有 `config.json`、`VERSION` 和 `data/` 内共 113 个 JSON/版本文件的内容哈希未变化。没有执行真实注册、外部生图或部署，没有更新版本号、`CHANGELOG.md`、发布标签或创建新发布。
