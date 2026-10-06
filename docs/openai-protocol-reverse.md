# OpenAI 协议全量逆向

证据来源标注：**[实测]** = 本次对现网抓取/测量；**[静态]** = 代码走查，附 `文件:行号`。
分析基线：HEAD `b368b6a`，工作区含未提交改动。

---

## 1. 一次生图的真实协议时序 [实测]

对池内账号打探针（hook `curl_cffi.Session.request`），真实编排 `stream_conversation(system_hints=["picture_v2"])`：

```
 1. GET   http://ip-api.com/json/?fields=status,countryCode,timezone   200    1666ms
 2. GET   https://chatgpt.com/                                         200   11412ms   _bootstrap()
 3. POST  /backend-api/sentinel/chat-requirements/prepare              200    3630ms
 4. POST  /backend-api/sentinel/chat-requirements/finalize             200    1754ms
 5. POST  /backend-api/f/conversation/prepare                          200    1639ms   -> conduit_token
 6. POST  /backend-api/f/conversation                                  200    7174ms   -> 13 个 SSE 帧
                                                       总计 40.0s / 6 次请求
SSE 内命中: conversation_id x10, multimodal_text x1, image_asset_pointer x1, server_ste_metadata x1
```

关键结论：**正常路径不轮询**。图片的 `asset_pointer` 就在 SSE 流里返回；`_poll_image_results` 是兜底路径。
第 1 发是**非 OpenAI 的外部调用** `ip-api.com`，用来把出口 IP 映射成时区/语言（`utils/egress_locale.py`），
第 2 发耗时 11.4s 是整条链路最大的固定开销，且结果按 `_POW_BOOTSTRAP_TTL_SECS` 缓存，命中则跳过 [静态: openai_backend_api.py:2509-2543]。

---

## 2. SSE 载荷协议：JSON-Patch over SSE [实测]

不是命名事件流，而是 RFC 6902 补丁流。首帧固定为版本协商串 `"v1"`，末帧 `[DONE]`。

帧类型（`{"type": ...}`）：
`resume_conversation_token`、`message_marker`、`server_ste_metadata`、`title_generation`、
`message_stream_complete`、`beacon_ui_response`

消息帧 `{"p":<path>, "o":<op>, "v":{...}}`，实测 op 集合：`add`(path `""` 根插入)、`replace`(`/message/status`)、`patch`、`append`(`/message/content/parts/0`)。

消息 `content_type` 全集（4 份抓包）：
`model_editable_context`、`code`、`reasoning`、`reasoning_recap`、`text`、`multimodal_text`、`system_error`。

成功出图 = `author.role=="tool"` + `content_type=="multimodal_text"` + `parts[].asset_pointer` 为 `sediment://…`。

### Conduit resume 令牌内部结构 [实测]

`resume_conversation_token` 携带 ES256 JWT，解出：

```json
{"conduit_uuid":"59acf25b…","conduit_location":"10.130.44.218:8306",
 "cluster":"unified-128","iat":1791149028,"exp":1791163428,
 "turn_topic_id":"conversation-turn-a4ee478c-…"}
```

`exp - iat = 14400s = 4h`。`conduit_location` 是内网地址、`cluster` 形如 `unified-{72,128,129,197}`，
即 OpenAI 内部 Conduit 分片拓扑直接下发给客户端，用于断线续传。

### 图片资源 ID 提取规则 [静态]

```python
FILE_SERVICE_ID_RE      = r"file-service://([A-Za-z0-9_-]+)"
REAL_IMAGE_FILE_ID_RE   = r"\bfile_00000000[a-f0-9]{24}\b"   # 过滤 file_upload_business_upsell 之类
SEDIMENT_ID_RE          = r"sediment://([A-Za-z0-9_-]+)"
```

`extract_conversation_ids()` 直接对 SSE 原文正则扫描；`update_conversation_state()` 有一条关键守卫：
**user 消息里含上传的输入图，绝不能当成生成结果**，故只在 `tool_invoked` 已置位且非 user 消息时接受 ID
[静态: protocol/conversation.py:604-660]。

---

## 3. Sentinel / PoW / Turnstile / Arkose

### 双实现

| | `utils/pow.py` | `utils/sentinel.py` |
|---|---|---|
| 用途 | chatgpt.com 生图/对话 | auth.openai.com 注册登录 |
| 配置长度 | 26 | 18 |
| config[0] | `width+height`（整数和） | `"1920x1080"`（字符串） |
| config[11] | `__reactContainer$fzelfjyxej8` 等 | `location`/`implementation` |
| sdk 来源 | 从首页 `<script src>` 动态解析 | 硬编码 `sentinel/20260124ceb8/sdk.js` |
| 前置 token | `gAAAAAC`+b64 | 同 |

### PoW 求解（两套共用同一哈希）[静态]

```python
h = 2166136261
for c in s: h ^= ord(c); h = (h * 16777619) & 0xFFFFFFFF   # FNV-1a 32
h ^= h >> 16; h = (h * 2246822507) & 0xFFFFFFFF
h ^= h >> 13; h = (h * 3266489909) & 0xFFFFFFFF
h ^= h >> 16                                             # 终态混合 → 8 位 hex
```

- nonce 写 `config[3]`，求解耗时 ms 写 `config[9]`
- 接受判据 `digest[:len(difficulty)] <= difficulty` —— **字符串前缀比较**，不是数值比较（这是最容易写错的地方）
- 成功 token：`"gAAAAAB" + b64(config) + "~S"`（`~S` = 同步求解标记）
- 上限 500000 次；失败落固定前缀 `wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D` + `b64("e")`
- 静态预序列化切片 `static_1/2/3`，每轮只拼 nonce 与耗时，避免重复 JSON dump

### 请求头 [实测]

```
OpenAI-Sentinel-Chat-Requirements-Token: gAAAAAB…
OpenAI-Sentinel-Proof-Token:             gAAAAAB…
OpenAI-Sentinel-Turnstile-Token:         <可选>
OpenAI-Sentinel-SO-Token:                <可选, finalize 下发>
```

requirements token **不可复用缓存**，复用必 403，因此每次重新 prepare+finalize
[静态: openai_backend_api.py:2544-2549]。

注册侧 sentinel 走 `POST https://sentinel.openai.com/backend-api/sentinel/req`，
`flow` 实际取值 [实测 grep]：`authorize_continue`、`username_password_create`、`email_otp_validate`、
`oauth_create_account`、`login_password`；响应 `token` 同时作为头里的 `c` 与 cookie `oai-sc = "0" + token`。

