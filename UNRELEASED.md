# 未发布改动

基于 1.8.13，本批次保留现有用户设置和数据。

## 修复与优化

- Grok 刷新网络错误：远端 `refresh network error: HTTPSConnectionPool(host='auth.x.ai', port=443): Read timed out.` 的根因**不是代理不通**（同一代理打该端点 0.4 秒返回）。真实链路是：该 SOCKS 池对**新建** TLS 握手有 25-50% 的 `SSLEOFError: UNEXPECTED_EOF_WHILE_READING` 抖动（复用 keep-alive 连接实测 8/8 成功）；urllib3 把这个抖动包装成 `Max retries exceeded`，正好命中 `_is_egress_connect_error`，于是**一次丢包被当成「出口已死」** —— 拉黑健康出口并回退 `direct`，而直连无路由，挂满整个 30 秒预算，最终抛出的直连超时掩盖了真实原因。修复：新增 `is_transient_tls_error()` 两级分类（握手 EOF 可重试任意方法；连接重置等**阶段不明**的失败仅对幂等方法重试，避免重放一个可能已轮换一次性凭证的 POST），`grok_backend_api._request` 对**同一出口**重试 3 次后才降级，并优先抛出主出口的错误而非回退路径的超时。实测真实代理 12/12 = 100%。
- Grok 死 refresh_token 不再被当成健康号：`_classify_failure` 只认 401/403，而 xAI 的 token 端点对失效 refresh_token 返回 **HTTP 400 + `invalid_grant`**（RFC 6749 §5.2，永久性错误，重试无用），且 `status is not None` 分支在检查文本前就把它判成 `other`；`ensure_fresh_account` 又硬编码 `{401, 403}`，`refresh_accounts` 更是**只写 `last_error`、从不改 `status`**。三处叠加的结果是：一个 `access_token` 已过期 85 天、每次刷新都被拒的账号，在号池里始终显示「正常」并被反复选中。修复：`invalid_grant` 在状态回退分支**之前**判定为 `auth`（仅此一个 OAuth 协议错误码穿透，`_classify_failure(502, "HTTP 403 somewhere in attempts")` 仍为 `other`，既有裸数字防护不变），`ensure_fresh_account` 改走 `_classify_failure`，`refresh_accounts` 按判定结果落 `异常`/`限流`（网络抖动仍保持「正常」）。
- 测试隔离：新增用例曾用 `mock.patch.stopall` 清理，它会连带停掉**其他测试模块**正在生效的 patch，导致 `test_image_recovery_regressions` 随机失败。改为显式 `addCleanup(patcher.stop)`。

## Grok 网页协议逆向与媒体能力（2026-10-19 ~ 10-21）

> 全部结论基于对 `grok.com` 前端 bundle（1367 chunk / 56.2 MB）的静态逆向，
> 加上真实凭证的实测验证。完整协议文档见 `docs/grok-web-protocol.md`。

### 网页生图打通

- **模型名是 `build`**（`modelName: "build"` + `kind: "CONVERSATION_KIND_IMAGINE"` + `mediaGenInput.textToImage`）。`grok-3` 通过名称校验但 `modelNotAvailable`；`grok-4.7` / `grok-4.5` / `grok-4.20-*` / `grok-build-0.1` 一律 403 `Model is not found`。这些名字来自 **`POST /rest/models`**（GET 返回 501，共 33 个模型）。
- **图片在 `cardAttachment` 里**，不是 `streamingImageGenerationResponse`（后者实测为空）。`jsonData` 是**字符串化的 JSON**，要二次解析。进度分两帧：`seq=0 progress=50`（路径带 `-part-0`）→ `seq=1 progress=100`（去掉）。`ModelResponse` 里的 `<grok:render card_type="generated_image_card">` XML 标记**不含图片 URL**。
- **下载需要 Bearer**：`GET https://assets.grok.com/<imageUrl>` 带 Bearer → 200；无认证 → 403。实测下载 65,930 字节 → 784×1168 RGB JPEG。
- **`upload-file` 是 JSON + base64，不是 multipart**（`chatUploadFileRaw`）：body `{fileName, fileMimeType, content(base64), makePublic, fileSource, thirdPartyFileId, workspaceId}`。用 `files=` 会 400。
- **直连模型名比 `build` 快一倍**：`imagine-image-gen` + `imageGenModelConfig {aspectRatio, mode:"fast"|"quality", resolutionName?}` → 实测 6.5s（`build` 是 12.5s），且支持 `quality`+`2k`。`imagine-image-edit` + `imageEditModelConfig {imageReferences[], aspectRatio?(仅单图), selectionRegions?}` 是图生图入口。
- **视频生成可用**：`modelName: "imagine-video-gen"` + `mediaGenInput.textToVideo` → 200 返回 video id；`imagine-video` 是错名（403）。视频是异步任务，`/rest/media/video/*` 里没有状态查询端点，产物获取方式未确认。
- **`mediaGenInput` 是六选一的 oneof**：`textToImage` / `imageToImage` / `textToVideo` / `imageToVideo` / `referenceToVideo` / `videoExtension`，各变体字段已全部提取。

