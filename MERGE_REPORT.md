# free-fix-pack 合并报告

- 日期：2026-07-21
- 源包：`/root/chatgpt2api-free-fix-pack/chatgpt2api-free-fix-pack`
- 目标：`/root/chatgpt2api`
- 策略：**语义移植**（保留项目已有 `session_only`/`fragile` 与 d0d6f44 行为，不整文件覆盖 pack）
- 备份：`backups/pre-free-fix-20260721/{account_service,gpt_register_service,openai_backend_api}.py`
- 提交：未提交（按用户要求仅合并，等指示再 commit）

## MUST 行为验收

| # | 要求 | 状态 | 落点 |
|---|---|---|---|
| 1 | session 刷新后必须 `/backend-api/me` 探活，废 JWT 不算成功 | 已合入 | `account_service._validate_access_token_alive` / `_refresh_access_token_via_session` / `refresh_access_token`；`openai_backend_api._try_refresh_access_from_session` |
| 2 | free + session/password 不自动删除；恢复成功以探活 200 为准 | 已合入 | `remove_invalid_token`（force recover + alive 校验；auto_remove 时 free 保留异常） |
| 3 | revoked 不可选；网络超时 + 本地 quota>0 可 fallback | 已合入 | `_token_looks_revoked` / `_is_image_account_available`；`get_available_access_token` hard/soft 分支 |
| 4 | 注册缺 quota 默认值 + `fetch_remote_info` | 已合入 | `gpt_register_service._import_local`：`GPT_FREE_DEFAULT_IMAGE_QUOTA`（默认 30）+ remote sync |
| 5 | 文本取号不每次 force session 刷新 | 已合入 | `get_text_access_token`：仅 异常/限流 或有 `last_refresh_error` 时 `force=True` |

## 改动文件

### `services/account_service.py`（核心）

- 新增：`_token_looks_revoked`、`_validate_access_token_alive`、`_refresh_access_token_via_session`
- 重写：`refresh_access_token`（OAuth → session cookie → password；成功前强制 `/me`）
- 调整：`_is_image_account_available`（revoked 排除；quota>0 可进池；free 新号 bootstrap 需 session/refresh 材料）
- 调整：`get_available_access_token`（硬鉴权 skip / 网络软错误本地 fallback / remote 要求 quota>0）
- 调整：`get_text_access_token`（条件 force；异常号有恢复材料可 soft 候选）
- 调整：`remove_invalid_token`（free 恢复 + 不误删 + 保留 session_only 标记）
- 调整：`_should_defer_invalid_token`（free+session/password 更长宽限；仍保留 session_only 永不急删）
- 调整：`_apply_refreshed_tokens` 持久化 `session_token`

**适配点（相对 pack 整文件）：**

- 保留项目 `session_only`/`fragile` 标记与 `_is_session_only_account`
- 文生图：不再「无 refresh 一律不可选」；有真实 `quota>0` 或 free bootstrap 材料时可进候选
- 未改 pack 中与 free 无关的其它模块

### `services/gpt_register_service.py`

- `_import_local`：缺 quota 时写默认 `GPT_FREE_DEFAULT_IMAGE_QUOTA`（30）
- 保留 `session_only`/`fragile` 标记
- `fetch_remote_info` 前尝试按 email 解析可能 rotate 的 token；`list_accounts` 失败不阻断 import

### `services/openai_backend_api.py`

- 新增：`_attach_session_cookie`、`_try_refresh_access_from_session`
- `_get_me` / `_get_conversation_init`：401 时走 session 续期并再试
- 直连 `/api/auth/session` 回退路径同样要求 `/me` 200（比 pack 更严，避免假刷新写库）

### 测试

- `test/test_account_image_capabilities.py`：改为覆盖 revoked 排除、free bootstrap、quota>0 可进池
- `test/test_gpt_register.py`：mock `list_accounts`；断言默认 quota=30 与 rotate token 解析

## 未合入 / 跳过

| 项 | 原因 |
|---|---|
| pack 整文件覆盖三服务 | 会丢掉 d0d6f44 的 session_only 策略与项目其它局部差异 |
| `c7abfed` 中 Dockerfile / Grok / CFD1 运维改动 | 用户要求只合 free 相关修复 |
| `data/gpt_register_config.json` 等配置 | 运行时配置，非代码；含敏感信息 |
| 风控绕过 / 打码 / 批量养号 | 明确禁止 |

## 验证

```text
python -m py_compile services/account_service.py services/gpt_register_service.py services/openai_backend_api.py
uv run python -m unittest test.test_account_image_capabilities test.test_gpt_register -v
# Ran 31 tests … OK
```