`arkose.required=true` → `ArkoseRequiredError`，**无解决方案**，直接换号/换出口。

### PoW 被拒的自愈 [静态]

`is_sentinel_proof_rejected()` 命中且是首轮 → `reset_pow_bootstrap_cache()` 丢弃
`sdk.js URL / data-build` 快照并重解析一次（`openai_backend_api.py:2491-2500`）。

---

## 4. 限流模型与时间窗口 [实测]

### `conversation/init` 响应全量 schema

```json
{
 "type": "conversation_detail_metadata",
 "banner_info": null,
 "blocked_features": [],
 "model_limits": [],
 "limits_progress": [ {"feature_name":"…","remaining":int,"reset_after":"ISO8601"} x5 ],
 "default_model_slug": "auto",
 "intended_default_model_slug": "auto",
 "atlas_mode_enabled": null,
 "file_attachment_limits": {"max_size_mb": 512}
}
```

### 核心规律：满额特征的 `reset_after` 是服务端合成的

单变量对照（两次采样间隔 8.22min，唯一变量 = 请求时刻）：

```
khpbgvrm34rz  deep_research:+0.00min  file_upload:+0.00  reason(50,满):+0.00  image_gen:-8.22min
4ryagu8u96s8  deep_research:+0.00min  file_upload:+0.00  reason(160,满):+0.00 image_gen:-8.22min
005rf9gpke65  deep_research:+0.00min  file_upload:+0.00  reason(45,耗):-8.22  image_gen:-8.22min
```

- `remaining == cap` → `reset_after = 请求时刻 + 整数窗口`（无历史信息）
- `remaining < cap` → `reset_after` 是真实锚定时刻，随墙钟倒计时

旁证：同一次响应里满额特征微秒递增 `.113783 → .113797 → .113808`（服务端按特征循环合成），
真实锚定的 `reason` 是独立微秒基 `.258826`。

### 由此推出的窗口表

| 特征 | 标准档 | 低档 | 上限 |
|---|---|---|---|
| `image_gen` | 24h | 24h | **25** |
| `reason` | 3h | 5h | 160 / 50 |
| `file_upload` | 3h | 24h | 80 / 15 |
| `paste_text_to_file` | 24h | 24h | 3 |
| `deep_research` | 30 天 | 30 天 | 5 |

### 发行上限可被反推

若某号 `reset_after - created_at ≈ 24.000h`，说明首次刷新（注册后约 1.8s）时 `image_gen` 满额，
当时的 `remaining` 即发行上限。42 个可定版账号：**cap=25 → 39 个(92.9%)，cap=10 → 2 个，cap=5 → 1 个**。
三个低 cap 账号 `success=0`，即发行即低档。全库 `image_gen.remaining` 从未出现 50。

### 闸门与账本解耦

`blocked_features[].limit` 实测 `25.0`（其他号段见 `5.0`、`2.0`，说明按号分档）。
`limits_progress` 是**账本**，闸门关闭后仍会继续播报剩余额度——实测 `remaining=20` 而账号已在 5 张处硬拒
[静态: openai_backend_api.py:800-810 注释]。故调度必须优先看闸门。

### 工具拒绝的两类载荷

| 层 | 判据 | 原文 | 处理 |
|---|---|---|---|
| L3 冷却 | `role=tool` + `content_type=text` + 含 `too quickly`/`rate limits in place` | `You're generating images too quickly. … Please wait for an hour …` | `ToolCooldownError`，回落 3600s |
| L4 配额 | `content_type=system_error` + `name=ChatGPTAgentToolRateLimitException` | `你已达到 Free 套餐的图像生成请求上限。上限将在 10小时 后重置…` | `ToolQuotaError`，按文本解析窗口 |

**绝不匹配 `author.name`** —— 它是每次构建轮换的混淆哈希（`t2uay3k.sj1i4kz`）。
重置窗口解析：`(\d+)\s*(小时|hours?|hrs?)` 优先，次选分钟，无时长返回 0 让调用方用自己的默认值。

### 实测吞吐

连续 8 张零间隔串行：**8/8 全成功**，单张 15.3–25.8s。选号键 `(在途数↑, 剩余额度↓, token)`
[静态: account_service.py:1778]，串行下恒命中最高额度号，故单账号连吃 7 张（23→16）无一次拒绝。
代码里不存在任何自建的账号间隔或冷却。

---

## 5. size / quality 不是协议参数 [实测]

Web 生图链路**没有尺寸/质量字段**，它们是拼进 prompt 的中文自然语言：

```python
hints.append(f"输出图片尺寸为 {size}。")
hints.append(f"输出图片质量为 {quality}。")
```
[静态: protocol/conversation.py:306-312]

实测效果（同账号）：

```
请求 size=1024x1024  -> 实际 PNG 1254x1254   736KB     ← 被模型自有方形分辨率覆盖
请求 size=1536x1024  -> 实际 PNG 1536x1024  1136KB     ← 生效
```

即提示词式尺寸**只是建议**，非标准比例才显现，方形会被模型默认值吃掉。

Codex 链路则携带**真结构化参数** [静态: openai_backend_api.py:1338-1364]：

```json
{"model":"gpt-5.5","instructions":"Use the image_generation tool to create exactly one image…",
 "store":false,"stream":true,
 "tools":[{"type":"image_generation","model":"gpt-image-2",
           "action":"generate|edit","size":"1024x1024","quality":"auto","output_format":"png"}],
 "tool_choice":{"type":"image_generation"}}
```

`POST /backend-api/codex/responses`，仅需 `Authorization: Bearer` + `Content-Type`，
不需要 conduit_token / sentinel proof。

---

## 6. 凭据体系

### JWT claims 实测（access_token，RS256）

