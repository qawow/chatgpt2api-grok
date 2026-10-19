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