### 生图被拒的三种形态

| 形态 | 表现 |
|---|---|
| 请求层 429 | `{"error":{"code":8,"message":"Too many requests"}}`，流开始前就被拒 |
| 流内限流 | `renderToolRateLimited` + `image_chunk.systemErrCode: 1006`；**`progress` 仍是 100 但无 `imageUrl`** |
| 静默降级 | HTTP 200、无 error、无 `cardAttachment`，正文却说"已生成" |

**`silent_degrade` 有两个完全不同的成因**（2026-10-21 对照实验）：

- **① 请求体不完整** —— 缺 `disableSearch` / `isReasoning` / `modelMode` 任一，或缺 `x-xai-request-id` 头。正文声称已生成但无 `cardAttachment`；换完整 body 立刻成功。**这是调用方 bug，不是配额问题。**
- **② 真配额耗尽** —— 用完整 body 连发约 4–8 次后出现，表现为 429 或正文为空。

**排查顺序：先确认走的是 `build_body`（完整字段 + request-id 头），再看配额。** 用精简 body 探配额会得到**假阳性**。

> **教训**：判定生图成功必须看 `cardAttachment.image_chunk.imageUrl` 是否存在 —— 不能看 HTTP 200，也不能看正文。

### 额度与限流

- **网页额度端点是 `GET /rest/grok/credits`**（不是 `/rest/media/imagine/quota_info`）。从 bundle 模块 `9096207` 的 base64 `fileDesc` 解码出完整 protobuf：`service GrokBuildBilling` 的 14 个 RPC、`GrokCreditsConfig`（11 字段）、`enum Product`（含 **`PRODUCT_GROK_IMAGINE = 5`**）。
- **SSO cookie 可读**：`Cookie: sso=<token>` → 200。CLI 签发的 OAuth token 则**看账号** —— 四个凭证 JWT 的 `aud`/`client_id`/`scope` 完全相同，只有 `team_id` 不同，其中 1 个能读、3 个 403 `bad-credentials`。
- **但生图不计入这个账本**：实测 8 张图后 `creditUsagePercent` 恒 0、`productUsage` 恒 `[]`。CLI 账本（`/billing?format=credits`）同样只有 `GrokBuild`。`free-usage-gates` 的 `imagine` 恒 `0/0`。`image-generations` 生图后仍为 `[]`。**四条路径都不记生图用量。**
- **CLI 控制面**：`GET /billing?format=credits`（WEEKLY 7 天账期）、`GET /user?include=subscription`（不发 `x-userid`）、`GET /settings`、`GET /auto-topup-rule`。语义：`creditUsagePercent` **缺失 ≠ 0%**；proto3 把零值 cents 编码成 `{}`，须解析为 `val=0`。
- **版本门控（真实 bug 修复）**：早先硬编码 `x-grok-client-version: 1.0.1`，上游要求 **1.0.13** —— 实测 `1.0.1` → **HTTP 426** `Your Grok CLI version (1.0.1) is outdated`，`1.0.13` / `1.0.62` → 200。**控制面端点不做版本门控**，只有生成端点做。新增 `version_outdated()` / `required_client_version()`。
- **上游错误 → 冷却映射**（对齐 codex2api `applyGrokCooldown`）：免费额度耗尽（滚动 24h 窗口）→ 24h；超支 → 24h；**裸 402 证据不足不硬隔离**；429 → `Retry-After` 或 60s、**上限 900s**；401 → 60s + 异步刷新；403 → 300s 且**绝不刷新 token**；426 → 60s。**先看错误体再看状态码** —— 免费额度耗尽可能以 429 或 402 出现。
- **密集重试会加剧限流**：配额耗尽后按 3 分钟间隔重试，前 4 次静默降级，第 5 次起升级为 429。每次重试都计入窗口，应静默等待而非周期探测。
- **图片与视频共享同一预算桶**：一次 `textToVideo` 成功后再发图片立刻 429。

### 代码执行

`POST /rest/app-chat/run-code` **真的执行代码**。`language` 只认 `python`/`python3`（CPython 3.12.3）、`bash`、`javascript`（Node v24.15.0）；`sh`/`node`/`sql`/`ruby` 一律内部错误。沙箱：`Linux-4.19.0-gvisor`（**gVisor**）、`uid 0`、2 CPU、`cwd /home/workdir`、**DNS 通但 TCP 拒绝（无网络出口）**。可用库 `numpy pandas requests PIL bs4 lxml scipy matplotlib torch httpx yaml`。

### 端点权限分布（实测）

- `/rest/media/*` 89 个：9×200、**20×400（授权通过只是参数不对，同样可用）**、60×403。
- `/rest/app-chat/*` 42 个：**14×200**、27×400/500、仅 1×403。
- **`/rest/media/post/list` 不能用来数自己的生图** —— 返回公共 feed，穷举 18 种 `filter` 形态全部被服务端忽略。
- 权限是**端点级**的：`oauth2-auth-forbidden`（已鉴权但策略拒绝）与 `bad-credentials`（token 校验失败）是两个不同错误，不可混为一谈。

