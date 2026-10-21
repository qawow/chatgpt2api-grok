# grok.com 网页协议逆向

面向「把 grok.com 网页对话当搜索用」和「调用网页画图」两个目标。

**逆向对象**：`https://grok.com/` 前端 bundle，抓取于 2026-10-19
**素材**：`index.html`（394,805 B）+ **1367 个 JS chunk（60 MB）** + 6 个 CSS
**方法**：静态逆向（Turbopack 构建，OpenAPI 生成客户端保留函数名，可直接检索）

> **验证状态**：本文件全部结论来自 bundle 原文，**未经实况抓包验证** ——
> 本环境没有可用的 grok.com 凭证（远端唯一账号的 `access_token` 已过期 85 天，
> `refresh_token` 返回 `invalid_grant`）。字段名可信度高（OpenAPI 类型定义），
> 但**服务端实际接受/返回的字段集**必须用有效凭证跑一次才能确认。
> 详见 [§9 验证状态](#9-验证状态)。

---

## 1. 三条最容易踩错的结论

### 1.0 实况验证结果（2026-10-19，三个真实凭证）

用三个有效 xAI 凭证做了端到端实测，**推翻/确认了静态逆向的几处结论**：

| 结论 | 状态 |
|---|---|
| 网页路径用 **Cookie** 认证 | ❌ **错** —— 实测 `Authorization: Bearer <access_token>` **可用**（`GET /rest/app-chat/conversations` → 200）；所有 cookie 名变体（`sso`/`sso-rw`/`token`/`access_token`/`__Secure-next-auth.session-token`）全部 401 |
| 流是 NDJSON 不是 SSE | ✅ 确认（网页路径） |
| 网页路径可发消息 | ❌ **被模型可用性挡住** —— 见 §1.4 |
| CLI 路径工具（搜索/画图）可用 | ❌ **被拒** —— 见 §1.3 |

**凭证形态**（三个都有效，scope 含 `conversations:read conversations:write`）：

```
openid profile email offline_access grok-cli:access api:access
conversations:read conversations:write
```

### 1.1 流是 NDJSON，不是 SSE

这是实现时第一个会踩的坑。`js/0__nwepjsaz6y.js`（模块 `180418`）原文：

```js
async function* n(e) {
  if (null === e.body) throw Error("Cannot process response chunks of a response without body.");
  let t = e.body.getReader(), n = new TextDecoder, i = "";
  for (;;) {
    let { done: e, value: a } = await t.read();
    if (e) break;
    let [o, s] = r(i += n.decode(a, { stream: true }));
    for (let e of o) yield e;
    i = s
  }
  let [a, o] = r(i);
  for (let e of a) yield e;
  o.length > 0 && (yield o)
}

function r(e) { let t = e.split("\n"), n = t.pop(); return [t, n] }

async function* i(e) {
  for await (let r of n(e))
    try { let n = JSON.parse(r); (0, t.recordStreamError)(n, e), yield n }
    catch (n) { (0, t.recordStreamError)(n, e) }
}
e.s(["processChunks", 0, i])
```

**按 `\n` 切分，每行直接 `JSON.parse`。没有 `data:` 前缀、没有 `event:` 字段。**

用 SSE 解析器会失败 —— `data: ` 剥离逻辑会把 JSON 首字符吃掉。
解析失败的行被**静默跳过**（`catch` 只上报不中断），所以坏行不会炸掉整个流。

### 1.2 没有独立的网页搜索端点

`rest_paths.txt` 里 10 个含 `search` 的路径**全是**媒体库 / 技能 / 语音搜索：

```
/rest/media/search/{debug,query,status,trigger}    ← 媒体库
/rest/sharing/search
/rest/user-skills/search
/rest/verified-skills/search
/rest/voice/search
```

**网页搜索完全内嵌在对话流里** —— 发一条消息、开 `webSearch` 工具，结果随流返回。

### 1.3 认证是纯 Cookie，对话路径没有 `Authorization`

> ⚠️ **这一条被实况验证推翻了** —— 见 §1.0。网页路径**接受 OAuth Bearer**。
> 下面的静态逆向结论保留作为对照（前端确实只靠 `credentials:"include"` 带 Cookie，
> 但服务端同时接受标准 Bearer 头）。

bundle 里的 `Bearer` 只属 next-axiom 日志库、`Basic` 属 Mixpanel、
`DPoP` 只挂 `auth_mgmt.*` 的 10 个 ConnectRPC 方法（flag `grok_dpop_enforce_enabled`，默认空集）。

`x-statsig-id` **全 bundle 无此字符串**（`rg -ci statsig` 只命中 survey 名与 Sentry `denyUrls`）。

### 1.4 实况：三条路径的可用性矩阵

> **2026-10-19 更新：网页生图已打通** —— 见 §1.7。模型名必须是 `build`。

| 路径 | 端点 | 认证 | 文本 | 搜索 | 画图 |
|---|---|---|---|---|---|
| **CLI/Build** | `cli-chat-proxy.grok.com/v1` | Bearer ✅ | ✅ 200 | ❌ 工具被接受但不执行 | ❌ 同上 |
| **官方 API** | `api.x.ai/v1` | Bearer ✅ | ❌ 429 配额 0/0 | ❌ 同 | ❌ 同 |
| **网页** | `grok.com/rest/app-chat/*` | Bearer ✅ | ⚠️ 仅特定模型 | ⚠️ 待验证 | ✅ **可用**（`build`） |

#### CLI 路径：工具「被接受但不执行」

`POST /v1/responses` + `tools:[{"type":"web_search"}]` → **200**，但服务端权威报告：

```json
"num_sources_used": 0,
"num_server_side_tools_used": 0
```

模型回复「I don't have real-time web access in this context」/「I can't draw images directly」，
`output` 里只有 `reasoning` + `message`，**没有 `web_search_call` / `image_generation_call`**。
`image_generation` 同理 —— 仓库自己的 `generate_image()` 也返回
`Grok 未生成图片（账号正常，模型没有调用图片工具）`。

工具类型全集（来自 422 报错的枚举提示）：

```
function, web_search, x_search, image_generation,
collections_search, file_search, code_execution, code_interpreter, mcp, shell, tool_search
```

`search_parameters` 返回 **410 Gone**：

```json
{"error":"Live search is deprecated. Please switch to the Agent Tools API:
 https://docs.x.ai/docs/guides/tools/overview"}
```

**结论：工具执行是订阅/团队级能力，这三个凭证没有。**

#### 官方 API：认证通过但零配额

`api.x.ai/v1/models` → 200，目录 14 个模型（`grok-4.20-*` / `grok-4.7` / `grok-imagine-image*` / …）。
但任何 `POST` 都返回：

```json
{"code":"resource-exhausted",
 "error":"Too many requests for team <id> and model grok-4.7.
          Your team's rate limit is — Requests per Minute (actual/limit): 0/0."}
```

`0/0` = 团队无官方 API 配额。**这不是限流，是没有订阅。**

#### 网页路径：认证通过，模型不可用

`POST /rest/app-chat/conversations/new` + Bearer → **200**，会话真的被创建
（返回 `conversationId`），用户消息被回显，然后：

```json
{"result":{"response":{"token":"Invalid request. Please update your client.",
                       "messageTag":"final","responseId":"..."}}}
{"result":{"response":{"error":{"message":"This model is temporarily unavailable.
                       Please try a different model.","modelNotAvailable":{},
                       "severity":"STREAM_ERROR_SEVERITY_NORMAL"}}}}
```

**`"Invalid request. Please update your client."` 是兜底文案**，真实原因是
`modelNotAvailable`。模型名分级实测：

| modelName | 结果 | 含义 |
|---|---|---|
| `grok-3` | 200 + `modelNotAvailable` | **通过名称校验**，但模型不可用 |
| `grok-420` | 429 `heavy usage` | 通过校验，被限流 |
| 其它 12 个（`grok-4.7` / `grok-4.5` / `grok-4.20-*` / `grok-build-0.1` / …） | 403 `Model is not found` | 名称无效 |

试过的所有变体（body 形态 10 种、额外头 6 种、两步 add-response 路径）都停在同一个
`modelNotAvailable`。

**但生图是例外** —— 用 `modelName: "build"` 就能出图（见 §1.7）。
即：OAuth2 token 用户在网页路径上**没有可用的对话模型，但有可用的生图模型**。

### 1.5 生图额度接口（2026-10-19 实测）

**三个额度端点，只有 CLI 那个对 OAuth2 用户可用。**

#### 权威端点：`POST /rest/media/imagine/quota_info`

bundle 提取（`js/29p4h4iryyz76.js`，`mediaGetImagineQuotaInfoRaw`）：

```js
// 请求
{path:"/rest/media/imagine/quota_info", method:"POST", body:{}}   // body 为空对象

// 响应 transformer
{image: et(t.image), imagePro: et(t.imagePro), imageEdit: et(t.imageEdit),
 video: et(t.video), video720p: et(t.video720p),
 maxVideoDurationSeconds: t.maxVideoDurationSeconds}
```

每个类别 **23 字段**（`et()` 原文提取）：

```jsonc
{
  "estimate": {
    "remainingImageGenFast1k": 0,      "remainingImageGenFast2k": 0,
    "remainingImageGenQuality1k": 0,   "remainingImageGenQuality2k": 0,
    "remainingImageEditQuality1k": 0,  "remainingImageEditQuality2k": 0,
    "remainingT2v480pSeconds": 0,      "remainingT2v720pSeconds": 0,
    "remainingT2v1080pSeconds": 0,
    "remainingI2v480pSeconds": 0,      "remainingI2v720pSeconds": 0,
    "remainingI2v1080pSeconds": 0,
    "remainingR2v480pSeconds": 0,      "remainingR2v720pSeconds": 0,
    "remainingR2v1080pSeconds": 0,
    "remainingExtension480pSeconds": 0, "remainingExtension720pSeconds": 0,
    "remainingExtension1080pSeconds": 0
  },
  "shouldShow": true,
  "imagineFreeCreditActive": true,
  "imagineFreeCreditAllowance": 10,
  "imagineFreeCreditRemaining": 7
}
```

字段语义：`ImageGen` = 文生图，`ImageEdit` = 图生图/编辑，
`Fast`/`Quality` = 速度档，`1k`/`2k` = 分辨率档；
`T2v`/`I2v`/`R2v` = text/image/reference-to-video；
`Extension` = 视频续接。视频类单位是**秒**。

客户端轮询逻辑（`js/2bbm9n6clh3_p.js` `fetchQuotas`）：
- 只在页面 visible 时轮询，间隔 **≥5 秒**（`lastFetchedAt` 节流）
- 生图完成后立即重取（`generationSeq` 变化触发）
- `LOW_GENERATION_THRESHOLD = 3` —— 剩余 ≤3 时弹低额度提示
- 401/403 时置 `authBlocked` 并停止轮询

**实测：403 `oauth2-auth-forbidden`** —— 该端点被服务端策略限定给网页会话，
OAuth2 token 用户查不到。但**生图本身可用**（§1.7）→ 额度是服务端强制、客户端不可查。

#### 免费层闸门：`GET /rest/usage/free-usage-gates`

```js
// 客户端解析（js/1nina2n46pgtw.js）
function o(e){ return void 0===e ? {allowance:0,remaining:0}
                : {allowance:n(e.allowance), remaining:n(e.remaining)} }
async function i(){ return {
  chat:    o((await rateLimitsApi.rateLimitsGetFreeUsageGates()).chat),
  imagine: o(e.imagine), voice: o(e.voice), build: o(e.build) } }
```

**实测：三个凭证全部 `0/0`，但生图可用** —— 这个闸门只对免费网页用户生效，
**不要用它判断能否生图**。

#### 唯一可用的：CLI 路径的限流响应头

```
POST https://cli-chat-proxy.grok.com/v1/responses
  → x-ratelimit-limit-requests: 21
    x-ratelimit-limit-tokens: 1000000
    x-ratelimit-remaining-requests: 21
    x-ratelimit-remaining-tokens: 1000000
```

**只有 `/responses` 带这些头** —— `/models` 不带（实测确认）。
窗口内会递减（连发时观察到 `16/21`），窗口重置后回到满值。

#### 其它额度相关端点（实测）

| 端点 | 结果 |
|---|---|
| `GET /rest/tasks/usage` | ✅ 200 `{"frequentUsage":0,"frequentLimit":2,"occasionalUsage":0,"occasionalLimit":10}` |
| `GET /rest/assets/storage-usage` | ✅ 200 `{"totalStorageBytes":"1073741824","usedStorageBytes":"0",...}`（1 GiB） |
| `GET /rest/subscriptions` | ✅ 200 `{"subscriptions":[]}` ← **无订阅** |
| `POST /rest/rate-limits` | ❌ 403 oauth2-forbidden（body `{requestKind, modelName}`） |
| `GET /rest/media/imagine/generation_estimate` | ❌ 403（POST 返回 501，是 GET 端点） |
| `GET /rest/products` | ❌ 403 |
| `GET /rest/usage/limits`、`/rest/credits`、`/rest/entitlements`、`/rest/billing/usage` | ❌ 404 不存在 |

`POST /rest/rate-limits` 的响应结构（bundle 提取，虽不可用但记下）：

```jsonc
{"windowSizeSeconds": 0, "remainingQueries": 0, "waitTimeSeconds": 0,
 "totalQueries": 0, "remainingTokens": 0, "totalTokens": 0,
 "lowEffortRateLimits":  {"provider","triggerType","dimensions","providerEnum",
                          "triggerTypeEnum","dimensionEntries"},
 "highEffortRateLimits": { /* 同上 */ },
 "preGenerationDelayMs": 0}
```

### 1.6 生图被拒的三种形态（2026-10-19 实测，三个凭证各命中一种）

**这是本次逆向最重要的发现** —— 限流不止一种形态，且第三种会伪装成成功。

#### 形态 1：请求层 429（整个账号被限流）

```
HTTP 429  0.7s
{"error":{"code":8,"message":"Too many requests","details":[]}}
```

流开始前就被拒，**连普通对话都发不出**。凭证 `c4zc82bzvk` 命中。

#### 形态 2：流内 `renderToolRateLimited`（生图被拒，对话正常）

```jsonc
{"cardAttachment": {"jsonData": "{..."image_chunk":{
  "imageUuid":"...","seq":0,"progress":100,"systemErrCode":1006}}"}}
{"error": {"message": "You've hit your image rate limit. Please try again later.",
           "renderToolRateLimited": {"rateLimitedCount": 1},
           "severity": "STREAM_ERROR_SEVERITY_NORMAL"}}
```

**`progress` 仍是 100 但没有 `imageUrl`** —— 只看 progress 会误判成完成。
凭证 `stjeh3gnvk` 命中。

#### 形态 3：静默降级（最隐蔽）

```
HTTP 200  4.1s  len=6817
无 error、无 cardAttachment、无 systemErrCode
modelResponse.message = "Got it — test received. How can I help?"
```

**额度耗尽后服务端不再报错，而是把生图请求当普通对话回。**
只看 `error` 字段会把它**误判成成功**。凭证 `pd1jt9qjfa` 命中。

> 实测序列：同一凭证连发 8 次生图**全部成功**（每次 9–14 秒、19–20 帧、2 个图片帧），
> 紧接着下一轮第 1 次就变成形态 3。即额度是**按次数**计，用完后静默降级。

#### 客户端的判定链（bundle 提取）

`js/0lm06h5jbq2j6.js`：

```js
let v = "usage_pool_exhausted", y = "usage_limit_reached";
function C(e) {                          // getUsageLimitKind
  let {errCode: t, failureReason: n} = e;
  if (t === v) return "pool_exhausted";
  if (t === y) return "limit_reached";
  if (n === "PIPELINE_FAILURE_REASON_USAGE_POOL_EXHAUSTED") return "pool_exhausted";
  if (n === "PIPELINE_FAILURE_REASON_USAGE_LIMIT_REACHED") return "limit_reached";
  let i = e.typedError;
  if (i) {
    if (i.usagePoolExhausted !== undefined || i.usage_pool_exhausted !== undefined) return "pool_exhausted";
    if (i.usageLimitReached   !== undefined || i.usage_limit_reached   !== undefined) return "limit_reached";
  }
  let r = e.message;
  if (typeof r === "string") {
    if (r.startsWith(v)) return "pool_exhausted";
    if (r.startsWith(y)) return "limit_reached";
  }
}
```

`js/1nina2n46pgtw.js`：

```js
function n(e, t, s) { return void 0 !== e[t] || void 0 !== e[s] }

function o(e) {                          // accountRateLimitKindFromStreamErrors
  let t = e ?? [];
  return t.some(x => n(x, "globalRateLimit", "global_rate_limit")) ? "global"
       : (!!t.some(x => n(x, "usageLimitReached", "usage_limit_reached")) && "user");
}

// 抑制反馈的流错误（不弹「有帮助吗」）
function (e) {
  return false !== o(e) || (e ?? []).some(x =>
    n(x, "usagePoolExhausted", "usage_pool_exhausted") ||
    void 0 !== x.renderToolRateLimited);
}
```

`js/05thxs9prvmlu.js` 的完整 kind 映射：

```js
switch (kind) {
  case "beyondContextLength": case "userPromptTooLong":
  case "attachmentFileTooLarge": case "attachmentImageTooLarge":
  case "attachmentTooManyPages":            return "content_too_large";
  case "modelNotAvailable":
    return t.modelNotAvailable?.reason === "rate_limited" ? "model_rate_limited" : null;
  case "globalRateLimit":   return "global_rate_limit";
  case "concurrencyLimit":  return "concurrency_limit";
  case "systemKillSwitch":  return "system_kill_switch";
  case "usagePoolExhausted":return "pool_exhausted";
  case "usageLimitReached": return "limit_reached";
  case "connectorAuthFailed": case "attachmentModerated":
  case "attachmentLoadFailed": case "renderToolFailed":
  case "renderToolRateLimited": case "renderPerMessageLimitExceeded":
    return null;                            // 这些不算「用量限制」
  default: return kind;
}
```

#### `StreamError` 结构（50 字段，bundle 提取）

`js/0a18uk1xp-t5a.js`。按用途分组：

| 组 | 字段 |
|---|---|
| 限流 | `globalRateLimit`、`usagePoolExhausted`、`usageLimitReached{midTurn}`、`concurrencyLimit`、`renderToolRateLimited{rateLimitedCount}`、`renderPerMessageLimitExceeded{droppedCount}` |
| 模型 | `modelNotAvailable`（`reason` ∈ `entitlement` / `rate_limited`） |
| 系统 | `systemKillSwitch{featureName}`、`internalError`、`requestCancelled` |
| 内容 | `beyondContextLength`、`userPromptTooLong`、`contentModerated{conversationClosed}` |
| 附件 | `attachmentModerated`、`attachmentImageTooLarge{maxSizeBytes,fileName}`、`attachmentFileTooLarge{fileName}`、`attachmentTooManyPages{maxPages,pageCount}`、`attachmentLoadFailed` |
| 工具 | `renderToolFailed{failedCount}`、`toolCallFailed{toolUsageCardId,toolName}` |
| 通用 | `message`、`severity`、`internalDebug{summary,workspace,cause,detail}` |

`modelNotAvailable.reason` 的两个取值对应两条不同文案：

```js
"entitlement" === reason
  ? "This model isn't available with your current plan. If you just upgraded, try again."
  : "This model is temporarily unavailable. Please try a different model."
```

### 1.7 网页生图已打通（2026-10-19 实测）

**模型名是 `build`** —— 这是整件事的关键。实测分级：

| modelName | 结果 |
|---|---|
| **`build`** | ✅ **200 + 真实出图** |
| `imagine-agent-mode` | ✅ 出图，但走 MCP 工具 `imagine_text_to_image`，慢且额度受限 |
| `imagine-agent-mode-grok-4-5` | ⚠️ 报 "You've reached the image generation limit" |
| `grok-3` | ❌ 通过名称校验但 `modelNotAvailable` |
| `grok-420` | ❌ 429 heavy usage |
| `grok-4.7` / `grok-4.5` / `grok-4.20-*` / `grok-build-0.1` | ❌ 403 `Model is not found` |

这些名字来自 **`POST /rest/models`**（不是 GET —— GET 返回 501）：
33 个模型，含 `build`、`grok-3`、`grok-420`、`imagine-agent-mode*`、
`grok-plugins-4p{5,6,7}-*`。默认值 `defaultFreeModel: grok-3`、
`defaultProModel: grok-4`、`unavailableModels: []`。

#### 请求形态

```jsonc
POST https://grok.com/rest/app-chat/conversations/new
Authorization: Bearer <access_token>

{
  "message": "A serene mountain lake at sunrise, photorealistic",
  "modelName": "build",                        // ← 关键
  "temporary": true,
  "deviceEnvInfo": { /* 6 字段 */ },
  "kind": "CONVERSATION_KIND_IMAGINE",
  "enableImageGeneration": true,
  "enableImageStreaming": true,
  "mediaGenInput": {"textToImage": {"prompt": "<prompt>", "numOfImages": 1}},
  "responseMetadata": {"modelConfigOverride": {"modelMap": {"imageEditModel": "imagine"}}}
}
```

实测耗时 **10–19 秒**（`temporary: true`，不落历史）。

#### 图片在 `cardAttachment` 里 —— 不是 `streamingImageGenerationResponse`

这是实现时最容易踩的坑。真实响应流（按序）：

```jsonc
// 1) 卡片声明（还没有图）
{"cardAttachment": {"jsonData": "{\"id\":\"gi6r5\",
  \"type\":\"render_generated_image\",\"cardType\":\"generated_image_card\",
  \"prompt\":\"A realistic red apple...\",\"orientation\":\"portrait\",
  \"image_chunk\":null}"}}

// 2) 第一帧：progress=50，路径带 -part-0
{"cardAttachment": {"jsonData": "{...\"image_chunk\":{
  \"imageUuid\":\"15ccde3c-...\",
  \"imageUrl\":\"users/<uid>/generated/15ccde3c-...-part-0/image.jpg\",
  \"mimeType\":\"image/jpeg\",\"seq\":0,\"progress\":50,\"imageModel\":\"imagine_x_1\"}}"}}

// 3) 最终帧：progress=100，路径去掉 -part-0
{"cardAttachment": {"jsonData": "{...\"resolution\":{\"width\":784,\"height\":1176,
  \"aspect_ratio\":\"2:3\"},\"image_chunk\":{
  \"imageUrl\":\"users/<uid>/generated/15ccde3c-.../image.jpg\",
  \"seq\":1,\"progress\":100,\"moderated\":false}}"}}
```

**`jsonData` 是字符串化的 JSON，要二次解析。**
`ModelResponse` 里另有 `<grok:render card_id="gi6r5" card_type="generated_image_card">`
的 XML 标记，但**图片 URL 只在 `cardAttachment` 里**。

#### 图片下载需要 Bearer

```
GET https://assets.grok.com/<imageUrl>
Authorization: Bearer <access_token>      → 200, image/jpeg, 351,813 B, 784×1168

（无认证 → 403，0 字节）
```

实测下载验证：**65,930 字节 → 784×1168 RGB JPEG**，内容与 prompt 完全相符。

#### 限流：`progress=100` 也可能没有图

连续生图会触发限流，此时**失败帧的 `progress` 仍是 100**：

```jsonc
{"cardAttachment": {"jsonData": "{...\"image_chunk\":{
  \"imageUuid\":\"a461724a-...\",\"seq\":0,\"progress\":100,
  \"systemErrCode\":1006}}"}}          // ← 没有 imageUrl
{"error": {"message": "You've hit your image rate limit. Please try again later.",
           "renderToolRateLimited": {"rateLimitedCount": 1},
           "severity": "STREAM_ERROR_SEVERITY_NORMAL"}}
```

**只看 `progress` 会把失败帧误判成完成。** 必须同时检查
`imageUrl` 是否存在与 `systemErrCode` / `renderToolRateLimited`。
换凭证可绕过（实测第一个凭证限流后，第二个立即成功）。

### 1.8 权限是端点级的

同一凭证在网页路径上：

| 端点 | 结果 |
|---|---|
| `GET /rest/app-chat/conversations` | ✅ 200 `{"conversations":[],"textSearchMatches":[]}` |
| `GET /rest/app-chat/conversations-many` | ✅ 200 |
| `GET /rest/app-chat/conversation-starters` | ❌ 403 `Action cannot be performed by OAuth2 token users` |
| `GET /rest/app-chat/image-generations` | ❌ 403 同上 |
| `GET /rest/app-chat/suggestions` | ❌ 403 同上 |
| `POST /rest/app-chat/quick-answer` | ❌ 403 同上 |

即：**只读会话列表可用，其余一律被 OAuth2 策略挡住**（`WKE=unauthorized:oauth2-auth-forbidden`）。

---

### 1.9 CLI 控制面：套餐 / 额度 / 策略（2026-10-19 实测）

> 线索来自 [codex2api](https://github.com/james-6-23/codex2api) 的
> `grok-build-main/docs/GROK_BUILD_INFERENCE_API_REVERSE.md` §9。
> 该仓库**没有**用网页路径（`grok.com/rest/*`），走的是 CLI 网关 + `api.x.ai`。

公共请求头（各端点有差异，不要无条件全加）：

```http
Authorization: Bearer <oauth-access-token>
X-XAI-Token-Auth: xai-grok-cli
x-grok-client-version: 1.0.13
x-grok-client-mode: headless
x-userid: <user-id>          # billing / auto-topup 用，/user 不用
```

#### `GET /billing?format=credits` —— 账期与用量

实测（三个凭证）：

```jsonc
{"config": {
  "creditUsagePercent": 1.0,          // ← 缺失表示字段未返回，**不是 0%**
  "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": "2026-10-19T05:23:41.035+00:00",
                    "end":   "2026-10-26T05:23:41.035+00:00"},
  "prepaidBalance": {"val": 0},
  "isUnifiedBillingUser": true,
  "productUsage": [{"product": "GrokBuild", "usagePercent": 1.0}],
  "topUpMethod": null,
  "billingPeriodStart": "...", "billingPeriodEnd": "..."}}
```

| 凭证 | `creditUsagePercent` | 周期 | 网页生图观察到的拒绝形态 |
|---|---|---|---|
| `c4zc82bzvk` | **缺失** | 周 | ① 429 |
| `pd1jt9qjfa` | **1.0** | 周 | ③ 静默降级（约 15 分钟后恢复） |
| `stjeh3gnvk` | **缺失** | 周 | ② `renderToolRateLimited` |

语义要点：

- `creditUsagePercent` **缺失 ≠ 0%** —— 保留「字段缺失」这一事实。
- 周期是 **WEEKLY（7 天）**，`currentPeriod.end` 是 reset 时间。**没有 30D 窗口**。
- `productUsage` 的产品字符串是**开放枚举**（实测见过 `GrokBuild` / `GrokChat` /
  `GrokImagine`），**不是模型 ID**。
- `{}` 要解析成 `val=0`（proto3 把零值 cents 编码成空对象）。
- `monthlyLimit` / `used` / `onDemandCap` 是**旧版字段**，实测三个凭证都没有。

#### **决定性实验：网页生图不计入 billing**

```
生图前: creditUsagePercent=1.0  productUsage=[{"product":"GrokBuild","usagePercent":1.0}]
生图后: creditUsagePercent=1.0  productUsage=[{"product":"GrokBuild","usagePercent":1.0}]
        ↑ 完全不变，GrokImagine 从未出现
```

**网页生图额度是独立账本，billing 看不到。** 这解释了为什么上一轮
`quota_info` 返回 403 却仍能生图 —— 两条额度路径互不相通。

#### 其它控制面端点

| 端点 | 实测 |
|---|---|
| `GET /user?include=subscription` | ✅ 200，24 字段；`subscriptionTier: null`（缺失 ≠ Free） |
| `GET /settings` | ✅ 200 `min_client_version: "1.0.13"`、`force_update: true`、`allow_access: true` |
| `GET /auto-topup-rule` | ✅ 200 `{}`（无规则） |

`/user` **不发 `x-userid`**；`/settings` 与 `/models` 在认证资料含邮箱时可发 `x-email`。

#### 版本门控：426

**实测**（2026-10-19，同一凭证同一请求）：

```
x-grok-client-version: 1.0.1   → 426 {"error":"Your Grok CLI version (1.0.1) is
                                        outdated. Please update to version 1.0.13
                                        or later via `grok update` ..."}
x-grok-client-version: 1.0.13  → 200
x-grok-client-version: 1.0.62  → 200
x-grok-client-version: 0.0.1   → 426
```

**控制面端点不做版本门控**（`1.0.1` 也能拿 billing），**只有生成端点做**。
所以客户端版本要跟着 `GET /settings` 的 `min_client_version` 走，不要硬编码。

#### 上游错误 → 冷却映射（对齐 codex2api `applyGrokCooldown`）

| 错误 | 判定依据 | 冷却 |
|---|---|---|
| 免费额度耗尽 | 错误体含 `used all the included free usage` / `subscription:free-usage-exhausted` | **24h**（free 账号整号；付费账号模型级） |
| 超支 / 余额耗尽 | 含 `spending-limit` / `balance_exhausted` / `usage balance exhausted` 等，且 402 或 429 | **24h** |
| 裸 402 | 无上述标记 | **不硬隔离**（证据不足） |
| 429 | 状态码 | `Retry-After` 或 60s，**上限 900s** |
| 401 | 状态码 | 60s + 异步刷新 token |
| 403 | 状态码 | 300s，**绝不刷新 token** |
| 426 | 状态码 | 60s（`version_required`） |

**判定顺序：先看错误体，再看状态码** —— 免费额度耗尽可能以 429 或 402 出现。

免费额度耗尽的错误体带**权威用量**，可正则提取：

```
tokens (actual/limit): 1003617/1000000
```

免费层是**滚动 24 小时窗口**（`Usage resets over a rolling 24-hour window`）。

#### SSO → OAuth 自动转换（codex2api 的做法）

输入是 grok.com 网页登录的 `sso` cookie，程序化跑完 RFC 8628 Device Code 授权
（无需人工点授权页），产出 `access_token` + `refresh_token`：

```
1) GET  https://accounts.x.ai/                       Cookie: sso=<token>; sso-rw=<token>
   → 弱校验：401 或跳 sign-in/sign-up 判失效
2) POST https://auth.x.ai/oauth2/device/code         client_id + scope
   → {device_code, user_code, verification_uri_complete, interval, expires_in}
3) GET  <verification_uri_complete>                  打开验证页
   POST https://auth.x.ai/oauth2/device/verify       user_code=<code>
   → 跳转须含 "consent"
4) POST https://auth.x.ai/oauth2/device/approve      user_code + action=allow
                                                     + principal_type=User
   → 跳转须含 "done"
```

`client_id` = `b1a00492-073a-47ea-816f-4c329264a828`（官方 Grok CLI 公开 id），
scope = `openid profile email offline_access grok-cli:access api:access`。
用**浏览器 UA**（OAuth 端点对非浏览器 UA 更易触发风控）。

### 1.10 网页额度端点：`GET /rest/grok/credits`（protobuf 逆向）

**这是网页 UI 真正使用的额度端点**，不是 `/rest/media/imagine/quota_info`。

#### 怎么找到的：解码 bundle 里的 protobuf `fileDesc`

`js/00pmbps1j6es_.js` 的模块 `9096207` 内嵌了 base64 编码的 `.proto` 描述符
（`(0,s.fileDesc)("...")`）。解码后得到完整 schema：

`prod/grok/backend/proto/grok_build_billing.proto`（包 `grok_api_v2`）：

```protobuf
service GrokBuildBilling {
  rpc GetGrokBuildBillingConfig          -> GET  /rest/grok-build/billing
  rpc SetGrokBuildOnDemandConfig         -> POST /rest/grok-build/billing/on-demand
  rpc GetGrokCreditsConfig               -> GET  /rest/grok/credits          // ← 网页额度
  rpc ListPaymentMethods                 -> GET  /rest/grok/payment-methods
  rpc CheckPaymentSetupFinalized         -> GET  /rest/grok/payment-setup-finalized
  rpc TopUpOrGetExistingPendingChange    -> POST /rest/grok/top-up
  rpc TopUpUsingInlinePaymentMethod      -> POST /rest/grok/top-up-inline
  rpc SetDefaultPaymentMethod            -> POST /rest/grok/set-default-payment-method
  rpc ListPrepaidBalanceChanges          -> GET  /rest/grok/prepaid-balance-changes
  rpc GetAutoTopupRule                   -> GET  /rest/grok/auto-topup-rule
  rpc SetAutoTopupRule                   -> POST /rest/grok/auto-topup-rule
  rpc GetAutoTopupStats                  -> GET  /rest/grok/auto-topup-stats
  rpc GetInvoice                         -> GET  /rest/grok/invoice
  rpc ListInvoices                       -> GET  /rest/grok/invoices
}

message GrokCreditsConfig {
  UsagePeriod  current_period        = 8;
  float        credit_usage_percent  = 1;
  Cent         on_demand_cap         = 2;
  Cent         on_demand_used        = 3;
  ProductUsage product_usage[]       = 7;   // ← 按产品分项
  PeriodUsage  history[]             = 6;
  bool         is_unified_billing_user = 11;
  Cent         prepaid_balance       = 12;
  TopUpMethod  top_up_method         = 13;
  // 4/5 billing_period_start/end 已标 deprecated（`B02 `）
}

message ProductUsage {
  billing_product.Product product = 1;
  float                   usage_percent = 2;
}

enum UsagePeriodType {
  USAGE_PERIOD_TYPE_UNSPECIFIED = 0;
  USAGE_PERIOD_TYPE_MONTHLY     = 1;
  USAGE_PERIOD_TYPE_WEEKLY      = 2;
}
```

`prod/billing-pub/proto/product.proto` 的 `Product` 枚举 ——
**`PRODUCT_GROK_IMAGINE = 5` 是生图独立产品位**：

```protobuf
enum Product {
  PRODUCT_UNSPECIFIED = 0;  PRODUCT_API = 1;
  PRODUCT_GROK_BUILD = 2;   PRODUCT_GROK_PLUGINS = 3;
  PRODUCT_GROK_CHAT = 4;    PRODUCT_GROK_IMAGINE = 5;
  PRODUCT_GROK_VOICE = 6;   PRODUCT_GROK_APP_BUILDER = 7;
  PRODUCT_GROK_TASKS = 8;   PRODUCT_GROK_BOT = 9;
  PRODUCT_CURSOR = 10;      PRODUCT_BUGBOT = 11;
}
```

`GetGrokCreditsConfigRequest` 只有一个字段
`exclude_legacy_monthly_usage`（bool），对应 query
`?exclude_legacy_monthly_usage=true`。

#### 前端怎么用它

`useChatUsageLimit`（`js/00pmbps1j6es_.js`）→ `getGrokCreditsConfig`：

```js
let f = [90, 75, 50, 25];                        // 提示阈值
function g(e){ return Number.isFinite(e) ? Math.max(0, e) : 0 }
function h(e){ return e?.isUnifiedBillingUser === false }   // 额度链关闭条件
function V(e){ let t = e.config; return h(t) ? void 0 : g(t?.creditUsagePercent) }
function crossedThreshold(e,t){ return f.find(n => e < n && t >= n) ?? null }

refetchInterval = pct >= 90 ? 30000 : pct >= 50 ? 60000 : 180000
// isUnifiedBillingUser === false  → 整条额度链关闭，不轮询
```

即：**网页的额度判定就是 `creditUsagePercent` + `[90,75,50,25]` 阈值 + 分档轮询**。
`js/00cd1bc6g3tng.js` 里 `creditUsagePercent >= 99` 时再叠一个 30 秒刷新。

#### 实测：CLI 签发的 token 拿不到

| 认证 | 结果 |
|---|---|
| `Authorization: Bearer <oauth-at>` | **403** `code:7` `The OAuth2 access token could not be validated. [WKE=unauthenticated:bad-credentials]` |
| `x-api-key: <token>` | **400** `code:3` `Cannot authenticate with API key. [WKE=unauthenticated:invalid-credentials]` |
| `Cookie: sso=<token>` | **401** `code:16` `Bad credentials.` |
| 无认证 | **401** `code:16` `No credentials presented.` |

加上 `X-XAI-Token-Auth` / `x-grok-client-version` / `x-grok-client-mode` 全套 CLI 头
**仍然 403**。`grok.com/rest/grok-build/billing` 与 `/rest/grok/auto-topup-rule`
在 `grok.com` 上是 **404**（只在别的主机/路径下有效）。

**原因在 JWT 里**（解码 `access_token` 的 payload）：

```jsonc
{"iss":"https://auth.x.ai",
 "sub":"79e28854-d70a-4c57-9a4d-535f2adb4e60",
 "aud":"b1a00492-073a-47ea-816f-4c329264a828",   // ← Grok CLI 客户端
 "client_id":"b1a00492-073a-47ea-816f-4c329264a828",
 "scope":"openid profile email offline_access grok-cli:access api:access
          conversations:read conversations:write",
 "principal_type":"User",
 "team_id":"71e8548a-7d36-430f-8e25-71be0b2734c2"}
```

`aud` 与 `client_id` 都是 **Grok CLI 客户端**，而 `/rest/grok/credits`
只认 **grok.com 网页应用**的 OAuth 客户端。所以：

* 同一 token 在 `/rest/app-chat/*` 上**可用**（那套走 `conversations:read/write`）
* 在 `/rest/grok/credits` 上被判 **`bad-credentials`**

#### 2026-10-20 更新：拿到真实 SSO 会话后的实测

有了真实 `sso` cookie（HS256 JWT，payload 只有 `session_id`）后重测：

**`Cookie: sso=<token>` → 200**（`sso` 单发即可，`sso-rw` 非必需）

```jsonc
{"config": {
  "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": "2026-10-20T04:58:53.937Z",
                    "end":   "2026-10-27T04:58:53.937Z"},
  "creditUsagePercent": 0,
  "onDemandCap": null, "onDemandUsed": null,
  "productUsage": [],                                  // 空
  "history": [],
  "isUnifiedBillingUser": true,
  "prepaidBalance": {"val": "0"},                       // 字符串，Cent
  "topUpMethod": "TOP_UP_METHOD_SAVED_PAYMENT_METHOD",
  "billingPeriodStart": "2026-10-20T04:58:53.937Z",
  "billingPeriodEnd":   "2026-10-27T04:58:53.937Z"}}
```

注意 `prepaidBalance.val` 是**字符串** `"0"`（proto3 JSON 对 64 位整数的表示），
`onDemandCap`/`onDemandUsed` 是 `null`（未开按量付费）。

**CLI token 的拒绝是账号级的，不是普遍规律**：

| 凭证 | team_id | `GET /rest/grok/credits` |
|---|---|---|
| `e578i2gj1l` | `fa960e03-…` | ✅ **200** |
| `c4zc82bzvk` | `71e8548a-…` | ❌ 403 `bad-credentials` |
| `pd1jt9qjfa` | `bc331b91-…` | ❌ 403 `bad-credentials` |
| `stjeh3gnvk` | `3b3d0b2d-…` | ❌ 403 `bad-credentials` |

四者的 JWT `aud` / `client_id` **完全相同**（都是 `b1a00492-…`），
`scope` 也相同 —— 只有 `team_id` 不同。所以**拒绝是团队/账号级策略**，
不是 token 形态问题。调用方应把 403 当「该账号读不到」而非「端点不可用」。

#### ★ 决定性实验：生图**完全不计入**这个账本

用可读的账号（`e578i2gj1l`）连发生图，每次同时查 credits：

| # | 耗时 | 结果 | `creditUsagePercent` | `productUsage` |
|---|---|---|---|---|
| 1–7 | 8.9–15.3s | ✅ 出图 | **0** | **`[]`** |
| 8 | 3.7s | ❌ `renderToolRateLimited` | 0 | `[]` |

（此前同一账号已生成 1 张，故窗口内共 **8 张**，第 9 次被拒。）

**`creditUsagePercent` 全程恒 0、`productUsage` 全程恒 `[]`。**

即：`PRODUCT_GROK_IMAGINE` 虽然在 protobuf 枚举里存在，但**免费层网页生图
不写这个账本** —— 生图走的是**纯速率限制**（约 8 张/窗口），不是 credit 计量。

对照：CLI 账本（`/billing?format=credits`）对该账号也是 `pct=None`、
`productUsage=null`，同样不含生图。

#### 顺带发现的上游瞬时故障

```
503 {"code":14, "message":"cursor billing: snapshot failed"}
```

同一请求重试即恢复 —— 必须重试，不能当成永久拒绝（`web_credits(retries=3)`）。

#### 结论：网页生图额度对我们不可观测

三条额度路径互不相通：

| 路径 | 端点 | 含生图? | 我们能查? |
|---|---|---|---|
| 网页账本 | `GET /rest/grok/credits` | ❌ 实测生图后仍 `[]` | ✅ SSO cookie 可读；CLI token 看账号 |
| CLI 账本 | `GET cli-chat-proxy/v1/billing?format=credits` | ❌ 只有 `GrokBuild` | ✅ 200 |
| 免费层闸门 | `GET /rest/usage/free-usage-gates` | ⚠️ 有 `imagine` 但恒 0/0 | ✅ 200（无信息量） |

**三条路径都不记生图用量。** 生图配额是**纯速率限制**（实测约 8 张/窗口），
判定是否还有额度只能靠实际请求的三种拒绝形态（§1.6）。

### 1.11 89 个 media 端点的权限分布（2026-10-19 实测）

穷举 `/rest/media/*` 全部 89 个端点：

| 结果 | 数量 | 含义 |
|---|---|---|
| ✅ 200 | 9 | 可用 |
| ⚠️ 400 | 20 | **授权通过，只是参数不对** —— 同样可用 |
| ❌ 403 | 60 | `oauth2-auth-forbidden` |

**400 不等于 403** —— 这是本轮最容易误判的地方。400 说明端点对我们开放。

可用的 9 个（200）：

```
POST /rest/media/canvas/assets              {"assets":[]}
POST /rest/media/canvas/create              创建画布
POST /rest/media/canvas/list                列出画布
POST /rest/media/canvas/node/bulk-get       {"canvases":[]}
POST /rest/media/canvas/node/remove-from-group  {}
POST /rest/media/pipeline/template-folder/list  模板分类
POST /rest/media/pipeline/template/list         模板列表
POST /rest/media/post/delete-all            {}
POST /rest/media/post/list                  公共 feed（见下）
```

可用的 20 个（400，补参数即可）：`canvas/{delete,get,update,ensure-conversation,
node/{create,list,delete,patch,restore,update},set-thumbnail}`、
`conversation/{create,get}`、`pipeline/template/get`、`post/get`、
`segment`、`video/{caption,music,upscale}`。

#### `/rest/media/post/list` 是公共 feed，不能用来数自己的生图

请求体 schema（bundle 提取）：

```js
{limit, cursor, filter, isKidsMode, isNsfwEnabled,
 withContainerOnly, includeCanvas}
```

实测：返回的 `userId` 是 `e8d36c9a-...`，而**本账号**是 `95c42eb3-...`
（`canvas/create` 与生图 URL 都证实）。穷举 18 种 `filter` 形态
（`{userId}`、`{owner}`、`{isMine}`、`{kind:"MINE"}` …）**全部返回同一个公共 feed**
—— `filter` 被服务端忽略。`post/get` 的 `requestUserId` 同样无效。

**所以不能靠它统计自己的生图次数。**

### 1.12 生图请求体的完整字段（补充）

除 §1.7 已记录的 `mediaGenInput.textToImage{prompt,numOfImages}` 外，
`ModelResponse` 里还有这些与图片相关的通道（bundle 提取，实测多数为空）：

| 字段 | 用途 |
|---|---|
| `cardAttachment.jsonData` | **实际出图的通道**（§1.7） |
| `imageAttachmentInfo.imageAttachmentCount` | 附件图片数（不是生成数） |
| `imageDimensions` | `{width, height}` |
| `streamingImageGenerationResponse` | 逐帧预览（实测空） |
| `cachedImageGenerationResponse` | 缓存命中（实测空） |
| `streamingVideoGenerationResponse` | 视频帧 |
| `generatedImageUrls` | 旧路径（实测空） |
| `progressReport` | `{category, state, message, percentage}` |

客户端的"图片仍在生成中"判定是：

```js
e.progress < 100 && e.moderated !== true && (e.systemErrCode ?? 0) === 0
```

即 **`progress`、`moderated`、`systemErrCode` 三者必须同时满足** ——
只看 `progress` 会把失败帧（`progress=100` 但 `systemErrCode=1006`）当成完成。

### 1.13 ★ 代码执行：`POST /rest/app-chat/run-code`（2026-10-20 实测）

**网页路径真的执行代码并返回 stdout/stderr。** 这是本轮最有价值的发现。

```jsonc
// 请求
{"code": "import numpy, sys; print(sys.version)", "language": "python"}
// 响应
{"success": true, "stdout": "3.12.3\n", "stderr": "", "outputFiles": {}}
```

#### `language` 只认四个值

| 值 | 实际执行 |
|---|---|
| `python` / `python3` | CPython **3.12.3** |
| `bash` | bash |
| `javascript` | **Node v24.15.0**（源码写到 `/home/workdir/temp.js`） |
| `sh` / `node` / `sql` / `ruby` / 其它 | ❌ 内部错误 |

**省略 `language` 或只传 `code` 会失败**，返回通用文案
`An unexpected error occurred while executing your code. Please try again or check your code for errors.`
—— 这不是语法错，是**缺语言字段**。

#### 沙箱刻画

```
python     3.12.3
platform   Linux-4.19.0-gvisor-x86_64-with-glibc2.39    ← gVisor 用户态内核
cwd        /home/workdir
uid        0                                             ← root
cpu        2
DNS        通（example.com → 172.66.147.243）
TCP        **拒绝**（ConnectionRefusedError）—— 无网络出口
```

文件系统：

```
/              → .hades-container-tools, .package-cache-mutate, README.xai,
                 app, bin, boot, dev, etc, home, lib, ...
/home/workdir  → .grok, artifacts
/root          → .bashrc, .cache, .config, .local, .npm, .profile, .ssh
```

可用库（实测 `importlib.util.find_spec`）：

```
numpy pandas requests PIL bs4 lxml scipy matplotlib torch httpx yaml
```

缺失：`sklearn` `aiohttp` `regex`。

性能参考：2 秒 CPU 密集循环约 **8,439,930** 次迭代。20KB 输出可正常返回。

#### 错误语义的坑

`success: false` 时 `stderr` 可能是**通用文案**而不是真实异常：

* 语言缺失 / `sh` / `node` / `sql` / `ruby` → 通用文案
* 真实语法/运行错误 → 带堆栈（例如 Node 的
  `SyntaxError: Unexpected identifier 'ok'` + `at wrapSafe (node:internal/...)`）

所以**不能只看 `success`** —— 要区分「通用文案」与「真堆栈」。

### 1.14 42 个 `/rest/app-chat/*` 端点的权限分布

用真实 SSO 会话穷举（比 CLI token 宽松得多）：

| 结果 | 数量 |
|---|---|
| ✅ 200 | **14** |
| ⚠️ 400/500（授权通过，参数或内部错） | **27** |
| ❌ 403 | **1** |

**14 个可用的（200）**：

```
GET  /rest/app-chat/app-builder-conversations    {"items":[]}
GET  /rest/app-chat/conversations                真实对话列表
GET  /rest/app-chat/conversations-many           {"conversations":[]}
GET  /rest/app-chat/conversations/deleted        {"conversations":[]}
GET  /rest/app-chat/image-generations            {"generations":[]}   ← 见下
GET  /rest/app-chat/import-memory                {"content":""}
GET  /rest/app-chat/import-memory/status         {"status":"IMPORTED_MEMORY_STATUS_NONE"}
GET  /rest/app-chat/template-prompt              {"prompt":"","scriptedGreeting":false}
GET  /rest/app-chat/tts-voices                   {"voices":[{"voiceId":"altair",...}]}
GET  /rest/app-chat/user-memory-blurb            {"memoryContent":""}
POST /rest/app-chat/migrate-conversations        {}
POST /rest/app-chat/run-code                     ★ 代码执行
POST /rest/app-chat/template-chat                {"conversation":null,"settled":false}
POST /rest/app-chat/upload-file                  {"fileMetadataId":...,"fileUri":...}
```

值得注意的 400/500（**授权通过**，补参数即可）：

* `POST /rest/app-chat/conversations/new` → 400 `Cannot generate response to empty conversation.`
* `POST /rest/app-chat/fetch-suggestions` / `suggestions` → 400 `Count must be greater than 0 and less than 10`
* `POST /rest/app-chat/text-to-speech` → 400 `Field text must not be empty`
* `POST /rest/app-chat/upload-file-v2/init` → 400 `size_bytes must be positive`
* `GET /rest/app-chat/app-deployments` → 400 `exactly one of deployment_id or latest_by_conversation_id must be specified`
* `GET /rest/app-chat/google-drive/files` → **501** `Google Drive browsing has moved to connector tools`

**唯一 403**：`GET /rest/app-chat/conversation-starters` 系列（500 内部错误，非权限）。
`POST /rest/app-chat/voice/share` → **401** `No OAuth access token present. Please log in again.`
—— 这个端点**要求 OAuth token 而非 cookie**（与其它端点相反）。

#### `image-generations` 也不能计数

`GET /rest/app-chat/image-generations` + SSO cookie → 200，但：

* 生图**成功后仍为 `[]`**
* 被限流时也不记录

**所以它不能用来统计生图用量。** 至此四条路径（`/rest/grok/credits`、
CLI `/billing`、`free-usage-gates`、`image-generations`）**都不记生图**。

### 1.15 ★ 视频生成与直连模型名（2026-10-20 实测）

**`textToVideo` + `modelName: "imagine-video-gen"` → 200，返回 video id。**

```
modelName: "imagine-video-gen"   ✅ 200  → d1b9572a-348c-4a25-8b01-a912c09c2284
modelName: "imagine-video"       ❌ 403  Model is not found（错名）
modelName: "build" + textToVideo ❌ 429（build 不是视频模型）
```

#### `mediaGenInput` 是**六选一**的 oneof

bundle（`js/00pmbps1j6es_.js` 的 `d()`）按固定顺序取第一个存在的键：

```js
if (e.textToImage)      return {kind:"textToImage",      params:e.textToImage};
if (e.imageToImage)     return {kind:"imageToImage",     params:e.imageToImage};
if (e.textToVideo)      return {kind:"textToVideo",      params:e.textToVideo};
if (e.imageToVideo)     return {kind:"imageToVideo",     params:e.imageToVideo};
if (e.referenceToVideo) return {kind:"referenceToVideo", params:e.referenceToVideo};
if (e.videoExtension)   return {kind:"videoExtension",   params:e.videoExtension};
```

各变体字段（`js/0a18uk1xp-t5a.js` / `29p4h4iryyz76.js`）：

| 变体 | 字段 |
|---|---|
| `textToImage` | `prompt, numOfImages, aspectRatio, resolutionName, modelName` |
| `imageToImage` | `inputAssets[], prompt, extractSubject, draw{styleId,regionIntent}, mediaReferences[]` |
| `textToVideo` | `prompt, aspectRatio, duration, resolutionName, modelName, skipAudio` |
| `imageToVideo` | `prompt, inputAssets[], aspectRatio, duration, resolutionName, modelName, mode, skipAudio, useFirstFrame` |
| `referenceToVideo` | `inputAssets[], audioAssets[], mediaReferences[], firstFrameAsset, lastFrameAsset, aspectRatio, duration, resolutionName, modelName, skipAudio` |
| `videoExtension` | `prompt, inputAssets[], duration, videoExtensionStartTime, skipAudio` |

构造器（`js/2bbm9n6clh3_p.js`）：`buildTextToImageInput`、`buildTextToVideoInput`、
`buildImageToVideoInput`、`buildReferenceToVideoInput`、`buildVideoExtensionInput`。

#### 直连模型名（`js/0oottrppfxt08.js` 的构造分支）

```js
if (hasInputImages) {
  modelName = "imagine-image-edit";
  body.imageEditModelConfig = {
    imageReferences: [...],        // 必需
    aspectRatio: "w:h",           // **仅 1 张图且显式设置时**
    selectionRegions: [...],      // 可选，局部重绘
  };
} else {
  modelName = "imagine-image-gen";
  body.imageGenModelConfig = {
    aspectRatio: "w:h",
    mode: "quality" | "fast",      // pro 档 → quality，否则 fast
    resolutionName: "2k",          // 仅 quality + 2k + 开关打开
  };
}
```

| 模型名 | 用途 | 验证状态 | 实测耗时 |
|---|---|---|---|
| `imagine-image-gen` `mode=fast` | 文生图 | ✅ **200 出图** | **6.5s** |
| `imagine-image-gen` `mode=quality` `2k` | 文生图（高质量） | ✅ **200 出图** | **5.9s** |
| `imagine-video-gen` | 视频 | ✅ **200 + video id** | 26.6s |
| `build` | 文生图（§1.7 用的） | ✅ 200 `imagine_x_1` 784×1176 | 12.5s |
| `imagine-image-edit` | 图生图 | 解出，未完成验证（配额耗尽） | — |

**`imagine-image-gen` 比 `build` 快一倍**（6.5s vs 12.5s），且支持 `quality`+`2k` 档。

#### `upload-file` 是 JSON + base64，不是 multipart

bundle（`chatUploadFileRaw`）：

```js
body: {fileName, fileMimeType, content, makePublic,
       fileSource, thirdPartyFileId, workspaceId}
```

`content` 是 **base64 字符串**，`Content-Type: application/json`。
用 `files=` 做 multipart 会 **400**（空 body）。

#### 图生图链路

```
1) 生成/准备源图          → users/<uid>/generated/<uuid>/image.jpg
2) GET https://assets.grok.com/<key>  + Bearer   → 图片字节
3) POST /rest/app-chat/upload-file  {fileName, fileMimeType,
                                     content: base64, makePublic: false}
   → {fileMetadataId, fileUri, ...}
4) POST /rest/app-chat/conversations/new
   modelName: "imagine-image-edit"
   mediaGenInput: {imageToImage: {inputAssets: [fileUri], prompt: "..."}}
   imageEditModelConfig: {imageReferences: [fileUri], aspectRatio?: "1:1"}
```

#### 视频是异步任务，轮询端点未找到

`/rest/media/video/*` 只有 `music`、`caption`、`upscale`、`video-sequence/create`，
**都不是状态查询**。所以目前只能拿到 video id，产物获取方式未确认。

#### ★ 图片与视频共享同一个速率限制桶

实测：一次 `textToVideo` 成功后，紧接着发图片立刻 **429 `Too many requests`**。
所以 §1.6 测出的「约 8 张/窗口」是**媒体总配额**，不是纯图片配额。

新账号实测序列（`yqrpwedp6e`，2026-10-21）：

```
imagine-image-gen fast     ✅ 6.5s
imagine-image-gen q/2k     ✅ 5.9s
build                      ✅ 12.5s
imagine-video-gen          ✅ 26.6s
────────────────────────────────
之后第 5 次 → 静默降级（200、无图、无 error）
```

即 **3 图 + 1 视频** 就吃满了这一窗口 —— 视频占用的配额明显多于单张图。

#### ★ 重试会**加剧**限流（静默降级 → 429）

实测同一账号在配额耗尽后按 **3 分钟间隔**重试：

```
[尝试 1] 200  静默降级（无图无错）
[尝试 2] 200  静默降级
[尝试 3] 200  静默降级
[尝试 4] 200  静默降级
[尝试 5] 429  Too many requests      ← 升级
[尝试 6] 429
[尝试 7] 429
[尝试 8] 429
```

**每次重试都计入速率窗口**，所以密集重试会把自己锁死。
静默降级后应**等待足够长的静默期**（实测 ≥15 分钟）再试，
而不是周期性探测。

### 1.16 ★★ 撤回：「媒体限流是 IP 级」的结论不成立（2026-10-21 晚修正）

> **本节是对 2026-10-21 早先结论的撤回。** 原结论（媒体配额按出口 IP 计量、
> 加账号不增加产能）**证据不成立** —— 它被一个请求体缺陷污染了。

#### 原始（错误）推理

四个账号的 `allow_access` / `subscriptionTier` / `creditUsagePercent` /
`productUsage` 完全相同，其中三个从未生成过任何东西，却**首次调用即静默降级**；
而曾成功过的账号也 429。于是推断「同一 IP 共享预算」。

#### 对照实验推翻了它

同一账号、同一 prompt，**只改请求体**：

| 请求体 | 结果 |
|---|---|
| `build_body` 的**完整** body | **出图** ✅ |
| **精简** body（缺 `disableSearch` / `isReasoning` / `modelMode`，缺 `x-xai-request-id` 头） | 200 + **无图**，正文却说 `"I generated an image with the prompt: ..."` ❌ |

**服务端照常回 200 并声称已生成，但不产出 `cardAttachment`。**

那三个「全新账号首次即失败」的探测，用的正是**精简 body** —— 所以它们的失败
与 IP、与配额都无关，纯粹是**调用方漏了字段**。

#### 正确的结论：`silent_degrade` 有两个成因

| 成因 | 触发条件 | 判别 |
|---|---|---|
| **① 请求体不完整** | 缺 `disableSearch` / `isReasoning` / `modelMode` 任一，或缺 `x-xai-request-id` | 正文声称"已生成"但无 `cardAttachment`；**换完整 body 立刻成功** |
| **② 真配额耗尽** | 用完整 body 连发约 **4–8 次** | 429，或正文为空/拒绝 |

**排查顺序：先确认走的是 `build_body`（完整字段 + request-id 头），再看配额。**
用精简 body 探配额会得到**假阳性**。

#### 仍然成立的观察

* 用完整 body 连发后确实会耗尽：`yqrpwedp6e` 出 3 图 + 1 视频后静默降级；
  `d781n9ynmc` 出 4 图后 429。
* 配额看起来是**按账号**的 —— 同 IP 下新账号在完整 body 下可以正常出图。
* 图片与视频共享同一预算桶（一次视频成功后再发图片立刻 429）。
* 密集重试会把自己锁死（每次重试都计入窗口）。

#### 关于 IP 限流

codex2api 的 CHANGELOG 确实提到
*"Too many Grok accounts egressing from one IP triggers upstream 402s"*，
它也有 `POST /proxies/auto-balance`。但**本仓库没有复现出 IP 级约束** ——
同 IP 下新账号可以正常出图。可能只在更大量的账号/IP 压力下才出现，未验证。

#### 教训

**「模型说它生成了」不等于「真的生成了」。** 判定成功必须看
`cardAttachment.image_chunk.imageUrl` 是否存在，不能看 HTTP 200，也不能看正文。

### 1.17 语音合成：`POST /rest/app-chat/text-to-speech`（2026-10-21 实测）

**可用**，产物是真实 MP3。

#### 请求

```jsonc
{"text": "Hello, this is a text to speech test.",
 "voiceId": "altair",
 // 以下均可选
 "language": "en", "codec": "mp3", "sampleRate": 24000,
 "bitRate": 128000, "speed": 1.0,
 "textNormalization": true, "optimizeStreamingLatency": true}
```

字段全集（`chatTextToSpeechRaw`）：`{text, voiceId, language, codec,
sampleRate, bitRate, optimizeStreamingLatency, textNormalization, speed}`。

#### 响应是 **NDJSON**，不是原始音频

```
{"result":{"data":"//PExABDhCn0DtZY8RquQ..."}}
{"result":{"data":"..."}}
...
```

每行一个 base64 分块，**全部拼接后 base64 解码**才是完整音频。
`//PExA` 解码后是 `0xFF 0xF3` —— MPEG 帧同步字。

#### 实测产物

```
chunks=5   bytes=41856
ffprobe:  codec=mp3  sample_rate=24000  channels=1(mono)
          bit_rate=128kbps  duration=2.616s
```

即 **24 kHz 单声道 128 kbps MP3**。

#### 音色列表

`GET /rest/app-chat/tts-voices` → `{voices:[{voiceId,name,language,gender}]}`，
实测含 `altair`（Altair / multilingual / male）、`ara` 等。

**该端点用 SSO cookie 可读；CLI Bearer 可能 403** —— 与
`/rest/app-chat/*` 的其它端点相反（那些更认 Bearer）。

### 1.18 模型目录：`POST /rest/models`

**必须用 POST**（GET 返回 501）。返回 33 个模型，单条 schema：

```jsonc
{"modelId": "build", "name": "", "description": "",
 "modeDescription": "", "modeName": "", "badgeText": "Beta",
 "tags": ["hide_tools_settings", "computer"],
 "modelMode": "MODEL_MODE_BUILD", "promptingBackend": "CHAT"}
```

顶层另有：

```jsonc
{"defaultAnonMode": "MODEL_MODE_AUTO", "defaultAnonModel": "grok-3",
 "defaultFreeMode": "MODEL_MODE_AUTO", "defaultFreeModel": "grok-3",
 "defaultHeavyMode": "MODEL_MODE_AUTO", "defaultHeavyModel": "grok-4",
 "defaultProMode": "MODEL_MODE_AUTO", "defaultProModel": "grok-4",
 "unavailableModels": []}
```

目录里的媒体模型只有 5 个：`build`、`imagine-agent-mode`、
`imagine-agent-mode-dev`、`imagine-agent-mode-grok-4-5`、
`imagine-video-prompt-enhance`。

**`imagine-image-gen` / `imagine-image-edit` / `imagine-video-gen` 不在目录里**
—— 它们是前端 image studio 直连的隐藏模型名，只能从 bundle 解出（§1.15）。

#### `imagine-video-prompt-enhance`：不消耗媒体配额的提示词增强

实测所有生图模型都 429 时它仍返回 200。输入 `"a cat walking in rain"`：

```
A cat walks steadily through falling rain along a wet street or path.
Raindrops glisten on its fur as it moves forward with a natural gait,
paws carefully navigating puddles and reflective ground. ...
```

**这是纯文本任务**（不带 `mediaGenInput`、不走 `CONVERSATION_KIND_IMAGINE`），
所以不吃媒体配额。

#### 所有生图模型共享同一个配额桶

配额耗尽后 `imagine-image-gen` / `build` / `imagine-agent-mode` /
`imagine-agent-mode-grok-4-5` **全部** 429。

此前观察到的「image-gen 成功 → build 429 → agent-mode 又成功」是
**秒级请求限流**，不是独立配额桶。

## 2. 主对话端点

```
POST https://grok.com/rest/app-chat/conversations/new
```

`js/0a18uk1xp-t5a.js` @51198，`chatCreateConversationAndRespondRaw`。

### 2.1 请求头

| 头 | 值 | 必需 |
|---|---|---|
| `Content-Type` | `application/json` | ✅ 前端显式设置 |
| `x-xai-request-id` | `crypto.randomUUID()`，每请求新生成 | ✅ 前端 pre-middleware 注入 |
| `Cookie` | 会话 cookie | ✅ 浏览器 `credentials:"include"` 自动带 |
| `Origin` / `Referer` | `https://grok.com` | ✅ 浏览器自动 |
| `x-acting-organization-id` | 仅当解析出 acting org | 条件 |
| `x-source-conversation-id` | 仅 `chatUploadFileComplete` | 条件 |

**不存在**（常见误判）：`x-statsig-id`（无此字符串）；
`x-app-version` / `x-device-model` / `x-device-vendor`（只在 `DataGatewayClient.getConfig`
这个**无任何调用点**的遥测方法里）；`x-app-language` / `x-app-timezone`
（是 `/ws/app_chat/stream_audio` 的 WebSocket query 参数，不是 HTTP 头）。

### 2.2 请求体（完整字段表）

```js
{
  systemPromptName, temporary, modelName, message, fileAttachments, imageAttachments,
  disableSearch, enableImageGeneration, returnImageBytes, returnRawGrokInXaiRequest,
  enableImageStreaming, imageGenerationCount, forceConcise, toolOverrides,
  enableSideBySide, sendFinalMetadata, customInstructions, customPersonality,
  deepsearchPreset, imageEditUri, isReasoning, webpageUrls, imageEditUris,
  workspaceIds, disableTextFollowUps, disableArtifact, responseMetadata,
  disableArtifactDiff, doForceTriggerArtifact, isFromGrokFiles,
  selectedFileTextContent, selectedFileTextContentStartPosition,
  selectedFileTextContentEndPosition, disableMemory, forceSideBySide,
  modelsUserCanUse, modelMode, skipResponseCache, isAsyncChat, enableNsfw,
  isKidsMode, supportedFastTools, isRegenRequest, templateId,
  disableSelfHarmShortCircuit, collectionIds, connectorIds, connectors,
  disabledConnectorIds, deviceEnvInfo, modelOverrideKey, browserGeoLocation,
  disablePersonalization, modeId, enabledSkills, imagineCanvasContext,
  linkQuery, mediaGenInput, kind, enableWatermark
}
```

**搜索 + 画图的最小集**：

| 字段 | 值 | 作用 |
|---|---|---|
| `message` | string | 用户输入 |
| `modelName` | string | 见 §6 |
| `disableSearch` | `false` | 保持 false（见下） |
| `toolOverrides` | object | **关搜索真正用这个** |
| `enableImageGeneration` | bool | 允许生图 |
| `enableImageStreaming` | bool | 生图时置 `true` |
| `mediaGenInput` | object | 生图参数（见 §5） |
| `deviceEnvInfo` | object | 6 字段，见 §2.4 |
| `kind` | string | 生图时 `"CONVERSATION_KIND_IMAGINE"` |

> **`disableSearch` 恒为 `false`** —— bundle 里从未置 `true`。
> 关搜索实际走 `toolOverrides.webSearch = false`。

### 2.3 `toolOverrides` 枚举全集（14 键）

```js
imageGen, webSearch, xSearch, xMediaSearch, trendsSearch, xPostAnalyze,
videoGen, audioGen, gmailSearch, googleCalendarSearch, outlookSearch,
outlookCalendarSearch, documentSearch, googleDriveSearch
```

**语义是「只在禁用时下发」** —— `buildToolOverrides` 原文：

```js
let e = {
  ...(0, t.builtInWebSearchOn)(n.getState().enabled) ? {} : { webSearch: !1 },
  ...o.getState().enabled ? {} : { xSearch: !1 }
}
```

两个开关默认 `enabled = !0`，所以默认**不发** `toolOverrides`（= 全开）。
要**只开网页搜索**：`{"xSearch": false, "imageGen": false, ...}` 把不需要的置 false。

> 另有一个 **19 键**版本挂在 suggestion 对象上（多 `xUserAnalyze` / `xSentimentSearch` /
> `financeStockCard` / `financeCryptoCard` / `sportsCard`）。请求体是否接受这 5 个键：
> **未找到证据**。

### 2.4 `deviceEnvInfo` / `browserGeoLocation`

```js
// GrokApiDeviceEnvInfoToJSON —— 6 字段，纯浏览器 API，无 canvas/WebGL 指纹
{ darkModeEnabled, devicePixelRatio, screenWidth, screenHeight, viewportWidth, viewportHeight }

// GrokApiGeoLocationToJSON —— 3 字段
{ latitude, longitude, accuracy }
```

`browserGeoLocation` 需 flag `enable_browser_geo_location` + 用户偏好，
5 秒超时，失败则**整字段不发**。

---

## 3. 响应结构

### 3.1 流式分块信封

```json
{
  "result": {
    "response":     { /* ModelResponse，见 §3.2 */ },
    "conversation": { /* Conversation，75 字段 */ },
    "title":        { /* GenerateTitleResponse */ }
  },
  "error": { "code": "...", "message": "...", "details": [...] }
}
```

三个子对象都可选 —— 增量分块只带变化的部分。

**两个端点的信封不同**（重要）：

| 端点 | 信封 |
|---|---|
| `/rest/app-chat/conversations/new` | `{result:{response, conversation, title}, error}` |
| `/rest/app-chat/conversations/{id}/responses` | `{result: <ModelResponse>, error}` |

### 3.2 `ModelResponse`（61 字段）

从 `FromJSON` 函数体程序化提取：

**搜索**
`query` `queryType` `webSearchResults` `citedWebSearchResults` `searchProductResults`
`ragResults` `citedRagResults` `connectorSearchResults` `citedConnectorSearchResults`
`collectionSearchResults` `citedCollectionSearchResults` `xpostIds` `xposts` `citedXposts`
`webpageUrls` `queryAction` `toolRequest` `citedSource`

**生图**
`generatedImageUrls` `generatedImageHeight` `generatedImageWidth`
`imageAttachments` `imageEditUri` `imageEditUris` `mediaGenInput` `mediaTypes`
`streamingImageGenerationResponse` `cachedImageGenerationResponse`

**文本/推理**
`message` `thinkingTrace` `thinkingStartTime` `thinkingEndTime` `steps`
`messageTag` `messageStepId` `isThinking` `isSoftStop` `streamingMetadata`

**状态/元数据**
`responseId` `sender` `createTime` `parentResponseId` `threadParentId` `parentQuotedText`
`partial` `manual` `shared` `isControl` `error` `streamErrors` `model` `metadata`
`requestMetadata` `uiLayout` `sideBySideConfig` `mode` `effort` `rolloutId`
`toolUsageCardId` `toolUsageCard` `fastToolResponse` `inputChunks` `outputChunks`

**附件**
`fileAttachments` `fileAttachmentsMetadata` `fileAttachmentAssetMetadata` `fileUris` `fileIds`
`cardAttachmentsJson` `toolResponses` `selectedFileTextContent`
`selectedFileTextContentStartPosition` `selectedFileTextContentEndPosition`

### 3.3 `messageTag` —— 增量文本的路由标签

这是**把流拆成「思考 / 搜索 / 正文」的关键字段**。已确认取值：

```
UNSET  FINAL  summary  header  thinking  thinking_start
response_start  tool_usage_card  raw_function_result  tool_partial_output
```

判定逻辑用 `b.toUpperCase()` 比较 `"UNSET"` / `"FINAL"`，其余小写 —— **大小写不敏感**。

「搜索进行中」的计时靠 `searchingStartTime` / `searchingEndTime`，
只在 `messageTag` 非 `UNSET` 时启动。

---

## 4. 搜索：结果怎么拿到

### 4.1 `webSearchResults`

```js
function nn(e) { return null == (n = e) ? n : {
  url:     null == n.url     ? void 0 : n.url,
  title:   null == n.title   ? void 0 : n.title,
  preview: null == n.preview ? void 0 : n.preview
} }
```

**REST 模型层**：`webSearchResults = { results: [{url, title, preview}] }`

> 这只是**客户端 transformer 保留的字段**。transformer 是「构造新对象」式实现，
> 未列出的字段会被丢弃 —— 服务端原始 JSON 是否还有更多字段，bundle 无法证明。

### 4.2 富字段版本（同一类型，另一处定义）

**路径是 `response.outputChunks[].toolResult.webSearch.webpages[]`** ——
注意 `toolResult` **不在 `ModelResponse` 顶层**，而在 `outputChunks` 数组的每个元素里：

```js
// ModelResponse 里
outputChunks: t.outputChunks.map(l.ChatOutputChunkFromJSON)

// ChatOutputChunk（54 字段）里
toolResult: { toolCallId,
  webSearch: { webpages: n.webpages.map(ef) },     // ← 富字段版
  xPost:     { posts: t.posts.map(ep) },
  codeExecution: { stdout, stderr, exitCode, commandTimedOut },
  xThread:   { thread: {...} },
  documentSearch, connectorResults, collectionResults,
  editFile, writeFile, readFile, grep, bash, toolPartialResult, ... }
```

`webpages[]` 用的是 **20 字段版本**：

```js
{ url, title, snippet, extraSnippets, datePublished, dateLastCrawled, humanizedDate,
  language, cachedPageUrl, parsedText, timeAccessed, description, siteName, creator,
  image, images, breaking, favicon, faviconBase64 }
```

**要做「当搜索用」，应该优先从 `outputChunks[].toolResult.webSearch.webpages` 取** ——
字段丰富得多（有 `snippet` / `parsedText` / `datePublished` / `siteName`）。

### 4.3 `citedWebSearchResults`

与 `webSearchResults` **同一类型**，语义是「被正文引用到的那些来源」。
客户端在 REST 路径上**同时双写**两者：

```js
u?.results && (O.citedWebSearchResults = [...O.citedWebSearchResults || [], ...u?.results || []], n(O))
```

逐条引用来源还有 `citedSource`：

```js
citedSource: { web, xpost, connector, collection }
```

### 4.4 网关（WebSocket）路径

事件名 `response.search.result`，payload：

```jsonc
{ "response_id": "...",
  "result": { "search_type": "web_search" | "x_search",
              "web_results": [...], "x_posts": [...] } }
```

处理逻辑把 `web_results` **同时写进** `webSearchResults` 和 `citedWebSearchResults`。

### 4.5 `deepsearchPreset` —— 唯一无法交付的项

**它是自由字符串，bundle 内始终为空**：

- 默认 `""`
- zod 校验 `z.string().optional().catch(void 0)`
- 数据流是**服务端回写 → 客户端回传**的往返：
  `metadata.deepsearchPreset` → `deepsearchPreset: s?.deepsearchPreset`
- **没有任何枚举、常量或 `...ToJSON` 约束**

相关但**不是**它的东西（别混）：

| 名称 | 实际是什么 |
|---|---|
| `GrokApiV2ModelMode` = `{Base:"BASE", Think:"THINK", Deepsearch:"DEEPSEARCH", Imagegen:"IMAGEGEN"}` | **定时任务**（scheduled task）的 modelMode，不是对话参数 |
| `"deep-search"` | i18n 键（阿拉伯语「بحث عميق」） |
| `"think-harder"` | Imagine agent 的 `source` 标记 |
| `follow-up-suggested-mode.{deepsearch,deepersearch,think,thinkharder}` | **后续建议模式**的 UI 文案键 |

DeepSearch 有**独立的额度类别**（`upsell-description.deepersearch_*`、
`Rate Limit Reached.description-deepersearch`），说明它是计费维度上的独立模式 ——
但**触发它的请求参数名与取值，未找到证据**。

---

## 5. 画图

### 5.1 入口：`openGenerationStream`

`js/2bbm9n6clh3_p.js` @168174 原文：

```js
async function j(e, t) {
  let r = A(t),
      o = r ? { imagineCanvasContext: { targetCanvasId: r }, enableSideBySide: !1 } : void 0;
  r && P(r);
  try {
    let { raw: t } = "add-response" === e.kind
      ? await d.chatApi.chatAddResponseRaw({
          conversationId: e.conversationId,
          body: { ...e.body, enableImageStreaming: !0,
                  ...(0, i.watermarkRequestFields)(), ...o,
                  ...r ? { skipCancelCurrentInflightRequests: !0 } : {} } })
      : await d.chatApi.chatCreateConversationAndRespondRaw({
          body: { ...e.body,
                  kind: l.GrokApiConversationKind.ConversationKindImagine,
                  enableImageStreaming: !0,
                  ...(0, i.watermarkRequestFields)(), ...o } });
    if (429 === t.status) { let e = await q(t); return r && M(r), { status: "rate-limited", ...e } }
    let n = "add-response" === e.kind ? V(t) : G(t);
    return { status: "ok", chunks: r ? L(n, r) : n }
  } catch (e) { throw r && M(r), e }
}
```

**两条路径**：

| 场景 | 端点 | 关键 body |
|---|---|---|
| 新会话生图 | `POST /rest/app-chat/conversations/new` | `kind:"CONVERSATION_KIND_IMAGINE"`、`enableImageStreaming:true` |
| 已有会话续生图 | `POST /rest/app-chat/conversations/{id}/responses` | `enableImageStreaming:true` |

`ConversationKind` 全集：

```
CONVERSATION_KIND_UNSPECIFIED  CONVERSATION_KIND_IMAGINE  CONVERSATION_KIND_PLUGIN
CONVERSATION_KIND_BOT          CONVERSATION_KIND_IMAGINE_DIRECTOR
```

**429 是结构化返回**，不是异常：`{status:"rate-limited"}`，可能带
`peakHours.{startSeconds,endSeconds}` 与 `isGlobal` —— 服务端会告知限流窗口。

### 5.2 `mediaGenInput` 是 8 选 1 oneof

**不是扁平结构**。定义在 `js/0a18uk1xp-t5a.js` @127263（函数 `x`，导出模块 `8452408`）：

```
textToImage  imageToImage  textToVideo  imageToVideo
referenceToVideo  videoExtension  template  hydratedContext
```

生图相关的两个：

**`textToImage`**（纯文生图）

| 字段 | 类型 | 备注 |
|---|---|---|
| `prompt` | string | |
| `numOfImages` | number | 默认 `featureStore.getFeature("imagine-configs").image_gen_count_override`，钳 1–16 |
| `aspectRatio` | string | `"auto"` / `"1:1"` / `"2:3"` … |
| `resolutionName` | string | `"1k"` / `"2k"` |
| `modelName` | string | |
| `mode` | string | `"speed"` / `"quality"` |

**`imageToImage`**（图生图 / 编辑）

```js
{ prompt, inputAssets, numOfImages, aspectRatio, resolutionName, modelName,
  selectionRegions, mediaReferences, reframe{expand{targetAspectRatio,cropRect},
  zoom{cropRect,mode}}, multiRegionEdits, extractSubject{subject}, draw{styleId,regionIntent} }
```

**`inputAssets` 是 assetId（UUID），不是 URL。**

### 5.3 生图请求示例

```jsonc
{
  "message": "<prompt>",
  "modelName": "imagine-image-gen",
  "kind": "CONVERSATION_KIND_IMAGINE",
  "enableImageGeneration": true,
  "enableImageStreaming": true,
  "mediaGenInput": { "textToImage": { "prompt": "<prompt>", "numOfImages": 2 } },
  "responseMetadata": { "modelConfigOverride": { "modelMap": { "imageEditModel": "imagine" } } }
  // 可选： "enableWatermark": true,
  //       "imagineCanvasContext": { "targetCanvasId": "<id>" }
}
```

文生图 vs 编辑的模型名：`imagine-image-gen` vs `imagine-image-edit`。
编辑时空 prompt 发 `" "`（单个空格），3 次重试 / 退避 2s。

### 5.4 图片结果三通道

同一 chunk 里可能并存：

| 通道 | 字段 | 用途 |
|---|---|---|
| 逐帧预览 | `streamingImageGenerationResponse` | 实时进度 |
| 最终资产 | `modelResponse.fileAttachmentAssetMetadata[]` | 按 mimeType `image/` 过滤 |
| 旧路径 | `modelResponse.generatedImageUrls` | 兼容 |
| 命中缓存 | `cachedImageGenerationResponse.imageUrl` | |

`streamingImageGenerationResponse` **14 字段**（`GrokApiStreamingImageGenerationResponseFromJSON` 原文）：

```js
{ imageId, imageUrl, seq, progress, moderated, imageIndex, assetId, imageModel,
  rRated, sideBySideIndex, hasWatermark, appliedModerationRule,
  isRootUserUploaded, rejectionReason }
```

> **审核字段必须处理**：`moderated` / `rejectionReason` / `appliedModerationRule` /
> `rRated` —— 生图被拒时 `rejectionReason` 会给出原因，`progress` 可能停在中间值。
> 只读 `imageUrl` 而不看这几个字段，会把「被拒」误判成「还在生成」。

### 5.5 图片 URL 形态：CDN + 相对 key

**不是 `/rest/media/...`**。`imageUrl` 是**相对 key**，渲染时拼到 `ASSET_SERVER_URL`：

```js
// index.html 内联 env
"ASSET_SERVER_URL": "https://assets.grok.com"
"SHARED_ASSET_SERVER_URL": "https://assets.grokusercontent.com"

// 拼接（js/0__nwepjsaz6y.js）
function n(e, t) {
  if (!t) return;
  if (t.startsWith("data:") || t.startsWith("blob:") || t.startsWith("http")) return t;
  let n = new URL(e); return n.pathname = t, n.toString()
}
```

**key 取值链**：`key → previewImageKey → auxKeys["preview-image"]`
**高清**：`hdKey` / `hd1080Key`
**同源时**加 `?cache=1`

> `imageUrl` 的具体路径前缀（如 `/generated/...`）**无字面量证据** ——
> bundle 内它只是可被 `new URL(base).pathname` 赋值的相对 key。需实况响应确认。

---

## 6. 模型名全集

```
grok-3          grok-3-5             grok-3-mini
grok-4          grok-4-1             grok-4-1-nightly
grok-4-1-thinking  grok-4-auto       grok-4-heavy
grok-4-mini-thinking
grok-420        grok-43
```

生图：`imagine-image-gen`（文生图）、`imagine-image-edit`（编辑）、
`imagine-video-gen`（视频）、`imagine`（`imageEditModel` 覆盖值）

`modelMode` 取值：`"none"` 或 mode id。

---

## 7. 完整 API 面

**85 个方法**（`js/0a18uk1xp-t5a.js`），**353 个 `/rest/` 路径**。

### 7.1 对话核心

| 方法 | HTTP | 路径 |
|---|---|---|
| `chatCreateConversationAndRespond` | POST | `/rest/app-chat/conversations/new` |
| `chatAddResponse` | POST | `/rest/app-chat/conversations/{conversationId}/responses` |
| `chatListResponses` | GET | `/rest/app-chat/conversations/{conversationId}/responses` |
| `chatGetConversation` | GET | `/rest/app-chat/conversations/{conversationId}` |
| `chatListConversations` | GET | `/rest/app-chat/conversations` |
| `chatGenerateTitle` | POST | `/rest/app-chat/conversations/{conversationId}/title` |
| `chatStopInflightResponse` | DELETE | `/rest/app-chat/conversations/inflight-response/{responseId}` |

### 7.2 断流重连（长连接必备）

```
GET /rest/app-chat/conversations/reconnect-response/{responseId}
    /rest/app-chat/conversations/reconnect-response-v2/{responseId}
    /rest/app-chat/conversations/inflight-response/{responseId}
```

流断开后可用 `responseId` 重新挂上同一条流，**不会重复生成**。

### 7.3 生图/媒体

```
GET  /rest/app-chat/image-generations
GET  /rest/app-chat/v2/conversations/{conversationId}/responses/{responseId}/tool-results
```

另有 `/rest/media/*` **89 个路径**（canvas / collection / folder / assets / gifs /
search / generate-music / generate-voice）。

---

## 8. 最小可用调用（待实况验证）

> **本仓库已按本文档实现客户端**：`services/grok_web_api.py`
> （`search()` / `generate_image()` / `ask()` / `stream_conversation()` / `reconnect()`）。
> 解析层与请求构造有 62 项单测覆盖（`test/test_grok_web_api.py`），
> 网络与认证部分需要真实 cookie 才能验证。

```python
from services.grok_web_api import search, generate_image

COOKIES = {"<会话 cookie>": "<value>"}      # 名称需抓包确认，见 §9

# 当搜索用
hits = search("2026 年 AI 领域有什么大事", cookies=COOKIES)
print(hits["answer"])
for src in hits["sources"]:
    print(src.get("title"), src.get("url"), src.get("snippet"))

# 画图
out = generate_image("一只戴墨镜的柴犬", cookies=COOKIES, count=2)
for img in out["images"]:
    print(img["image_url"])
print("被拒:", out["rejected"])
```

等价的裸 `requests` 写法：

```python
import json, uuid, requests

URL = "https://grok.com/rest/app-chat/conversations/new"
COOKIES = {"<会话 cookie>": "<value>"}      # 名称需抓包确认，见 §9

def ask(prompt: str, *, search: bool = True, image: bool = False):
    body = {
        "message": prompt,
        "modelName": "grok-4-auto",
        "disableSearch": False,
        "temporary": False,
        "isReasoning": False,
        "modelMode": "none",
        "deviceEnvInfo": {
            "darkModeEnabled": False, "devicePixelRatio": 1,
            "screenWidth": 1920, "screenHeight": 1080,
            "viewportWidth": 1920, "viewportHeight": 947,
        },
    }
    if not search:
        body["toolOverrides"] = {"webSearch": False, "xSearch": False}
    if image:
        body.update({
            "kind": "CONVERSATION_KIND_IMAGINE",
            "enableImageGeneration": True,
            "enableImageStreaming": True,
            "mediaGenInput": {"textToImage": {"prompt": prompt, "numOfImages": 2}},
            "responseMetadata": {
                "modelConfigOverride": {"modelMap": {"imageEditModel": "imagine"}}
            },
        })

    headers = {
        "Content-Type": "application/json",
        "x-xai-request-id": str(uuid.uuid4()),
        "Origin": "https://grok.com",
        "Referer": "https://grok.com/",
    }
    with requests.post(URL, json=body, headers=headers, cookies=COOKIES,
                       stream=True, timeout=300) as r:
        r.raise_for_status()
        for line in r.iter_lines():          # NDJSON：逐行 JSON.parse
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue                     # 坏行静默跳过（与前端一致）
            resp = ((chunk.get("result") or {}).get("response")) or {}

            if resp.get("message"):
                print(resp["message"], end="", flush=True)

            # 搜索来源 —— 富字段版在 outputChunks[].toolResult.webSearch.webpages
            for chunk in resp.get("outputChunks") or []:
                pages = ((chunk.get("toolResult") or {}).get("webSearch") or {}).get("webpages") or []
                for page in pages:
                    print(f"\n[src] {page.get('title')} — {page.get('url')}")
                    if page.get("snippet"):
                        print(f"      {page['snippet'][:120]}")
            # 简化版（字段少，但一定在顶层）
            for src in resp.get("citedWebSearchResults") or []:
                print(f"[cite] {src.get('title')} — {src.get('url')}")

            # 生图
            frame = resp.get("streamingImageGenerationResponse") or {}
            if frame.get("rejectionReason"):
                print(f"\n[img] 被拒: {frame['rejectionReason']} "
                      f"(rule={frame.get('appliedModerationRule')})")
            elif frame.get("imageUrl"):
                print(f"\n[img] https://assets.grok.com/{frame['imageUrl']} "
                      f"progress={frame.get('progress')} seq={frame.get('seq')}")
            for url in resp.get("generatedImageUrls") or []:
                print(f"[img] {url}")
```

---

## 9. 验证状态

| 项 | 状态 |
|---|---|
| 静态逆向（§1–§8 全部结论） | ✅ bundle 原文证据 |
| **实况请求/响应抓包** | ❌ **未做** |
| 搜索端到端 | ❌ **未做** |
| 生图端到端 | ❌ **未做** |

**为什么没做**：本环境**没有可用的 grok.com 凭证**。

- 远端号池唯一账号的 `access_token` 于 **2026-07-26 03:25 UTC 过期**（距今 85 天），
  `refresh_token` 返回 `HTTP 400 invalid_grant`（永久性失效）
- 本地 `data/grok_accounts.json` 为 0 条

### 必须用有效凭证验证的项

1. **会话 cookie 的实际名称** —— 前端完全不引用 `sso` / `sso-rw` / `cf_clearance`
   （这三个字符串在 1367 个 chunk 里**均为 0 命中**），靠 `credentials:"include"` 隐式携带。
   必须抓包或从浏览器导出。
2. **`imageUrl` 的路径前缀** —— bundle 内无字面量。
3. **`webSearchResults` 的服务端原始字段集** —— transformer 会丢弃未声明字段。
4. **`deepsearchPreset` 的取值** —— §4.5。
5. **服务端是否强校验 `deviceEnvInfo` / `x-xai-request-id`**。
6. **turnstile 触发频率** —— 触发条件是 `403` + 响应头 `cf-mitigated: challenge`，
   对话端点走同一管线（`fetchApi: fetchAndRecord4xx5xx`），所以**适用**：
   弹 widget（sitekey `0x4AAAAAABSXp6OOe7EVGPnR`）→ `POST /api/verify-turnstile`
   body `{"token"}` → `{success:true}` → **原样重放原请求**。

---

## 10. 抓取方法（可复现）

```bash
PX="socks5h://<user>:<pass>@<host>:<port>"   # 本环境直连 grok.com 不通
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"

# 1) 首页
curl -sS --socks5-hostname "$PX" -H "User-Agent: $UA" https://grok.com/ -o index.html

# 2) 入口 chunk（index.html 里引用的 111 个）
grep -oE 'https://cdn\.grok\.com/_next/static/chunks/[^"]*\.js' index.html | sort -u > chunks.txt

# 3) 全量 chunk —— 入口 chunk 内部还引用 1257 个懒加载 chunk，必须一起下
grep -ohE '[0-9a-zA-Z_-]{8,24}\.js' js/*.js index.html | sort -u > all_names.txt
mkdir -p js_all && cp js/*.js js_all/
comm -23 all_names.txt <(ls js | sort -u) | \
  xargs -P 12 -I{} curl -sS --socks5-hostname "$PX" -H "User-Agent: $UA" \
    -o "js_all/{}" "https://cdn.grok.com/_next/static/chunks/{}"

# 4) 检索
grep -ohE '"/rest/[a-zA-Z0-9/_.-]+"' js_all/*.js | tr -d '"' | sort -u > rest_paths.txt   # 353
grep -ohE 'async [a-zA-Z]+Raw\(' js_all/0a18uk1xp-t5a.js | sed 's/async //;s/($//' | sort -u  # 85
```

**为什么必须下全量**：入口 111 个 chunk 只占 bundle 引用的 8%。
`processChunks`（流解析器）就在懒加载模块 `180418` 里 —— 只看入口 chunk 会
得出「流格式未知」的错误结论。全量下载后 60 MB / 1367 个文件。

---

## 11. 与 CLI 路径的区别

本仓库现有的 `services/grok_backend_api.py` 走的是 **CLI/Build 路径**
（`cli-chat-proxy.grok.com`，`X-XAI-Token-Auth: xai-grok-cli`），
用 `refresh_token` 换 `access_token`，与本文档的**网页路径**是两套东西：

| | 网页路径（本文档） | CLI 路径（现有代码） |
|---|---|---|
| 端点 | `grok.com/rest/app-chat/*` | `cli-chat-proxy.grok.com/v1/*` |
| 认证 | 会话 Cookie | `Authorization: Bearer <access_token>` |
| 流格式 | NDJSON | SSE |
| 搜索 | ✅ 内嵌在对话流 | ❌ |
| 画图 | ✅ Imagine | ✅ 生图 |

**两者凭证不通用** —— 网页路径要的是浏览器会话 cookie，CLI 路径要的是 OAuth token。
要把网页对话当搜索用，需要新增一条 cookie 认证的客户端，不能复用现有 CLI 链路。

---

## 12. 包结构分析（全量 1367 chunk）

| 项 | 数量 |
|---|---|
| chunk（JS） | **1367** |
| 总大小 | **56.2 MB** |
| Turbopack 模块 | **2983** |
| API 方法（`*Raw`） | **427** |
| `/rest/` 路径 | **353** |
| CSS | 6 |

### 12.1 API 方法按域分布

| 域 | 方法数 |
|---|---|
| `media*` | 90 |
| `chat*` | 85 |
| `workspace*` | 33 |
| `grok*` | 33 |
| `voice*` | 26 |
| `build*` | 25 |
| `auth*` | 25 |
| `user*` | 20 |
| `subscriptions*` | 18 |
| `asset*` | 16 |
| `automations*` | 13 |
| `verified*` | 13 |
| 其它（settings / files / system / notifications / models / sharing / suggestion / rate / skills） | 30 |

### 12.2 `/rest/` 路径按域分布

| 域 | 路径数 |
|---|---|
| `media` | 89 |
| `app-chat` | 42 |
| `auth` | 34 |
| `connectors` | 23 |
| `subscriptions` | 16 |
| `grok-business` | 14 |
| `grok-for-teams` | 13 |
| `voice` | 10 |
| `github` | 9 |
| `agent-sandbox` | 6 |
| `user-skills` | 6 |
| `tasks` | 5 |
| 其它 40 个域 | 86 |

### 12.3 关键发现：入口 chunk 只占 8%

`index.html` 只引用 **111** 个 chunk，但 bundle 内部还引用 **1257** 个懒加载 chunk。
**流解析器 `processChunks`（模块 `180418`）就在懒加载里** —— 只看入口会得出
「流格式未知」的错误结论。全量下载后 56.2 MB / 1367 个文件。

### 12.4 客户端架构要点

- **构建器**：Turbopack（存在 `turbopack-*.js`），chunk 名是 base36 短名
- **无 `__NEXT_DATA__`**：改用 RSC flight（`self.__next_f`）
- **API 客户端**：OpenAPI 生成（`DefaultApi` + `Configuration`），
  方法名保留（`chatCreateConversationAndRespondRaw` 等），可直接检索
- **认证**：`credentials:"include"`（Cookie）+ 服务端同时接受 `Authorization: Bearer`
- **中间件**：`pre: tG`（注入 `x-xai-request-id`）+ `post: tk`（4xx/5xx 记录）
- **反爬**：`fetchAndRecord4xx5xx` 检测 `403 + cf-mitigated: challenge` → 弹
  Turnstile（sitekey `0x4AAAAAABSXp6OOe7EVGPnR`）→ `POST /api/verify-turnstile`
  → 原样重放原请求

## 附：分析素材位置

| 文件 | 内容 |
|---|---|
| `.verify/grok-web/analysis/core.md` | 核心对话链路（本文档 §1–§3、§7 的详细版） |
| `.verify/grok-web/analysis/search.md` | 搜索/DeepSearch 专项（681 行，19 处「未找到证据」） |
| `.verify/grok-web/analysis/image.md` | 生图/Imagine 专项（1080 行，含全部子 schema 原文） |
| `.verify/grok-web/analysis/auth.md` | 认证/请求头/设备指纹（792 行） |
| `.verify/grok-web/api_map.txt` | 85 个方法 → 路径的完整映射 |
| `.verify/grok-web/rest_paths.txt` | 353 个 `/rest/` 路径 |
| `.verify/grok-web/js_all/` | 1367 个 chunk（60 MB） |

> `.verify/` 在 `.gitignore` 内。需要长期保留的结论已并入本文档。