## 已知限制（与 pack 一致）

- free passwordless 常无 OAuth `refresh_token`；本合并修的是本地假刷新 / 误删 / 取号，不是上游 `token_revoked` 本身
- 上游 7–15 分钟 `token_revoked` 无法靠本地逻辑阻止

## 后续本地减噪（2026-07-21，同工作树未 commit）

- free + session_only：`list_normal_tokens` / `list_limited_tokens` / `refresh_accounts` 跳过周期性全量探活
- revoked 冷却：`_REVOKED_COOLDOWN_SECONDS=3600`；`session_refresh_stale_token_revoked` / password 403 等命中后，`refresh_access_token`/`remove_invalid_token` recover / auto_relogin 不再每 5 分钟重试
- watcher 空闲时不再对纯 free session_only 池打 checking 日志

## 注册 OAuth 本地稳健性（2026-07-21，同工作树未 commit）

文件：`gpt_free_register/engines/platforms/chatgpt/register.py`

| 现象 | 本地修复 | 说明 |
|---|---|---|
| NextAuth CSRF 空/非 JSON → `Expecting value: line 1 column 1` | `_parse_json_response` + `_start_oauth(attempts=3)` | 记录 status/content-type/body snip；清 NextAuth cookie 后重试，不裸 `resp.json()` |
| signin/openai 非 200 / 无 url | 同 retry 环 | 每轮重建 csrf → signin |
| authorize/continue 409 `invalid_state` | `_rebuild_oauth_session` + `_submit_signup_form(allow_oauth_rebuild=…)` | 最多重建一次 OAuth（clear cookie → start_oauth → device id → sentinel → 再 continue）；非风控绕过 |

**明确未做：** 指纹/代理伪装、打码、批量养号、任何 OpenAI 风控绕过。

验证：`python -m py_compile gpt_free_register/engines/platforms/chatgpt/register.py`

## UI 报错：progress not found / no available image quota（2026-07-21，未 commit）

| 现象 | 根因 | 本地修复 |
|---|---|---|
| `progress not found` | `POST /api/accounts/refresh` 返回 `progress_id` 后 UI 立刻 poll；`refresh_accounts` 若把 free/session_only 全过滤掉会 **finish 未 init** 的进度，或 init 晚于首轮 poll | API 层 **create_task 前** `init_refresh_progress` / `init_relogin_progress`；空列表 finish 前确保已 init；service 侧 missing 才 init |
| `no available image quota` | free session_only 上游 `token_revoked`；本地仍显示「正常」+ quota=25，图池因 `_token_looks_revoked` 正确排除 → 空池 | 确认废 token 时 **立即标异常**（`auto_remove` 关也一样）；429 文案带 pool/revoked 诊断；已有死号手工标异常 |

验证：`uv run python -m unittest test.test_account_image_capabilities -v` → 21 OK；服务已重启。

**使用侧：** 池里没有可图选账号时生图必然 429——需重新注册存活 free 号（或 Plus/Pro + refresh_token）。本地无法复活上游已 revoke 的 session_only JWT。

## UI 卡在「生成中」（2026-07-21，未 commit）

| 现象 | 根因 | 本地修复 |
|---|---|---|
| 任务 `status=running` / `progress=generating` 长期不结束 | 账号取号成功后进入 SSE（`_start_image_generation` → `iter_sse_payloads`）；curl_cffi 的 `timeout=` 主要覆盖 connect/headers，body 半开（SOCKS CLOSE-WAIT）时 `iter_lines()` 永久阻塞；任务线程无 wall-clock | 1) `iter_sse_payloads` 后台读线程 + idle/total 超时（默认 90s / 420s）→ `SseStreamTimeoutError`；2) `image_task_service._run_task` 外层 `image_task_timeout_secs`（默认 600s）强制 error；3) SSE timeout 归入 connection timeout 重试路径 |
| 历史卡住任务 | 进程内线程已挂死，磁盘任务仍 running | 重启服务会 `_recover_unfinished_locked` 标「服务已重启，未完成的图片任务已中断」 |

配置键：`image_sse_idle_timeout_secs`、`image_sse_total_timeout_secs`、`image_task_timeout_secs`。

验证：
```text
uv run python -m unittest test.test_sse_idle_timeout test.test_image_task_service -v
# + 重启 main.py 清掉 22:56 卡住的 running 任务
```

**非本地可修：** free session_only 上游 SSE 经代理卡住 / token 很快 revoked；本地只保证 UI 不再无限「生成中」。