### 模型目录与提示词增强

- **`POST /rest/models`**（GET 返回 501）返回 **33 个模型**。单条 schema：`{modelId, name, description, modeDescription, modeName, badgeText, tags[], modelMode, promptingBackend}`。例 `build`：`badgeText: "Beta"`、`tags: ["hide_tools_settings","computer"]`、`modelMode: "MODEL_MODE_BUILD"`、`promptingBackend: "CHAT"`。顶层另有 `defaultAnonModel`/`defaultFreeModel` = `grok-3`、`defaultHeavyModel`/`defaultProModel` = `grok-4`、`unavailableModels: []`。
- **目录里的媒体模型只有 5 个**：`build`、`imagine-agent-mode`、`imagine-agent-mode-dev`、`imagine-agent-mode-grok-4-5`、`imagine-video-prompt-enhance`。**`imagine-image-gen` / `imagine-image-edit` / `imagine-video-gen` 不在目录里** —— 它们是前端 image studio 直连的隐藏模型名，只能从 bundle 解出。
- **`imagine-video-prompt-enhance` 是纯文本模型，不消耗媒体配额**。实测所有生图模型都 429 时它仍返回 200，把 `"a cat walking in rain"` 扩写成一段详细的视频描述（`"A cat walks steadily through falling rain along a wet street or path. Raindrops glisten on its fur..."`）。新增 `enhance_prompt()`。
- **所有生图模型共享同一个配额桶**：`imagine-image-gen` / `build` / `imagine-agent-mode` / `imagine-agent-mode-grok-4-5` 在配额耗尽后**全部** 429。此前观察到的「image-gen 成功、build 429、agent-mode 又成功」是**秒级请求限流**，不是独立配额桶。

### 语音合成（TTS）

- **`POST /rest/app-chat/text-to-speech` 可用**，产物是真实 MP3。响应是 **NDJSON**：每行 `{"result":{"data":"<base64>"}}`，全部拼接后 base64 解码才是完整音频（`//PExA` → `0xFF 0xF3` MPEG 帧同步字）。
- 实测产物经 ffprobe 确认：**`codec=mp3 / sample_rate=24000 / channels=1(mono) / bit_rate=128kbps / duration=2.616s`**，5 个分块共 41,856 字节。
- 请求字段全集：`{text, voiceId, language, codec, sampleRate, bitRate, optimizeStreamingLatency, textNormalization, speed}`。
- `GET /rest/app-chat/tts-voices` → `{voices:[{voiceId,name,language,gender}]}`（含 `altair` / `ara` 等）。**该端点用 SSO cookie 可读，CLI Bearer 可能 403** —— 与其它 `/rest/app-chat/*` 端点相反。
- 新增 `text_to_speech()` / `tts_voices()` / `_audio_format()` 与 `TTS_PATH` / `TTS_VOICES_PATH` / `TTS_FIELDS` 常量。

### Imagine 模板库

- **20 个模板，全部 `creditCost: 0`（免费）**，16 个 image + 4 个 video。`POST /rest/media/pipeline/template/list` → `{templates[], summaries[]}`；`template-folder/list` → 4 个分类；`template/get` → `{summary, isSaved}`。
- `summary` 含 `{id, name, description, thumbnailUrl, inputs[], publishedVersion, userId, runCount, creditCost, createdAt, primaryOutputMediaType, folders[]}`。**`inputs[]` 是 typed**：`inputType` 取 `image` / `text`，`required` 标明必填。最热门的是 **Quality Enhancer**（1,053,857 runs），其次 Chibi（951,339）、Professional Headshot（785,483）。
- **两个限制**：① `template/get` **只返回 `summary`，不含实际提示词**（提示词与流水线定义在服务端）；② **`POST /rest/media/pipeline/run` 实测 403 `oauth2-auth-forbidden`** —— 模板能列能读，**不能用 API 跑**，只能从网页 UI 用。
- 新增 `templates()` / `template_folders()` / `template_detail()` 与 `TEMPLATE_LIST_PATH` / `TEMPLATE_FOLDER_LIST_PATH` / `TEMPLATE_GET_PATH` / `PIPELINE_RUN_PATH` / `TEMPLATE_FIELDS` / `CATALOG_TEMPLATES` 常量。

### 媒体配额：实测数字（第三版修订）

