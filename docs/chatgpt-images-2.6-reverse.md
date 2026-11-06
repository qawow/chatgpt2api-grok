# ChatGPT 网页图片传输逆向与兼容修复

> 2026-11-05 的本地静态分析与离线实现记录。**没有使用真实账号生图，没有验证当前账号是否可用 GPT‑Image 2.6，也没有据此发布 `gpt-image-2.6` API ID。**

## 1. 结论与证据边界

- 网页图片入口仍可在捕获代码中确认：`POST /backend-api/f/conversation/prepare`，随后 `POST /backend-api/f/conversation`，携带 `system_hints: ["picture_v2"]`。
- 本项目公开图片模型继续使用 `gpt-image-2.5`，兼容旧 `gpt-image-2`；网页 conversation 请求映射为 `model: "auto"`。这是路由选择，不是底层模型版本证明。
- 捕获的网页代码明确包含命名 SSE、`v1` 增量解码、恢复控制 token、`/f/conversation/resume` 与图片资源恢复路径。原后端丢失事件名称、逐 `data:` 行解析、没有恢复协议，属于可证实的兼容缺口。
- 没搜到版本字符串，既不能证明“2.6 已启用”，也不能证明“2.6 不存在”。静态代码出现某个功能或路径，不能证明它是此次版本新增、对当前账号开放，或实际调用成功。
- 本地素材当时统计约 9,217 个 JS 文件、约 190 MB；下载记录有 1 项失败。它不是所有灰度、账号、地区及运行时分支的完整覆盖。抓取目录和原分析脚本是本地验证素材，不作为可公开的交付文件。

## 2. 可复核的静态定位

下表位置为 **UTF-8 文件的零基字节偏移**，不是行号；构建改变后不能机械复用。

| 捕获文件及模块 | 位置 | 可确认内容 |
| --- | --- | --- |
| [`956483.8b486f09f8.js`](<../.verify/cgpt26/js/956483.8b486f09f8.js>)，`r3oB` | 模块 `[246197,279470)`；268076、275152 | `/backend-api/f/conversation/resume` |
| 同上 | 274080 | 请求体 `conversation_id:f.conversationId,offset:f.offset` |
| 同上 | 279048 附近 `function Q(e)` | 恢复头 `x-conduit-token` 与 `x-oai-turn-trace-id` |
| 同上 | 255944、261867、262499 | prepare／初始 conversation／其现有重试中的 `x-oai-mcp-form-version: "2"` |
| [`418851.a4e06377a0.js`](<../.verify/cgpt26/js/418851.a4e06377a0.js>)，`Ydv6` | 模块 `[309230,347307)`；311450 | 按 SSE `event` 分派；每个新 `delta_encoding` 声明创建新 decoder |
| 同上 | 313318 | `decodeDelta` 的缩写字段与继承规则 |
| 同上 | 313969、314341、314574 | tracker、事件处理与最新恢复 token 更新 |
| 同上 | 314678、314757 | `acknowledgeEvent` 与 `prepareRetry`；非 response 事件计数 |
| `r3oB` | 273193 | 先处理事件，再确认计数 |

素材位于本地 `.verify/cgpt26/js/`；公开依据是当时下载的 ChatGPT 页面资源，而非官方版本公告。

## 3. 图片请求与挑战头

实现保留已有 prepare → conversation 图片调用形状，同时补齐下列传输行为：

| 范围 | 处理 |
| --- | --- |
| 已生成的 Chat Requirements／PoW | 按原有链路发送；保留已存在、且有回归测试的 403 proof-rejection 有界重试 |
| 已生成的 Turnstile／Session Observer token | 图片请求现在传递它们；空值不构造假的 token |
| MCP form version | 仅 prepare 与初始 `/f/conversation` 添加 `x-oai-mcp-form-version: "2"`；没有把它推测为所有路径的必需头 |
| Turn trace | 同一图片 turn 的初始流与一次恢复使用同一个 trace |
| 资源下载 | 不把上述挑战头发送到 CDN／外部资源域名 |

