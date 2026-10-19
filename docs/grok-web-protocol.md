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

bundle 里的 `Bearer` 只属 next-axiom 日志库、`Basic` 属 Mixpanel、
`DPoP` 只挂 `auth_mgmt.*` 的 10 个 ConnectRPC 方法（flag `grok_dpop_enforce_enabled`，默认空集）。

`x-statsig-id` **全 bundle 无此字符串**（`rg -ci statsig` 只命中 survey 名与 Sentry `denyUrls`）。

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