- **`silent_degrade` 有两个成因**：① **请求体不完整**（缺 `disableSearch` / `isReasoning` / `modelMode` 任一，或缺 `x-xai-request-id` 头）→ 200 + 无图 + 正文声称"已生成"；② **真配额耗尽**（完整 body 连发后）。**排查必须先确认走的是 `build_body`。**
- **实测配额**（完整 body、`imagine-image-gen`）：`28hwsf4ag4`（干净、>3.5h 无活动）→ **8 次请求**（每次返 2 张，共 16 张）后 429；`2q7vuj955a`（紧接着）→ **仅 3 次**（6 张）后 429；`d781n9ynmc`（被密集重试过）→ 5 小时后仍 429。注意 **`numOfImages: 1` 时服务端仍返 2 张**。
- **★★★ 找到真正的配额端点：`POST /rest/media/imagine/quota_info`**（2026-10-21）。**必须用 SSO cookie**（`Cookie: sso=<token>`），CLI 签发的 Bearer 一律 403 —— 这是之前一直读不到配额的原因。实测响应：`{"image":null,"imagePro":{"available":true,"remainingQueries":4,"windowSizeSeconds":86400},"imageEdit":null,"video":null,"video720p":{"available":true,"remainingQueries":1,"windowSizeSeconds":86400}}`。
- **★ 窗口是 86400 秒 = 24 小时。** 这一条解释了此前所有矛盾：为什么 3.5 小时后仍未恢复；为什么 bundle 里 `formatAvailableAt` 的「+30 分钟」对不上（那是 **credit-quota** 的显示逻辑，不是 media quota）。实测消耗轨迹 `imagePro` 从 **4 → 1**（3 次生图请求）。
- **桶的划分**：`image` / `imagePro` / `imageEdit` / `video` / `video720p`。实测 `image` / `imageEdit` / `video` 为 **null**，只有 `imagePro` 与 `video720p` 有数据 —— 当前账号走 Pro 桶。**`imageEdit` 为 null 说明图生图不单独计费，消耗 `imagePro`。**
- **配额按账号计量，与出口 IP 无关**（决定性实验）：用每次换 IP 的轮换住宅代理，让已 429 的账号连发 10 次（每次不同出口 IP）→ **0 张图**（7×429、2×Cloudflare challenge、1×TLS 抖动）。代理的 9 种请求头（`X-Proxy-Session` / `Proxy-Session` / `X-Sticky-Session` / `X-Session` / `X-Rotate` / `X-Exit-Country` / `X-Proxy-Url` / `Proxy-Url` / `X-Proxy`）全部被忽略，无法固定 IP。
- **已排除的旁路**：`template-chat` → 403；`pipeline/run` → 403；**`POST /rest/rate-limits` 不是配额端点**（GET 501 / POST 404 "Model not found."）；刷新 token 不重置；换生图模型无效（共享同一桶）；换 IP 无效；429 响应**无 `Retry-After`**。
- **其它打通的用量端点（均需 SSO cookie）**：`GET /rest/media/imagine/generation_estimate`、`POST /rest/models/imagine/overrides`（→ `{"modelMapOverrides":[]}`）、`GET /rest/usage/free-usage-gates`（chat/imagine/voice/build 四组 allowance+remaining）、`GET /rest/tasks/usage`（frequent 0/2、occasional 0/10）、`GET /rest/assets/storage-usage`（**总配额 1 GiB**）、`POST /rest/media/reference/list`（→ `{"references":[]}`）。
- **完整 API 表面**：从 1367 个 chunk（60 MB）静态提取 **250 个端点**，全量探测分布 **403×178 / 400×11 / 200×8 / 404×5 / 401×4**。**400 ≠ 403** —— 400 说明授权通过、只是参数不对，同样可用。清单见 `docs/grok-web-protocol.md` §1.20。
- 新增 `quota_info()` / `remaining_media()` 与 `QUOTA_INFO_PATH` / `GENERATION_ESTIMATE_PATH` / `IMAGINE_OVERRIDES_PATH` / `MEDIA_QUOTA_BUCKETS` 常量；`download_image()` 新增 `cookies` 参数（SSO 也能下载）。
- **账号级惩罚确实存在**：`d781n9ynmc` 被按 3 分钟间隔重试 8 次（第 5 次起转 429）后 **5 小时仍未恢复**，而同期从未被密集重试的 `28hwsf4ag4` 能拿满 8 次。**命中 429 后不要周期重试** —— 会触发更长的惩罚，应标为冷却并静默足够久。
- **未验证**：正常配额的重置窗口时长。bundle 的 `formatAvailableAt` 是 `nextAvailableAt + 30 分钟`，但实测 3.5 小时未恢复（可能被惩罚期掩盖），且 30 分钟后三账号全部仍 429。
- 备用出口实测可用：`socks5h://research:***@192.168.2.198:2260` 能到 `grok.com`（200 / 33 个模型）与 `auth.x.ai`（刷新 200），但**轮换 IP 会触发 Cloudflare challenge**，且对配额无帮助。
- **不消耗媒体配额**：`imagine-video-prompt-enhance`、`run-code`、`text-to-speech`、模板只读接口。
- 新增 `refresh_access_token()`（含 `OAUTH_TOKEN_URL` / `OAUTH_CLIENT_ID` / `OAUTH_SCOPES` 常量）：实测 `grant_type=refresh_token` 可用，返回新 token（`expires_in=21600` = 6h）；**失效 refresh token 返回 HTTP 400 + `invalid_grant`**（永久性错误，重试无用）。刷新**不重置**媒体配额。