```
header: {"alg":"RS256","kid":"n0z6Pr1-tB-17WoU4Tc98zuD0k6lyaMYBbwJAD8kRVs"}
aud: ["https://api.openai.com/v1"]        client_id: app_X8zY6vW2pQ9tR3dE7nK1jL5gH
iss: https://auth.openai.com              sub: auth0|pp8kHGi9zBoqdLcNUZzXMwdl
scp: [openid,email,profile,offline_access,model.request,model.read,
      organization.read,organization.write]
sl: true                                  pwd_auth_time: 1790166960350
https://api.openai.com/auth:
    amr: ["otp","urn:openai:amr:otp_email"]        chatgpt_plan_type: free
    chatgpt_account_id: b8afb84a-0a7d-…            chatgpt_user_id: user-DQ24VcuCa3…
    chatgpt_account_user_id: user-…__b8afb84a-…    chatgpt_compute_residency: no_constraint
    chatgpt_login_finalizer_auth_session_id: authsess_Iox4qAvrcc4z0cx5v34SuCDP
https://api.openai.com/profile: {email, email_verified, name}
exp - iat = 864000s = 240.00h = 10 天
```

`session_token` **不是 JWT**，是 JWE：`{"alg":"dir","enc":"A256GCM"}`，不透明、不解析。
cookie 名 `__Secure-next-auth.session-token`（回退 `next-auth.session-token`）。

### 三个 client_id（关键）

| client_id | 归属 | 出处 |
|---|---|---|
| `app_X8zY6vW2pQ9tR3dE7nK1jL5gH` | chatgpt.com Web（注册链路） | `gpt_free_register/…/constants.py:60` |
| `app_2SKx67EdpoN0G6j64rFvigXD` | 平台 OAuth / 刷新 | `account_service.py:45`、`openai_oauth.py:3` |
| `app_EMoamEEZ73f0CkXaXp7hrann` | Codex 客户端 | `constants.py:67` |

**因果闭合**：刷新请求以 `client_id=app_2SKx…` 发 `grant_type=refresh_token`
[静态: account_service.py:682-720]，而注册号的 JWT 由 `app_X8zY…` 签发 —— 签发方与刷新方 client_id 不一致。
实测远端 8 个注册号 **`refresh_token` 持有数 0/8**，而 `session_token` 8/8：
注册号的续期只能走 `session_token → GET /api/auth/session → accessToken → /me 200 验收` 这一条路。

### 刷新端点 [静态]

```
POST https://auth.openai.com/oauth/token
Content-Type: application/x-www-form-urlencoded
  grant_type=refresh_token & refresh_token=<rt> & client_id=app_2SKx67EdpoN0G6j64rFvigXD
```
body 内**没有** scope / redirect_uri / audience；注册引擎的另一实现额外带
`redirect_uri=https://chatgpt.com/api/auth/callback/openai`。
成功取 `access_token`(必需)、`refresh_token`(缺省沿用)、`id_token`；
**先落盘再改内存，落盘失败整体回滚**，轮换时旧 token 进 `_token_aliases` 并迁移在途计数。

### 关键常量 [静态]

`_ACCESS_TOKEN_REFRESH_SKEW_SECONDS=24h`、`_IMAGE_PROBE_JWT_MIN_REMAINING=300s`、
`_REFRESH_TOKEN_KEEPALIVE_SECONDS=3天`、`_REVOKED_COOLDOWN_SECONDS=3600`、
`_TOKEN_REFRESH_ERROR_BACKOFF_SECONDS=300`、`_SESSION_ONLY_PROBE_STALE_SECONDS=1800`。

### 存活性权威闸

`/backend-api/me` 是唯一判据：**200 活 / 401|403 死 / 其他及网络错 = 未知(None)**。
`/api/auth/session` 会持续返回同一个已死 JWT，故设三道闸：
新 token 必须 `/me` 200；新==旧 且验活失败 → `session_refresh_stale_token_revoked`；
新!=旧 但验活失败 → `session_refresh_token_still_invalid`。

硬 revoke 串（小写匹配 `last_refresh_error|last_token_refresh_error`）：
`token invalidated (/backend-api/me)`、`token_revoked`、`invalidated oauth`、
`session_refresh_stale_token_revoked`、`session_refresh_token_still_invalid`、
`refreshed_token_still_invalid_on_me`、`authorize_failed_403`、`password_verify_failed_403`、
`app_session_terminated`、`invalid_access_token`、`无可用续期手段`。

注释明示的分寸：*"Prepare 401 after TLS/device-id churn is not a hard revoke."*
—— prepare 的 401 常是新 device-id，不是配额用尽，不能直接杀号。

---

## 7. 设备身份与出口一致性

`fp` 恰好 10 键（与 `data/accounts.json` 全量并集一致）：
`user-agent`、`impersonate`、`oai-device-id`、`oai-session-id`、`sec-ch-ua`、`sec-ch-ua-mobile`、
`sec-ch-ua-platform`、`screen_width`、`screen_height`、`hardware_concurrency`。

实测 8 号：`impersonate` 7×chrome142 + 1×chrome136；屏幕 1920x1080 / 1440x900；
核数 8/16/32；`oai-device-id` 全部稳定复用。

落盘时机：`__init__` 内即 `_persist_client_identity()`，把 device/session id + 整个 `fp` +
当轮代理钉进账号行。理由（代码注释原文）：
*"Pin device/session ids on the account so TLS retries reuse the same browser"*；
*"Falling through to a shared runtime/global/direct IP looks like a new device"*；
CHANGELOG：换 `OAI-Device-Id` 会导致第二次生图废号。

PoW 与指纹联动：`_pow_profile()` 把屏幕/核数/`session_id` 注入 `build_pow_config`，
保证 PoW 上报的伪浏览器参数与请求头 `sec-ch-ua` 自相一致。

出口地域联动：`utils/egress_locale.py` 用 `ip-api.com` 把出口 IP 映射为
时区/语言/`new Date` 显示格式，喂给 PoW `config[1]` 与 `timezone`/`timezone_offset_min`，
避免出现「日本出口 + 德国时区 + 中文日期」这类自相矛盾指纹。

---

## 8. 账号侧端点实测 schema

`GET /backend-api/me`：
```
object:user  id:user-DQ24VcuCa3…  email  name  created:1790166959
mfa_flag_enabled:true  amr:["otp"]  email_domain_type:unknown  ads_segment_id:6812
client_id:app_X8zY6vW2pQ9tR3dE7nK1jL5gH
country:DE  region:Bavaria  region_code:BY          ← 出口国家直接反映在这里
orgs.data[0]: {id:org-rHHQM…, title:"Personal", personal:true, role:"owner", is_default:true,
               settings:{threads_ui_visibility:"NONE", usage_dashboard_visibility:"ANY_ROLE",
                         disable_user_api_keys:false, completed_platform_onboarding:false}}
```