`supports_buffering`、`supported_encodings`、`paragen_cot_summary_display_override`、`force_parallel_switch` 等既有字段没有因为“新 bundle 某处未出现”就盲删。也不再声称服务端一定忽略它们；需要运行时请求证据才能作该判断。

## 4. SSE 必须按完整事件处理

事件以空行结束；多个 `data:` 字段用换行连接为一个 payload。`event` 保留且逐帧重置，`id` 保留 SSE 规定的继承行为；注释、心跳、空分隔行不是数据事件。EOF 不把未结束的块擅自变成完整帧。

```text
event: delta_encoding
data: "v1"

event: delta
data: {"c":0,"p":"","o":"add","v":{"conversation_id":"example-id","message":{"author":{"role":"assistant"},"content":{"content_type":"text","parts":["开始"]}}}}

event: message
data: {"type":"resume_conversation_token","token":"<runtime-token>","conversation_id":"example-id"}

```

- `v1` 默认初态：channel 0、op `add`、path 空串、value 为 JavaScript `undefined`。
- 顶层缺失的缩写 `c/p/o` 继承**全局上一条 delta**，不是每通道独立的默认值；`v` 不继承。捕获实现先填短字段再映射长名，因此只传长名也不应自行猜测另一套规则。
- 各 channel 的完整根状态独立；支持 add／remove／replace／append／truncate／相对路径 nested patch。
- JSON Pointer 解码 `~1` 后 `~0`，根据下一段类型建立缺失容器；危险键／路径、非法 channel 和明显超大数组扩展明确报协议错误。
- 失败操作不提交部分状态。内部区分 `undefined` 与 null，向 JSON 投影时按对象省略／数组 null 的语义处理。
- **没有新 encoding 声明的恢复流复用 decoder；收到新 `delta_encoding: "v1"` 则重建基线**，与捕获的网页实现一致。
- UTF-16 截断可能短暂产生半个 surrogate；在重建 JSON 与最终 OpenAI／Anthropic SSE JSON 边界使用 ASCII 转义，保持内部语义，同时保证每个 HTTP chunk 可严格 UTF-8 编码。

旧 `iter_sse_payloads` 字符串接口仍可供既有调用使用；网页图片路径使用新的帧接口及 v1 重建，而不是假定所有旧调用已具备命名事件语义。

## 5. 恢复请求：token 是头，offset 是事件数

```http
POST /backend-api/f/conversation/resume
Accept: text/event-stream
X-Conduit-Token: <latest-stream-resume-token>
X-Oai-Turn-Trace-Id: <same-turn-trace>
Content-Type: application/json

{"conversation_id":"example-id","offset":5}
```

1. `X-Conduit-Token` 使用**最新流控制事件的 token 原值**；不是 prepare token，也不放入 body。
2. `offset` 是已完整处理、确认的 **non-response 事件数**。`delta_encoding` 和 resume-token 控制事件也计数；多条 `data:` 仍只计一个事件。不是字节、行数、空行数、SSE id 或 HTTP response 数。
3. 本项目同一 turn 最多自动恢复一次，保留账号、trace 和 encoding 状态，初始流与恢复流共享一个 monotonic 总预算；恢复 POST 使用剩余预算，不再提交 `messages` 或 prompt。
4. 正常 EOF 本身不伪装为 `[DONE]`；由既有同会话结果查询处理。明确传输失败后，有可信 conversation／输出 ID 才继续恢复结果。
5. 终止性 4xx（包括鉴权、参数校验和配额）保持原 HTTP 诊断；408／409／425 与服务端瞬态错误可退回同会话查询。普通任务 TimeoutError、取消、协议／回调／内容策略／工具配额错误不当成传输断线吞掉。
6. 控制 token 不进入日志、进度文本或持久化 checkpoint；业务层只接收安全的 conversation checkpoint。
7. 迭代器使用 monotonic 时间、有界队列、stop／close 和有界 join；离线 fake 响应支持 close 解除阻塞。没有据此宣称所有真实网络实现的后台线程都已被真机验证。

## 6. 调用层：不要把“换账号重新生成”叫续传