### 生图失败的五种形态（2026-10-22 补全）

- 判定生图成功**只能看** `cardAttachment.image_chunk.imageUrl`。HTTP 200、正文声称"已生成"、agent 说"这是结果" —— **全都不可信**。五种形态：① 请求层 429；② 流内 `renderToolRateLimited` + `systemErrCode 1006`；③ **请求体不完整**（正文 `"I generated an image with the prompt: ..."`）；④ **agent 幻觉**（正文 `"Here's the apple recolored to bright green, ... preserved:"`，冒号后什么都没有）；⑤ **模型报错**（`"Oops, something broke. Talk to me later?"`）。
- **形态 ④ 最危险**：`imagine-agent-mode` 在配额耗尽时用自然语言描述一张**并不存在的图**。抓完整 170 帧确认：`assets.grok.com` 出现 **0 次**、`cardAttachment` **0 次**，只有 `modelResponse`。新增 `claims_success_without_image()` / `model_broken()` / `quota_exhausted_hint()` 用于自动识别。
- 配额耗尽时 agent 追问后才明说：`"It looks like you've hit the image generation limit for this session, so I can't create or edit images right now."` —— 注意 "for this session" 的措辞，但换新会话（`temporary: true`）无效，仍是账号级配额。
- **`imagine-image-edit` 疑似不可用**：无论 body 形状一律返回 `"Oops, something broke. Talk to me later?"`，而同一时刻 `imagine-image-gen` / `imagine-agent-mode` 都能出图 —— **不是配额，是该模型自身报错**。图生图应改用 `mediaGenInput.imageToImage`。
- **`build` 有独立且更紧的限流**：`build` 429 时 `imagine-image-gen` 仍能出图 —— 与「所有生图模型共享同一桶」的旧结论**部分冲突**，已在文档标注。

### 图生图：`mediaGenInput.imageToImage`

- 正确形状：`modelName`（`build` / `imagine-image-gen` / `imagine-agent-mode`）+ `kind: CONVERSATION_KIND_IMAGINE` + `mediaGenInput.imageToImage{prompt, inputAssets[], numOfImages, aspectRatio}`。字段全集含 `resolutionName` / `modelName` / `selectionRegions` / `mediaReferences`。**`inputAssets` 与 `mediaReferences` 是两个独立字段**。
- **★ 不需要 `upload-file`**：生成的图本身就是服务端资产，URL 结构 `https://assets.grok.com/users/<uid>/generated/<imageId>/image.jpg`，把 `assets.grok.com/` 之后的部分直接作 `inputAssets` 即可。这绕开了 `upload-file` 的权限限制。
- **`upload-file` 只认 SSO cookie**：Bearer → 403 `oauth2-auth-forbidden`。带 SSO 时 body 必须 **raw base64**（data URL 会 400），返回 `fileUri: users/<uid>/<fileMetadataId>/content`（**不是文件路径**）。`fileSource` 枚举 `SELF_UPLOAD_FILE_SOURCE` / `IMAGINE_SELF_UPLOAD_FILE_SOURCE`。
- 图片资产完整字段：`{kind: "stream", image_url, asset_id, image_id, progress}`，`asset_id` 与 `image_id` 相同。
- **验证进度**：`build` + `imageToImage` + 资产 key → 模型**确实读到了图**（为 "make it green" 的歧义要求澄清）。换明确 prompt 后撞配额耗尽，**最终产物尚未取得**。

### 完整错误分类法（从 bundle 提取，权威）

- 前端模块 `962117` / `4150825` 里有一套**固定优先级**的错误判定 `F(e)`，是服务端错误语义的唯一权威来源 —— **比按状态码猜准确**，因为配额耗尽时 HTTP 仍是 200。
- **20 种 error kind**（按优先级）：`beyondContextLength` / `userPromptTooLong` / `modelNotAvailable` / `globalRateLimit` / `concurrencyLimit` / `systemKillSwitch` / 三个 snake_case 变体 / `usagePoolExhausted` / `usageLimitReached` / `connectorAuthFailed` / `attachmentModerated` / `attachmentImageTooLarge` / `attachmentFileTooLarge` / `attachmentTooManyPages` / `attachmentLoadFailed` / `renderToolFailed` / `renderToolRateLimited` / `renderPerMessageLimitExceeded` / 正则兜底。
- **只有 `beyondContextLength` 是 fatal**（`D = new Set(["beyondContextLength"])`），外加 `severity === "STREAM_ERROR_SEVERITY_FATAL"`。
- **★ 空响应的官方判据**（前端 `B(e)`，即 silent_degrade）：`!text.trim() && cardAttachments.length===0 && cardAttachmentsJson.length===0 && !error && usageLimitKind==null && !concurrencyLimited && !rateLimited`。**四个都空才算空响应。** 这解释了为什么「请求体不完整」会表现为静默降级 —— 服务端回了正文，`text.trim()` 非空，前端不会判空。
- **配额耗尽有两种语义**：`usage_pool_exhausted`（**共享池**，团队级）与 `usage_limit_reached`（**个人上限**），冷却策略应不同。各可由 `errCode` / `failureReason`（`PIPELINE_FAILURE_REASON_*`）/ `typedError` / `message.startsWith` 四种字段表达。
- 新增 `stream_error_kind()` / `is_fatal_stream_error()` / `is_empty_response()` 与 `STREAM_ERROR_KINDS` / `FATAL_STREAM_KINDS` / `USAGE_EXHAUSTED_KINDS` / `CONTEXT_LIMIT_RE` / `NEW_CONVERSATION_TITLE` / `IMAGINE_AGENT_MODEL_ID` 常量。