`GET /backend-api/accounts/check/v4-2023-04-27[?timezone_offset_min=…]`：
```
plan_type:"free"                 structure:"personal"
account_id:b8afb84a-0a7d-49cb-8503-75a0cc36555d     ← 即 ChatGPT-Account-Id 头
account_user_id:user-…__b8afb84a-…                  account_owner_id:user-DQ24…
created_time:"2026-09-23T12:36:00.869807Z"
account_residency_region:"no_constraint"            account_compute_residency:"no_constraint"
has_previously_paid_subscription:false  is_trial:false  is_view_only:false
trial_state:null  is_most_recent_expired_subscription_gratis:false
processor:{a001:{has_customer_object:false}, b001:{…}, c001:{…}}
tbo_config:{plan_eligible:false, tbo_available:false}
```
真正参与状态判定的只有 `account_id` 与 `plan_type`；`is_deactivated`、
`has_active_subscription`、`subscription_plan`、`account_user_role` 仅进日志。

`get_user_info()` 组装顺序（串行，注释说明 curl_cffi Session 非线程安全）：
`/me` → `POST /conversation/init` → `/accounts/check`，产出
`quota`/`restore_at` ← `image_gen.remaining/reset_after`；
`image_gate` ← `blocked_features[name==image_gen]`；
`image_gate_probe_ok = isinstance(blocked_features, list)`（缺字段不能读成"无闸门"）；
`status = "限流" if quota == 0 else "正常"`。

---

## 9. 文件上传与下载

### 上传（编辑参考图）三段式 [静态: openai_backend_api.py:1479-1537]

```
1) POST /backend-api/files
   {"file_name","file_size","use_case":"multimodal","width","height"}
   -> {"file_id","upload_url"}
2) PUT  <upload_url>            # Azure Blob 直传，不带 chatgpt.com 鉴权
   Content-Type: <真实 mime>
   x-ms-blob-type: BlockBlob
   x-ms-version: 2020-04-08
3) POST /backend-api/files/{file_id}/uploaded   body {}
```

随后消息体里以 `{"content_type":"image_asset_pointer","asset_pointer":"file-service://<file_id>",
"width","height","size_bytes"}` 引用，并在 `metadata.attachments` 重复一份元数据。

### 下载双通道 [静态]

`needs_chatgpt_file_auth(url)`：host ∈ {chatgpt.com, chat.openai.com} 且
path ∈ {`/backend-api/estuary`、`/backend-api/files/`、`/backend-api/conversation/`} → 必须走**主 session 鉴权下载**；
仅靠签名 `sig` 不够，匿名 GET 返回 `{"detail":"File stream access denied."}`
（`is_file_stream_denied` 命中 403 后回落到主 session）。
其余 CDN 直链走独立 `_resource_session`（只带 UA + `Accept: image/*`，绑 `resource_proxy_url`），
使主 session 保持钉在 chatgpt.com。

> 已知缺口：`_resource_session` 与 `get_with_egress_fallback` 都不经 `build_headers`，
> 因此 **cf_clearance 不注入资源/CDN 下载**。

---

## 10. 请求头全集 [实测]

```
User-Agent / Origin: https://chatgpt.com / Referer: https://chatgpt.com/
Accept-Language: zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7
Cache-Control: no-cache   Pragma: no-cache   Priority: u=1, i
Sec-Ch-Ua / Sec-Ch-Ua-Arch:"x86" / -Bitness:"64" / -Full-Version / -Full-Version-List /
-Mobile / -Model:"" / -Platform / -Platform-Version:"19.0.0"
Sec-Fetch-Dest: empty  -Mode: cors  -Site: same-origin
OAI-Device-Id / OAI-Session-Id / OAI-Language: zh-CN
OAI-Client-Version: prod-a194cd50d4416d3c0b47c740f206b12ce60f5887
OAI-Client-Build-Number: 6708908
Authorization: Bearer <access_token>
ChatGPT-Account-Id: <uuid>
X-OpenAI-Target-Path / X-OpenAI-Target-Route: <与真实 path 完全一致，由 _headers() 自动注入>
```

`_bootstrap_headers()` 是另一套（`Sec-Fetch-Dest: document`、`-Mode: navigate`、`-Site: none`、
`Upgrade-Insecure-Requests: 1`），用于第 2 发的首页预热。
TLS 层由 `curl_cffi` 的 `impersonate` 提供（实测 `chrome142`/`chrome136`）。

---

## 11. 注册 / 登录协议（auth.openai.com）

`api/accounts/*` 端点全集 [实测 grep]：
`authorize`、`authorize/continue`、`create_account`、`user/register`、`password/verify`、
`passwordless/send-otp`、`email-otp/send|resend|validate`、`phone-otp/resend|validate`、
`add-phone/send`、`mfa/verify`、`organization/select`、`workspace/select`、`session/select`、
`client_auth_session_dump`。

页面路由（302 状态机跳转依据）：`/log-in`、`/log-in/password`、`/create-account`、
`/create-account/password`、`/email-verification`、`/phone-verification`、`/about-you`、
`/add-phone`、`/mfa-challenge`、`/choose-an-account`、`/authorize`、
`/oauth/authorize`、`/oauth/token`、`/sign-in-with-chatgpt/codex/consent`。

`POST /api/auth/signin/openai` 带客户端生成的 `auth_session_logging_id=<uuid>`，
服务端原样透传进后续 `authorize` URL（纯打点关联 id，无回读校验）。

### 当前阻塞

远端 20 个注册任务 `added=0`：`获取验证码失败` ×22、`获取 Device ID 失败` ×7、
`初始化会话失败: oai_did_missing` ×5、CF 403 ×1、`unsupported_country_region` ×1。
本地出口 SOCKS `:19053` 回 `User was rejected by the SOCKS5 server (1 1)`。
`等待验证码超时 (1s)` 是误导性日志：`remaining = max(1, int(deadline - time.time()))`
使最后一轮必然显示 1s，实际是 300s 预算耗尽。

---

## 12. 轮询兜底路径 [静态]

仅当 SSE 未带回 ID 时进入，全部 sleep 严格受 `timeout_secs` 约束：