- SSE 已知会话／输出 ID 后断线，优先原账号、原会话的查询／下载；失败仍保留 checkpoint，不能因为文本回包或后期轮询超时而再次提交原 prompt。
- 按 prompt 找回的候选必须使用**请求开始时刻**、上下时间窗、实时排除集及原子 claim；所有恢复分支共用该条件，成功后同步任务 checkpoint 和上层已接受标记。
- 不再因为没有标题匹配就盲取“最新会话”；无时间信息、无有效关键词匹配或并列候选时不猜。
- 标题／关键词仍然只是启发式，不能当作严格所有权证明；atomic claim 减少并发交叉采用，**不等于 exactly-once**。完全丢失服务端确认且没有可信 ID 的情况，仍有不确定性。
- `input_message` 是输入参考图事件；即使工具已启动，也不把它的上传 file／sediment 指针当成输出图片。
- 进度与 checkpoint 回调在读取异常捕获范围之外，保留原错误；后期等待遵守同一个任务预算。

## 7. 图片内容格式

PNG／JPEG／WebP／GIF 按原字节签名识别：

- 新保存路径使用实际扩展名，WebDAV save／sync 显式使用真实 Content-Type。
- 本地 HTTP 流式返回只读前 12 字节判断 MIME；远端每响应只取一次完整 bytes。
- 旧 `.png` URL 即使实际内容为 GIF／JPEG，也保持 URL／索引不变而修正响应 MIME；不批量迁移旧文件。
- 原始 bytes／base64 不转码，不丢失 GIF 帧、duration 或 loop；缩略图仍可用首帧 PNG。
- 未识别的旧 opaque 内容仍保留 PNG 兼容回退；本次没有增加 AVIF／SVG 支持或新的结果 JSON 字段。

## 8. 其它静态功能：确认存在，不推测已经可用

捕获代码可定位 library／reference-image、局部编辑 mention、markup／layer／speed 等功能分支。它们**不是本次要自动调用的新后端接口**：

- library CRUD 涉及用户资产状态；本次不创建、改名或删除资源。
- reference mention／选区操作有自己的 offset、metadata 和分支条件；不能把 URI 占位符直接塞到普通 prompt 里当作完整编辑协议。
- Codex／共享组件分支不一定等价于普通 ChatGPT 网页请求。
- GIF speed UI 证明有后处理代码，不能证明它是生图触发条件或当前账号权限。
- markup／layer 路径存在，不能证明它们是 2.6 新增，也不能证明 SVG 导出已成功。

### 图片编辑协议 `dalle.from_client.operation`

捕获模块 `r3oB`（[`956483.8b486f09f8.js`](<../.verify/cgpt26/js/956483.8b486f09f8.js>)）中，图片编辑走独立的 `dalle` 字段而不是 `picture_v2`。**三种 operation 形状**（按出现顺序）：

```js
// A. 透传客户端已有的 operation 对象（编辑器产出）
dalle:{from_client:{operation:eY.operation}}

// B. 同一路径的另一分支
dalle:{from_client:{operation:eQ}}

// C. 从已生成图片做变换（"编辑这张图"）
{type:"transformation",
 original_file_id: ad,
 original_gen_id: nM?.chatGptImageMetadata?.generationId
                ?? nM?.chatGptGenerationId ?? ""}

// D. 标注画笔（markup）：额外带输入指针与 is_markup 标记
{type:"transformation",
 original_file_id: d.id,
 original_gen_id: r.chatGptGenerationId ?? void 0,
 input_image_asset_pointer: c.imageAssetPointer.asset_pointer,
 is_markup: !0}
```

**关键语义 —— 二者互斥。** 捕获代码在附加 `dalle` 后立刻从 `system_hints` 里剔除 `picture_v2`：

```js
null!=eY&&(a_={...a_,dalle:{from_client:{operation:eY.operation}}})
          .system_hints&&(a_.system_hints=a_.system_hints.filter(e=>"picture_v2"!==e))
```

所以同时发 `dalle` 和 `picture_v2` 与真实客户端行为矛盾。本批实现据此让 `dalle_operation` 非空时 `system_hints` 为空、`dalle` 键照发；不传该参数时请求体与之前**逐字节一致**。