### 凭据体系与请求头（JWT 全量实测）

- **Header**：`{"typ":"at+jwt","alg":"ES256","kid":"oauth2-production-2026-02-19"}`。`access_token` 是 `at+jwt`，`id_token` 是 `JWT`。
- **10 个账号逐一解码，claims 完全一致**：`aud == client_id == b1a00492-073a-47ea-816f-4c329264a828`，`exp = iat + 21600`（6h），`iss=https://auth.x.ai`，`sub == principal_id`，scope 恒为 8 项（含 `grok-cli:access`）。**只有 `team_id` / `principal_id` / `jti` / 时间戳不同。**
- **Grok 只有 1 个 client_id**（与 GPT 的 3 个不同）；bundle 里**搜不到任何 client_id** —— 网页走 SSO cookie，不走 OAuth。
- `id_token` 的 `family_name` / `given_name` 是注册时生成的假名（Smith / Martinez / Lee / Wilson / Perez / Rodriguez / Garcia / Davis / Thompson）。
- **凭证能力边界**：只读 + 生成认 **Bearer**；文件写入（`upload-file`）、模板执行（`template-chat` / `pipeline/run`）、配额（`quota_info`）、音色列表（`tts-voices`）认 **SSO cookie**。
- **`/rest/*` 实际必需头**只有 6 个：`Content-Type` / `x-xai-request-id`（uuid4，前端唯一显式设置的自定义头）/ `Accept` / `Origin` / `Referer` / `Authorization`（或 `Cookie: sso=`）。另有 `x-acting-organization-id` 仅在**有活跃组织时**注入（个人账号不带）。
- 遥测端点（`/v1/app_config`）的 15 个 `x-device-*` / `x-user-*` / `x-forwarded-*` 头**不用于 `/rest/*`**。常见误判：`x-statsig-id` 在 1367 个 chunk 里**无此字符串**；`x-app-language` / `x-app-timezone` 是 WebSocket query 参数不是 HTTP 头。

### ★★ 图生图完整打通 + 上传改走 v2（2026-10-22）

- **图生图已完整验证并产出图片**：v2 上传源图（832×1248 JPEG, 269,995 B）→ `modelName: "imagine-image-gen"` + `mediaGenInput.imageToImage{prompt, inputAssets:[fileUri], numOfImages:1, aspectRatio:"1:1"}` → 200、images=2、无幻觉 → 下载得 768×1152 JPEG（21,631 B）。源图中心像素 `(219,180,149)` → 输出 `(129,131,82)`（偏绿），模型确实读了输入并执行指令。**消耗 `imagePro` 1 次**（`remainingQueries` 1 → 0）。
- **`imagine-image-edit` 不要用** —— 稳定返回 `"Oops, something broke. Talk to me later?"`。`build` 可用但在部分账号上有更紧的独立限流。
- **★ v1 `upload-file` 已不可靠，改用 v2 三段式**：同一张图同一账号，00:11 返回 200，05:20 起稳定 `400 Cannot open given image [WKE=file:parse-failed]` —— **与格式/尺寸/大小无关**（8×8、259 字节的干净 PNG 也失败），且 v1 对 Bearer 一律 403。
- **v2 流程**：`POST /rest/app-chat/upload-file-v2/init` → `{uploadId, assetId, singlePut:{url, requiredHeaders}}` → `PUT <url>` **必须原样带 `requiredHeaders`** → `POST .../complete` `{presigned, completedParts:[], mode:"UPLOAD_FILE_COMPLETE_MODE_SINGLE_PUT"}` → `fileMetadata.fileUri = users/<uid>/<assetId>/content`。
- **PUT 的坑**：预签名 URL 的签名头实测为 `content-type;host;x-amz-meta-upload-id`，其中 `x-amz-meta-upload-id` 就是 init 返回的 `uploadId`。漏掉会被 GCS 拒为 `MalformedSecurityHeader`。存储桶 `storage.googleapis.com/xai-enterprise-api-mc-assets-prod/`，签名服务账号 `grok-backend-production@enterprise-api-425510.iam.gserviceaccount.com`，预签名有效期 `X-Goog-Expires: 3600`。
- 新增 `upload_file_v2()` 与 `UPLOAD_V2_INIT_PATH` / `UPLOAD_V2_COMPLETE_PATH` / `UPLOAD_V2_PATCH_PATH` / `UPLOAD_V2_ABORT_PATH` / `UPLOAD_V2_STATUS_PATH` / `UPLOAD_V2_MODES` / `UPLOAD_V2_SOURCE_IMAGINE` / `UPLOAD_V2_SOURCE_SELF` 常量。