```
image_poll_initial_wait_secs = 4 (+抖动, 上限 2s)   # SSE 无 ID 时才等
image_poll_sleep_secs:  <15s → 2s | <28s → 4s | <45s → 7s | 之后 → interval(5s)
image_poll_timeout_secs = 120
image_settle_secs       = 2      # SSE 已带回 ID 时的沉降等待
429/5xx/网络错退避       = min(2^min(attempt,4), 16) + jitter，优先采用 Retry-After
```

SSE 后约 200ms 立即查文档会撞上游瞬时 429（文档尚未落库），故必须留初始等待。
`/backend-api/tasks` 只在临近超时（剩余 ≤12s）时补查一次，用于给超时错误附上 `task_error`。

内容政策拒绝走另一条关键词表（`内容政策`、`防护限制`、`违反`、`moderation`、`policy`、`blocked`、
`不能生成`、`无法生成`、`裸体`、`色情`、`未成年`、`抱歉，我不能`），命中即抛
`ImageContentPolicyError`，**不计入配额**。

---

## 13. 对外接口映射

模型名 → 内部 slug：`gpt-image-2.5` / `gpt-image-2` → `"gpt-5-3"`；`codex-gpt-image-2` → 原名；
其余 → `"auto"`。`system_hints=["picture_v2"]` 是**是否走图工具的唯一开关**——
含它则走 `_stream_picture_conversation` 专用图链路，否则走通用 conversation。

`size`/`quality` 见第 5 节（web 是提示词、codex 是真参数）。
`thinking_effort` 经 `_normalize_thinking_effort()` 归一后才写入 payload。
`global_system_prompt` 存在时前置拼接进 user prompt，并作为 `role:"system"` 注入 normalized messages。

---

## 14. 注册 / 登录状态机（15 步全序）

证据：`register.py` 行号 + 仓库内真实抓包 `data/gpt_register_logs/protocol_capture_full_20260906-023223.json`
（`status=registered`，15 个 HTTP 事件逐条对齐）。浏览器注册已被摘除
（`plugin.py:194-201` 抛 `"浏览器注册已移除，请使用 protocol + mailbox"`），执行器固定 `protocol`。

```
 0. GET https://cloudflare.com/cdn-cgi/trace            解析 loc=，命中 OPENAI_BLOCK_REGIONS(默认CN) 即终止
 1. GET https://chatgpt.com/                            种 oai-did（≤4 次 + 换 sticky 出口 ≤3 次）
 2. GET  /api/auth/csrf                                  -> csrfToken（空则回退 cookie __Host-next-auth.csrf-token）
 3. POST /api/auth/signin/openai?prompt=login
        &ext-oai-did=<did>&screen_hint=login_or_signup&login_hint=<email>
        &auth_session_logging_id=<uuid4>&ext-passkey-client-capabilities=1111
        body: callbackUrl + csrfToken + json=true       -> {"url":"<authorize>"}
 4. GET  <authorize>  (allow_redirects)                   ★靠 final_url 分流★
        final=https://auth.openai.com/email-verification -> 判 _otp_auto_sent
 5. POST sentinel.openai.com/backend-api/sentinel/req    flow=authorize_continue
        (auto-OTP 时可整体跳过: OPENAI_SKIP_CONTINUE_ON_AUTO_OTP=1)
 6. POST /api/accounts/authorize/continue
        body {"username":{"value":<email>,"kind":"email"},"screen_hint":"signup"}
        -> page.type + email_verification_mode + signup_mode 决定后续路径
 7. [仅密码路径] GET /create-account/password -> sentinel(username_password_create)
                 POST /api/accounts/user/register {"password","username"}
 8. 发码: GET client_auth_session_dump(刷 mode) -> 候选首个 200 即止
        新注册: POST passwordless/send-otp -> resend -> send -> send(GET)
        (OPENAI_TRUST_AUTO_OTP=1 时整体跳过：二次 send 会 mint 新 challenge 使在途码失效)
 9. 收码: 分片轮询 + 每片 GET client_auth_session_dump 保活；影子码 493682 忽略
10. POST /api/accounts/email-otp/validate {"code":…}     每次前重取 sentinel(email_otp_validate)
11. GET  <continue_url>  -> /about-you
12. GET client_auth_session_dump -> sentinel(oauth_create_account)
    POST /api/accounts/create_account {"name","birthdate"}  -> continue_url 带 code=ac_…
13. GET  /api/auth/callback/openai?code=ac_…             落 __Secure-next-auth.session-token
14. GET  /api/auth/session                               -> accessToken
15. [默认关闭] Codex 分支换取 refresh_token
```

**分流不靠响应码，靠 302 后的 `final_url` + `page.type` + `oai-client-auth-session.email_verification_mode`。**

### 关键结论：默认配置下注册链路拿不到 refresh_token

`OPENAI_SKIP_CODEX` 默认 1，终点是 NextAuth 的 `accessToken`，`result.refresh_token=""`
（`register.py:3202-3208`）。这与第 6 节实测「远端 8 号 `refresh_token` 0/8」完全闭环。
要 refresh_token 必须开 Codex 分支或跑 `codex_upgrade.py`，而 free 号高概率 `add_phone` 软失败。

且默认 NextAuth 链路**不持有 `state`/`code_verifier`**（PKCE 由 chatgpt.com 后端持有，
`register.py:795-800`），因此 `oauth.py:293-295` 的 state 校验在该链路不可用。

### authorize 上由服务端注入的参数

抓包 event 4 实证：`audience=https://api.openai.com/v1`、`device_id`、
`ccaps=login_methods chatgpt_login_finalizer_v1`、`auth_return_target_category=chatgpt_home`、
`state=b-On6S3py-…`、`response_type=code` —— **代码里不存在，全部由 NextAuth 服务端注入**。

### 未实现项（显式声明，非推测）

`mfa/verify`、`phone-otp/*`、`add-phone/send`、`organization/select`、`session/select` 全仓**无实现**；
遇手机验证只判 `page.type=="add_phone"` 后放弃。`unsupported_country_region_territory`
**注册引擎不处理**，仅 `account_service.py:1411` 的密码重登路径识别。
死代码：`OTP_MAX_ATTEMPTS`（`constants.py:153`）无引用。

### OTP 预算

总 300s；`_otp_wait_policy()`：auto-OTP∧passwordless → `(slice 25, resend 75, max 2)`，
否则 `(25, 50, 3)`。login-challenge 快速失败：`OPENAI_OTP_LOGIN_CHALLENGE_FAST_FAIL`(默1)、
`OPENAI_OTP_LOGIN_CHALLENGE_PROBE_SECS`(默35，下限5)，生效后 `total=probe, slice=min(12,probe), max_resends=1`。