同一模块另有参考图选择字段，与 `dalle` 并列（**本批未实现**）：

```js
N.user_selected_reference_image_ids = [P.id]      // 选中的参考图文件 id
// 另：library_folder_references / shared_library_file_references 在发送前被删除
```

本批实现：当 `references` 非空时，在 `metadata` 中同时设置 `attachments` 和 `user_selected_reference_image_ids`，与捕获的客户端行为对齐。

以及文件用途枚举，说明 mask 与普通上传走不同的 folder/useCase：

```js
A ? Y="image_gen_upload_input"    // 普通图生图输入
  : _ ? Y="image_edit_mask"        // 编辑蒙版
  : …
A ? Q="image_tag" : _ ? Q="dalle_agent" : Q="my_files"
V = A ? "reference_image" : K
```

**证据边界：** 以上均为静态读取，**未用真实账号验证**（本轮账号 `access_token` 全部失效、刷新失败）。不能据此断言上游一定会接受 `is_markup`、`image_edit_mask` 或上述 operation 组合，也不能断言它们属于某个版本新增。

### GIF：端点已确认，可用性仍未验证

捕获代码中的 GIF 端点为：

```http
POST /image-gen/gif/select_speed
{"conversation_id":"...","message_id":"...","asset_pointer":"...","selected_speed":<number>}
```

前端对 404／405 静默处理（视为后端未提供该能力）。该端点语义是**生成后切换速度**，不携带"请生成 GIF"的意图；GIF 的判据读取自结果消息的 `mime_type == "image/gif"`、`metadata.generation.animation` 和 `gif_speed_variants`。

2026-11-06 尝试真实生成一次：本地服务正常启动、请求进入上游，但 5 个注册免费账号的 access_token 全部过期且刷新失败（`curl: (77)` 证书验证错误），接口返回 429。**没有产出 GIF，因此"能否生成 GIF"仍然未知**；不要引用本轮作为 GIF 或 2.6 可用性的证据。

### 公开模型 ID

本批**新增** `gpt-image-2.6` 与 `gpt-image-2.5` 并行列于 `/v1/models`，两者上游 slug 相同（`auto` + `picture_v2`）。**2.5 未删除**——没有证据表明上游已淘汰它。新增 ID 只改变对外名称与目录项，不改变请求内容，也不构成 2.6 已被上游启用的证明。

`model: "auto", modalities: ["image"]` 与 Responses 的 chat-model + image_generation tool 路由兼容问题没有顺手改动；它们需要单独的 API 兼容性决策。

## 9. 验证、配置保护与复现

本次验证使用 `.verify/transport-update/.venv`、临时数据／配置，以及测试入口的外部网络禁用：原生 curl 仅允许已核对的字面回环 IP 夹具，禁用环境代理和重定向，拒绝真实服务端口。不收集需要真实本地服务的 v1 测试模块，不消费账号配额、不刷新／注册真实账号、不调用真实 WebDAV 或删除远端会话。

```powershell
& '.verify\transport-update\.venv\Scripts\python.exe' -X utf8 -B '.verify\transport-update\run_tests.py' --syntax --all-offline
```

测试环境的公开依赖准备与**离线测试执行**分开：Windows IANA 时区库需要 `tzdata`；tokenizer 静态文件预先缓存到工作区。项目 lock、系统 Python、用户 config 与账号数据不因测试安装而修改。

当前运行的实际统计以 [UNRELEASED](../UNRELEASED.md) 的“本次验证”记录为准；历史测试数字不能当成本次执行结果。备份／protected hash 基线和本地回滚脚本位于 `.bak/20261105_223753/` 与 `.verify/transport-update/`。

如重做公开页面下载，代理口令仅从环境变量读取，不把它写入文档或脚本，也不打印：

```bash
curl --proxy "$CHATGPT_PROXY" --fail --location 'https://chatgpt.com/' --output homepage.html
```

原文曾包含代理凭据，本版已删除；旧备份可能仍含该内容，须保持本地忽略，勿提交或分享，必要时由用户自行轮换口令。本次未自动更改代理配置。