### ★★★ 风控拆解：账号是怎么被"拉黑"的（远端 8000 条日志实证）

- 数据源：远端 `http://192.168.2.198:3000/api/logs`，拉取 **8000 条**（2026-10-13 19:11 → 10-22 17:34；`account` 7472 / `call` 528）。
- **不是"拉黑"，是三层递进吊销**：① `session_refresh_stale_token_revoked` **989 次**；② `token invalidated (/backend-api/me)` **867 次**；③ `password_verify_failed_401` **23 次** → `code: "password_reset_required"` —— **这才是真正的锁定码**，响应体 `"For improved security please change your password and then log in again."`。
- **触发链**：refresh token 失效 → 系统 fallback 到**密码登录** → 密码登录从与注册时不同的环境（不同 IP / TLS 指纹 / 无原始 cookie）发起 → 服务端判定可疑 → 要求改密码。**反复 fallback 会加重**：一个账号被 `remote_probe` 探测 **112 次**，状态在 `session_refresh_token_still_invalid` ↔ `password_verify_failed_401` 之间反复横跳近 6 小时。
- **`failure_kind` 分布**：`token_revoked` 989 / `http_auth` 869 / `unknown` 330 / `tls` 106 / `proxy_connect` 76 / `timeout` 17。
- **`source` 分布**：**`remote_probe` 1147 次是第一大 refresh 触发源**（超过正常调用），覆盖 **928 个不同账号**；其后 `backend_session_refresh` 1013、`refresh_accounts` 722、`image_stream:auth_failed` 232。
- **网络失败会放大吊销**：451 条网络失败（`curl(35)` TLS 246 / `curl(97)` SOCKS 161 / `curl(28)` 超时 37），**25% 的 `token_revoked` 在发生前 5 分钟内有网络失败**。
- **吊销的小时分布是平的**（每小时稳定 10–16 次，无突发尖峰）—— 说明这是**结构性损耗**，不是某次操作触发封禁。
- **内容政策拦截与吊销是两件事**：34 次裸露/色情拦截、24 次提示词拦截、23 次内容政策、6 次欺诈拦截，都只是**单次请求被拒**，账号仍健康，不消耗寿命。
- 新增 `password_reset_required()` / `account_revoked()` / `should_fallback_to_password()` 与 `PASSWORD_RESET_REQUIRED` / `ACCOUNT_REVOKED_MARKERS` / `NO_PASSWORD_FALLBACK_MARKERS` 常量。**`should_fallback_to_password()` 的核心结论：鉴权类错误永远不该 fallback 到密码登录**，只有传输层错误（TLS/SOCKS/超时）才值得重试。

### ★★★ 认证协议逆向：风控的两层结构（2026-10-22）

- **登录 body 的确切形状（40+ 次爆破得出）**：`{"credentials":{"emailAndPassword":{"email":"...","password":"..."}}}`。命中后错误从 `"Credentials must be provided."` 变成 `403 {"code":7,"message":"Anti-abuse token is missing. [WKE=anti-abuse:token-missing]"}`。关键观察：**未知字段被静默忽略**（顶层 `{"bogusFieldXyz":1}` 也回基线错误），只能靠「错误是否变化」定位；`credentials` 传非对象会得到 `proto: syntax error (line 1:17)`，确认它是 message 字段。
- **风控第一层：Cloudflare Turnstile** —— sitekey `0x4AAAAAABSXp6OOe7EVGPnR`，`POST /api/verify-turnstile {token}` → `{success}`。密码登录缺令牌即 403。
- **风控第二层：Castle（第三方反欺诈引擎）** —— `castle-provider` 通过 `createRequestToken({method})` 铸造，`method ∈ {auth, chat, commerce}`。**令牌 TTL 60 秒**（`Date.now()+6e4`）；超时预算前台 `init 5s / mint 4s`、后台 `init 10s / mint 10s`；失败原因 `mint_error` / `castle-token-timeout` / `unmatched-castle-token-result`；错误类 `CastleTokenError`；遥测 `castle_mint` / `castle_mint_failed`。**Chat 请求也铸 Castle 令牌** —— 发消息同样在被打分。
- **完整错误码表 66 个 / 9 个命名空间**。真正的"拉黑"是 **`unauthorized:blocked-user`**，官方文案 **`"Your account has been blocked."`**。`unauthorized:*` 占 29 个（含 `banned-organization` / `x-user-suspended` / `deleted-user` / `oauth2-auth-forbidden`）。
- **★ 一次性邮箱被封**：`form-invalid:disposable-email` —— 这是号池大规模死亡的直接原因之一，号池全是临时邮箱域名（`awa91.cyou` / `5122331.xyz` / `cyt233.dpdns.org` 等）。
- **登录方式枚举 13 种**：`EMAIL_PASSWORD=1` / `GOOGLE_OAUTH2=2` / `X_OAUTH2=3` / `APPLE_OAUTH2=4` / `SSO=5` / `KEYCLOAK_OIDC=6` / `SPACEX_OIDC=7` / `GITHUB_OAUTH2=8` / `EMAIL_CODE=9` / `CURSOR_PARTNER=10` / `CURSOR_SIGN_IN_LINK=11` / `ANDROID_RESTORE_KEY=12`。
- **8 个终态错误**（命中即丢弃账号）：`account:password-reset-login-required` / `unauthorized:blocked-user` / `blocked-user-cursor-enterprise` / `blocked-user-underage` / `banned-organization` / `deleted-user` / `x-user-suspended` / `form-invalid:disposable-email`。
- 实测可达的认证端点：`POST /rest/auth/create-session-v2` / `create-session`，`GET /rest/auth/get-user` / `list-teams` / `get-user-feature-controls`（→ `{"allowNsfwContent":true}`）/ `list-mfa-devices` / `get-auth-status`。
- 新增 `auth_error_code()` / `is_terminal_auth_error()` 与 `AUTH_ERROR_CODES`（66 条）/ `AUTH_ERROR_MESSAGES` / `TERMINAL_AUTH_CODES` / `CASTLE_*` / `TURNSTILE_*` / `AUTH_METHODS` / `AUTH_*_PATH` 常量。

