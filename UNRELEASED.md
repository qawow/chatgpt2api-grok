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

### 新增 API

`services/grok_web_api.py` 新增：`download_image` / `free_usage_gates` / `imagine_quota` / `cli_rate_limits` / `billing` / `subscription` / `client_settings` / `auto_topup_rule` / `classify_upstream_error` / `clamp_rate_limit_cooldown` / `version_outdated` / `required_client_version` / `web_credits` / `product_name` / `usage_poll_interval_ms` / `crossed_usage_threshold` / `run_code` / `image_generations` / `generate_video` / `upload_file` / `diagnose_media_limit` / `media_gen_kind` / `build_text_to_video` / `build_image_gen_config` / `build_image_edit_config`，以及 `image_rate_limited` / `silent_degrade` 判定。

## 回归测试

## 清障行为与验证范围

## 本地验证

测试包将 `CHATGPT2API_DATA_DIR` 和 `CHATGPT2API_CONFIG_FILE` 指向临时目录；网络命名空间仅启用回环接口，供本地模拟服务使用，外部网络不可达。

```bash
unshare --net bash -c 'ip link set lo up && exec .venv/bin/python -m pytest -q --tb=short'
```

全套结果：1210 passed、17 skipped；另有 101 项 subtests 通过。跳过项依赖真实服务。两项已有警告涉及 SQLAlchemy 弃用接口及安全 Cookie 属性。前端类型检查 `web/node_modules/.bin/tsc --noEmit --incremental false -p web/tsconfig.json` 通过。

验证前后，现有 `config.json`、`VERSION` 和 `data/` 内 JSON/版本文件的内容哈希应保持不变。发布前请确认没有未提交的密钥、没有更新发布标签。