---

## 15. PoW 三套实现的逐下标对照

同一 PoW 语义，仓库里有**三套** config，长度与形态都不同：

| idx | `utils/pow.py` (25，生图/对话) | `utils/sentinel.py` (18，密码登录) | `register.py:293-327` (19，注册) |
|---|---|---|---|
| 0 | `width+height` **数值** | `"1920x1080"` **字符串** | `"{sw}x{sh}"` 字符串 |
| 1 | 随出口时区的 `""+new Date` | **永远 UTC** | `"%a, %d %b %Y …"`（**带逗号**，与前两者都不同） |
| 2 | `4294705152` 硬编码 | 同 | 按 `device_memory` 推 heap，上限 4294705152 |
| 3 | nonce | nonce | nonce |
| 5 | 首页 HTML 解析的 sdk src | 写死 `20260124ceb8` | `20260219f9f6`（可 env 覆盖） |
| 7/8 | language / languages（随出口国家） | **7=null、8="en-US"（索引错位）** | null |
| 9 | 求解耗时 ms | 同 | 同 |
| 10 | 34 候选 navigator 探针 | 4 候选全 `-undefined` | `webkitTemporaryStorage−undefined` |
| 11 | `__reactContainer$fzelfjyxej8` | `location`/`URL`/… | — |
| 12 | 43 候选 window（含 `__NEXT_DATA__`） | `Object`/`parseFloat`/… | — |
| 14 | 账号级稳定 `sid` | **每次新 uuid4** | `device_id` |
| 16 | `[8,16,24,32]` | `[4,8,12,16]` | — |
| 18-24 | `0×6` + 浏览器厂商位 | — | — |

三处 `FNV-1a32` 与 500000 上限逐字节相同；`ord(ch)` 逐 **UTF-16 code unit** 而非字节，
不能改成字节级 FNV。

**一处被误判为缺陷、已核验澄清的差异（附核查过程，防止后人再踩）**：

`pow.py` 的 `config[0]` 是 `width+height` **数值**，另两套是 `"WxH"` **字符串**。
曾有推断认为"真实线上是字符串形态，pow 发裸数字是指纹破绽"，并引用抓包前缀
`gAAAAACWyIxOTIwe` → base64 解出 `["1920`。**该推断不成立，已撤回**，理由：

1. 全部 5 个抓包样本的 `p` 令牌**只存了截断前缀**（`{"len":611,"prefix":"gAAAAACWyIxOTIwe…<611>"}`），
   且**均来自 `POST sentinel.openai.com/backend-api/sentinel/req`**（注册/登录链路）。
   仓库内**不存在任何一份 `chatgpt.com/backend-api/sentinel/chat-requirements/prepare` 的请求体抓包**。
2. 解出的三个前缀 `["1920`、`["1366`、`["1680` 恰好对应 `utils/sentinel.py` 与 `register.py`
   自己的输出形态 —— 即**抓包只是复证了这两个生成器与自身一致**，对 `chatgpt.com` 的 prepare
   端点期望什么形态**没有任何证明力**。
3. 反向实测证据更硬：本次 8/8 连续成功生图**全部经过 `pow.py` 的数字形态**，
   `chat-requirements/prepare` 与 `/finalize` 均返回 200（第 1 节时序第 3、4 发）。
   若该形态被拒，链路会在 finalize 或 conversation 处 403。

结论：这是**端点间的形态差异**（`sentinel/req` 用字符串、`chat-requirements/prepare` 接受数值），
不是缺陷。若要将其中和定论，唯一有效证据是**抓一次真实 prepare 请求体**。

其余两处不一致是确实存在的：

1. **`config[10]` 分隔符不一致**：pow 用 **U+2212 MINUS SIGN**（实测码点 `0x2212`），
   sentinel 用 **ASCII `-`**。
2. **失败 fallback 语义冲突**：`pow.py` 算出 fallback 但 `build_proof_token` 直接抛错不外发；
   `utils/sentinel.py` 却**伪造**一个 `gAAAAAB` 前缀的"已解" token；注册引擎注释明确否定：
   *"Official sdk.js unsolved prefix …; do not emit a fake gAAAAAB."*

### 难度值实测

来自抓包 5 个样本：`0694e5`、`06e1cc`、`07101f`、`0716b4`、`073762`。
规律：**恒 6 位小写 hex、首位恒 `0`、次两位落 `6x–7x`**；`seed` 是 19 字符的 `0.xxxx…` 浮点串。
用真实值复算 `difficulty="06e1cc"` → 55 次迭代命中 `06c313 <= 06e1cc`。

---

## 16. Turnstile：dx 是一个字节码 VM

`dx = base64( XOR( JSON字节码, key ) )`，**异或密钥 `key` 就是本次自己生成的 requirements token 整串**
（`"gAAAAAC"+b64(config)`）。解出后是 `[opcode, *args]` 指令序列，`args` 是寄存器槽位下标。

`utils/turnstile.py` 的 opcode 表：`1`XOR、`2`立即数、`3`**输出 b64 token**、`5`JS 弱类型加、
`6`属性访问（特判 `window.document.location` → `https://chatgpt.com/`）、`7`调用、`8`拷贝、
`14/15`json 解/编、`17`**环境探针**、`18/19`atob/btoa、`20`条件调用、`23/24`调用与拼接。

`func_17` 返回的是**被校验的指纹本身**：`window.Object.keys(window.localStorage)` 返回固定 8 键
（含 `oai-did`、`STATSIG_LOCAL_STORAGE_V4`、`UiState.isNavigationCollapsed.1`）。

它是**精简 VM**：opcode `4/11/12/13/22/27/29`（reject / script / vmstate / catch / exec / splice / cmp<）
未注册，由 `except Exception: continue` **静默跳过**。完整 VM 在
`gpt_free_register/engines/platforms/chatgpt/sentinel_vm.py`。

触发条件：`prepare` 响应 `turnstile.required && turnstile.dx` 二者皆真（抓包 `dx` 长 20552 字符）。
产物同时进 `finalize` body 与 `OpenAI-Sentinel-Turnstile-Token` 头。
**`utils/sentinel.py` 链路把 `t` 恒置空，不做 turnstile 求解。**

---

## 17. Arkose