### `/api/verify-turnstile` 完整行为 + 两个未解项（2026-10-22）

- **`POST /api/verify-turnstile`** 是 grok.com 自己的 Next.js API 路由，把 token 转给 Cloudflare `siteverify`。字段名就是 **`token`**。响应矩阵：`{}` → 400 `Missing or invalid Turnstile token`；`{"token":""}` → 400；`{"turnstileToken":"x"}` → 400（字段名不对）；`{"token":"x"}` → **403 `Invalid Turnstile token`**；`GET` → 405。**400 vs 403 是关键**：400 是「没给或给错字段」，403 说明服务端真的去 Cloudflare 校验了并失败。
- **★★ 未解：`create-session-v2` 的反滥用令牌字段名。** 已穷举约 **300 个候选**：260 个 body 字段名（20 前缀 × 13 后缀，分别在顶层与 `credentials` 内层）、14 个请求头、12 种嵌套 message 形状，全部返回同一个 `403 anti-abuse:token-missing`。**结论：无法靠盲猜得出**，只能抓真实浏览器的登录请求；Castle 令牌由浏览器端 JS 铸造，静态 bundle 里没有明文。
- **一个差点误判的陷阱**（已记录）：爆破中 `turnstileResponse` 内层连续 3 次返回 Cloudflare 质询页 `"Just a moment..."`，看起来像「字段对了、触发了更深层检查」。冷却后做对照 —— 基线正常、内层仍 CF、顶层正常 —— 证明那是**时间相关的边缘限流，与字段无关**。
- **★ 反向发现：密集 auth 请求会触发 Cloudflare WAF。** 260 候选 × 2 形状、间隔 0.15s 的爆破连续收到 `<title>Just a moment...</title>` 挑战页。认证端点前面还有一层 Cloudflare WAF，对短时间内的重复 POST **独立于应用层限流**生效 —— 与 §1.24 的「密集重试 → 更长惩罚」是同一类机制的两层。
- 新增 `verify_turnstile()` 与 `TURNSTILE_VERIFY_URL` 常量。

### 新增 API

`services/grok_web_api.py` 新增：`download_image` / `free_usage_gates` / `imagine_quota` / `cli_rate_limits` / `billing` / `subscription` / `client_settings` / `auto_topup_rule` / `classify_upstream_error` / `clamp_rate_limit_cooldown` / `version_outdated` / `required_client_version` / `web_credits` / `product_name` / `usage_poll_interval_ms` / `crossed_usage_threshold` / `run_code` / `image_generations` / `generate_video` / `upload_file` / `diagnose_media_limit` / `media_gen_kind` / `build_text_to_video` / `build_image_gen_config` / `build_image_edit_config`，以及 `image_rate_limited` / `silent_degrade` 判定。

## 回归测试

## 清障行为与验证范围

## 本地验证

测试包将 `CHATGPT2API_DATA_DIR` 和 `CHATGPT2API_CONFIG_FILE` 指向临时目录；网络命名空间仅启用回环接口，供本地模拟服务使用，外部网络不可达。

```bash
unshare --net bash -c 'ip link set lo up && exec .venv/bin/python -m pytest -q --tb=short'
```

全套结果：1261 passed、17 skipped；另有 101 项 subtests 通过。跳过项依赖真实服务。两项已有警告涉及 SQLAlchemy 弃用接口及安全 Cookie 属性。前端类型检查 `web/node_modules/.bin/tsc --noEmit --incremental false -p web/tsconfig.json` 通过。

验证前后，现有 `config.json`、`VERSION` 和 `data/` 内 JSON/版本文件的内容哈希应保持不变。发布前请确认没有未提交的密钥、没有更新发布标签。