判定在 `prepare` 响应里且**位置早于 turnstile/proofofwork**，一旦要求即放弃整轮 finalize。
**全仓无求解实现**。

退化行为是**换号、不换代理/出口**：不 invalidate、不 park、不扣配额（与 `ToolQuotaError` 相对）。
全部账号命中 → `503 / code="upstream_arkose_required"`，文案要求运维手工换出口。
**文本链路无 arkose 分支**，命中即整单失败 —— 与图链路行为不对称。

---

## 18. Cloudflare 层与 clearance 的真实生效范围

获取两路（`services/proxy_service.py`）：`manual` 读 `cf_cookies`/`cf_clearance`；
`flaresolverr` 发 `POST {url}/v1 {"cmd":"request.get","url":…,"proxy":{"url":…}}`
—— **代理会传给 FlareSolverr 以保证 clearance 与实际出口同 IP**；而 FlareSolverr 自身请求
用 `ProxyHandler({})` 清空环境代理（否则 docker DNS 名被 SOCKS 黑洞）。

缓存键 `(proxy_url, host, 账号隔离id)`，隔离 id 取 `oai-device-id→email→account_id→user_id`
→ 同出口不同账号的 clearance **互不串用**。

### 已定位缺陷

1. **FlareSolverr 返回的 `userAgent` 永远不生效**：`build_headers` 只在缺 UA 时写入，
   而 `session.headers` 在构造期已写死 UA，`_headers()` 从它起步 ⇒ **只有 cookie 被注入**。
   而 `cf_clearance` 与 UA 是绑定的 → manual 之外的 clearance 有效性存疑。
2. **UA / `Sec-Ch-Ua`(Edge 143) / `impersonate`(chrome142) / FlareSolverr 浏览器四者互不校验**，
   唯一绑定维度是**出口 IP** + host。
3. **无 "403 → 自动刷 clearance → 重试" 闭环**：`refresh_clearance` 生产调用方仅
   `POST /api/proxy/clearance/test`（强制 `force=True`）；`invalidate_clearance` 生产无调用；
   `reset_session_status_codes`(默 `[403]`) 与 `clearance.warm_up_on_start` **全仓无消费者**（死配置）。
4. 主链路**无 CF challenge 页识别串**（`Just a moment`/`cf-chl`/…只在注册服务的错误启发式里）。
5. `_resource_session` 与 `get_with_egress_fallback` **不经 `build_headers`**
   ⇒ clearance 不注入资源/CDN 下载。

真正对抗 CF 的工程手段在注册引擎：粘性出口 `apply_sticky_session`（用户名加 `.<session_id>` 钉住单 IP，
注释实测「出口中途变化会被 Cloudflare 判成会话劫持 → 403 challenge + OAuth 409 invalid_state」）
与 `rotate_sticky_session`。

---

## 19. 出口指纹一致性

`resolve_egress_locale()` 优先级：env `OAI_CLIENT_TIMEZONE`/`OAI_CLIENT_COUNTRY` →
**经同一个代理**探测 `http://ip-api.com/json/`（明文，HTTPS 是付费档实测 403）
→ 兜底 `Asia/Tokyo`/`JP`。缓存键 = `proxy_url or "<direct>"`，TTL 6h。
**这就是第 1 节时序里第 1 发 `ip-api.com` 的来源。**

联动字段：`timezone` / `timezone_offset_min`（**取反**，东京 UTC+9 → `-540`）进
conversation body、`conversation/init`、生图 prepare/start；`config[1]` 日期串；
`config[7]/[8]` 语言。

**没有任何校验或告警**，探测失败静默降级且 6h 内不重探。已定位的错位：

1. HTTP 头语言**恒为中文**（`Accept-Language: zh-CN…`、`OAI-Language: zh-CN`），不随 locale
   → 出口在美国时 `navigator.language=en-US`(PoW) 与 `Accept-Language: zh-CN`(头) 自相矛盾。
2. PoW `config[10]` 写死候选 `"language−zh-CN"` 与 `"hardwareConcurrency−32"`，后者可能与 `config[16]` 实际核数矛盾。
3. `utils/sentinel.py` **完全不接 locale**（固定 en-US / UTC / 1920x1080）
   → 登录链路画像与对话链路画像可能来自不同"国家"。
4. locale 只在 `__init__` 解析一次，换出口必须重建实例；生图 failover 传 `force_proxy` 会重建，
   **文本链路不传**。

---

## 20. 令牌续期与账号处置

### 换取关系

| 起点 | 终点 | 路径 |
|---|---|---|
| `refresh_token` | `access_token`(+轮换) | `POST auth.openai.com/oauth/token` form，`client_id=app_2SKx…` |
| `session_token` | `access_token` | `GET chatgpt.com/api/auth/session` → `accessToken` → **`/me` 200 验收** |
| `code`+`code_verifier` | access/refresh/id | 仅 Codex / 平台 OAuth |
| `access_token` | `refresh_token` | **不存在此路径** |

优先级：`refresh_token` → `session_token` → `email+password`。

`/api/auth/session` 会**持续返回同一个已死 JWT**且不轮换 cookie，故设三道闸：
新 token 必须 `/me` 200；`新==旧` 且验活失败 → `session_refresh_stale_token_revoked`；
`新!=旧` 但验活失败 → `session_refresh_token_still_invalid`。

### 一个重要判据分寸（注释原文）

> *"Prepare 401 after TLS/device-id churn is not a hard revoke.
> Only confirmed /me (or explicit token_revoked) should kill the row."*

即 `prepare` 的 401 常是换了 device-id，不是配额用尽，**不能直接杀号**，需 `confirm_token_revoked` 再确认。

### 处置优先级

即使开启 `auto_remove_invalid_accounts`，**`free` + (有 `session_token` | `password` | `session_only`)
的账号仍然不删**（`account_service.py:2115-2145`）——这是号池不被误删的主力保护。

---

## 21. Codex 链路与一处死路

### 升级序列

`POST /api/accounts/codex-upgrade` →
`authorize?client_id=app_EMoamEEZ73f0CkXaXp7hrann&…&codex_cli_simplified_flow=true`（**走 Hydra `/oauth/authorize`**）
→ 取 `oai-did` → sentinel(`authorize_continue`) → `authorize/continue`(referer `/log-in`)
→ `email-otp/send` → 等码 → `email-otp/validate`
→ **`page.type=="add_phone"` 即软失败（free 号最常见结局）**
→ 手跟 ≤15 跳找 `code=`&`state=` → `POST /oauth/token`（form，`authorization_code` + `code_verifier`）。

落库关键：`_prepare_account_payload` 在 `export_type=="codex"` 时强制 `source_type="codex"`
（`account_service.py:2242`），这才是 `_ensure_codex_source_account` 的准入值。

### 生图请求

`POST /backend-api/codex/responses`，**唯一带真 `size`/`quality` 结构化参数的链路**
（`model=gpt-5.5` + `tools[0].model=gpt-image-2`，`action` 由有无参考图定 `generate|edit`）。
返回的 `image_generation_call.result` 是**内联 base64** —— 一次 HTTP 得成品，
**无 file id、无下载、无轮询**。

与 web 链路的本质差异：仅 `Authorization: Bearer`，**不需要** sentinel proof / conduit / turnstile / arkose；
但也**绕过 `_headers()`** ⇒ 不带 `ChatGPT-Account-Id` 与 `X-OpenAI-Target-Path/Route`。

### 死路（值得单独指出）

升级产物默认 `type=free`，而无前缀 `codex-gpt-image-2` 的选号强制 `plan_types=("plus","team","pro")`。
所以**「补完 refresh_token 就能生图」不成立**：`source_type=codex` 那道门开了，
套餐过滤仍会把号挡掉，最终落 429 文案
*"codex-gpt-image-2 requires imported Codex OAuth accounts (Plus/Team/Pro with refresh_token);
auto-registered free web accounts only serve gpt-image-2.5"*。

`sign-in-with-chatgpt/codex/consent` 在生效代码里**没有 POST 实现**（只作 referer 与死常量存在）。
上游 Codex SSE 的事件类型全集**在本仓库无证据来源**（解析器类型无关，`logs.jsonl` 零命中）。

---

## 22. 文本 / 模型 / 错误协议

**文本模型当前被硬关**：`TEXT_MODELS_DISABLED` + 三个入口无条件 400
（`/v1/chat/completions` 非图请求、`/v1/responses` 无 `image_generation` 工具、`/v1/messages`）。
内部 `stream_text_deltas/collect_text` 仍在但**无调用方**。

登录态与匿名 conversation **body 与头完全相同**，差异只有路径
（`/backend-api/conversation` vs `/backend-anon/conversation`）、`Authorization`、
以及匿名**显式拒绝图片输入**。models 同理只差 query，且
**`list_models()` 在生产链路无任何调用方** —— 对外 `/v1/models` 走本地清单，上游结果不外泄。

### SSE 增量合并只有两个 op

```python
if op == "append":  return current_text + value
if op == "replace": return strip_history(value, history_text)
return current_text              # 其余 op 一律静默忽略
```

历史回显处理：`strip_history` 循环剥重复前缀；`iter_conversation_payloads` 逐条比对历史，
命中即**吞掉整条事件并清空 state**；回退分支意味着上游 replace 整段时整段重发。
只消费 `data:` 行，`event:`/`id:`/注释一律忽略；空闲/墙钟双预算默认 90s/420s。

### `thinking_effort` 归一化（三处实现一致）

`""`/`none` → 不下发；`low`/`medium`/`high` 原样；`xhigh` → **`extended`**；
**其他任意值（含 `minimal`、`max`、`auto`）静默丢弃**。图像模型请求强制清空。

### 对外错误映射

| status | `type` | `code` |
|---|---|---|
| 401 | `authentication_error` | `invalid_api_key` |
| 403 | `permission_error` | `permission_denied` |
| 429 | `rate_limit_error` | `rate_limit_exceeded` |
| 其他 4xx | `invalid_request_error` | `bad_request` |
| 5xx | `server_error` | `upstream_error` |

图链路专用 `ImageGenerationError` 实际出现的 `code`：`task_cancelled`、`insufficient_quota`、
`upstream_text_reply`、`content_policy_violation`、`no_image_generated`、`upstream_arkose_required`、
`tool_quota_exhausted`、`tool_rate_limited`。
`public_image_error_message` 会把含 `backend-api/`、`status=`、`body=`、`chatgpt.com` 的
内部细节整条替换成通用文案，防上游信息泄漏。

---

## 23. `/backend-api/tasks` 与一处未用变量

`GET /backend-api/tasks` **无 query 参数**，`conversation_id`/`task_id` 全在客户端过滤。
被消费字段仅 `tasks[].{conversation_id, original_conversation_id, task_id, image_gen_message}`。
`check_task_error` 只在 `metadata.is_error && content_type=="text"` 时拼出错误文本。

> **缺陷**：`check_task_error` 计算了 `is_assistant_role` 却**未使用**
> （`openai_backend_api.py:2166` 对比 `:2170-2174`），docstring 声称三条件实际只用两条；
> 且 `is_error` 为真但 `content_type != "text"` 时返回 `(True, "", metadata)` —— 调用方拿到"有错但无消息"。

---

## 24. 汇总：值得修的点（按影响排序）

1. ~~`pow.py config[0]` 数值 vs 字符串~~ —— **已撤回，非缺陷**（核查见 §15）：
   抓包只覆盖 `sentinel/req`，无 prepare 请求体样本，且本次 8/8 生图正是数字形态且双 200。
   若要定论需补抓一次真实 `chat-requirements/prepare` 请求体。
2. **FlareSolverr 的 `userAgent` 永不生效**（被 `session.headers` 抢先），使 clearance 与 UA 解绑。
3. **cf_clearance 不覆盖资源/CDN 下载**（`_resource_session` 绕过 `build_headers`）。
4. **无 403→clearance 自动刷新闭环**；`reset_session_status_codes`、`warm_up_on_start` 是死配置。
5. **`utils/sentinel.py` 伪造 `gAAAAAB` 失败 token**，注册引擎已明确否定该做法 —— 应统一为裸 `ERROR_PREFIX`。
6. **HTTP 头语言恒中文**与出口 locale 不联动；`utils/sentinel.py` 画像完全不接 locale。
7. `check_task_error` 的 `is_assistant_role` 未用；`is_error&&!text` 返回空消息。
8. Codex 升级产物 `type=free` 与 `plan_types` 过滤构成死路，升级对生图无实际收益。
9. `utils/turnstile.py` 是精简 VM，opcode `4/11/12/13/22/27/29` 被静默跳过，遇复杂 dx 会产不出 token。
