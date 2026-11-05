# 未发布改动

基于 1.8.14。本批次不更新发布版本、账号池或用户配置。

## 模型 ID 变更（2026-11-06）

- **新增公开 ID `gpt-image-2.6`**，与 `gpt-image-2.5` 并行对外列出，两者上游 slug 同为 `auto` + `system_hints:["picture_v2"]`。`gpt-image-2` 仍作为旧别名接受但不列出。
- **未删除 2.5**：没有任何证据表明 2.5 已被上游淘汰；删除会打断在用客户端。你选择「新增 2.6 再看 2.5 是否存在」，因此 2.5 保持列出。
- 前端 [models.ts](<web/src/lib/models.ts>) 同步新增 `WEB_IMAGE_MODEL_26`。

### GIF：静态证据存在，但未能实测

- 抓取 bundle 中确认 GIF 端点为 `POST /image-gen/gif/select_speed`（body：`conversation_id`、`message_id`、`asset_pointer`、`selected_speed`；前端对 404/405 静默）。证据文件：[439600.680dc5841e.js](<.verify/cgpt26/js/439600.680dc5841e.js>)、 [337882.25be89bb6b.js](<.verify/cgpt26/js/337882.25be89bb6b.js>)（含 `gif_speed_variants` / `selected_speed` / `animation` 读取逻辑）。
- 该端点语义是**生成后选速度**，不是"生成 GIF"的参数；生成仍走 picture_v2，是否产出 GIF 由服务端按请求内容决定。
- **实际生成尝试失败**：本地服务已启动、链路进入上游，但 5 个注册免费账号的 access_token 全部过期且刷新失败（`curl: (77)` 证书验证错误），返回 429。**未产出任何 GIF，未据此修改模型 ID 之外的行为，也未消费配额。**
- 因此**「能否生成 GIF」仍未验证**；不要把这轮当作 GIF 可用性的证据。

## 修复与优化

- 网页图片传输使用完整 SSE 帧：保留 event／id、多 data 换行；monotonic 子秒预算、有界队列与关闭清理，保留旧字符串适配接口。
- 按捕获的网页 v1 decoder 实现通道状态、短字段继承、nested patch／append／truncate，失败原子回滚；新 encoding 声明重建基线，无新声明的恢复保留状态。
- 补齐图片链路已有 Turnstile／Session Observer 头；仅 prepare／初始 conversation 使用 MCP form version 2，同一图片 turn 固定 trace。
- 最多一次 `/f/conversation/resume`：body 仅 conversation_id／offset，最新流 token 原值放 X-Conduit-Token。offset 计完整 non-response 事件（含 encoding／token），不是行、字节或 SSE id；初始与恢复共享时间预算。
- 已有可信会话／生成输出 ID 后，不把断线、下载失败或后期 poll 超时改成换账号重提。只有输出 ID、缺 conversation ID 时也禁止自动重新生成；pending 错误不谎称已有 CID。
- 恢复候选统一开始时间、时间窗、排除集、原子 claim 与 checkpoint；不盲猜最新或并列会话。关键词匹配仍为启发式，不等于所有权证明或 exactly-once。
- 输入参考图的 input_message 不计入生成输出；取消、协议、终止 HTTP、配额／策略及回调错误不伪装为传输恢复成功。UTF-16 中间态在最终 OpenAI／Anthropic SSE JSON 边界安全转义。
- PNG／JPEG／WebP／GIF 原字节保存、真实后缀与 WebDAV／HTTP MIME；兼容旧 PNG URL 内的其他格式，不迁移索引、不丢 GIF 帧／duration／loop、不增加结果 JSON 字段。
- 重写 [网页图片逆向说明](<docs/chatgpt-images-2.6-reverse.md>)，删除代理凭据及未经证实的“2.6 已发布／特定功能为 2.6 新增／历史字段一定无副作用”断言。公开 gpt-image-2.5／旧 gpt-image-2 与网页 model:auto 路由不变。

## 本次验证（2026-11-06，不是历史数字）

隔离 Python 3.14.4、pytest 9.2.0，UTF-8 模式；tzdata 2026.6 和 tokenizer 公开静态缓存只用于本地测试环境，项目 lock 与系统 Python 未改。

| 验证 | 实际结果 |
| --- | --- |
| 本批 Python 源码／测试语法门禁 | 通过；本地验证／回滚脚本亦检查 |
| 模型命名／路由专项（7 模块） | **122 passed，3 skipped，6 subtests passed**，2 warnings，3.66s |
| 六个传输／格式专项模块 | **161 passed，184 subtests passed**，2 warnings |
| 扩展离线 suite（含 2.6 改动后复跑） | **1379 passed，6 failed，274 subtests passed**，2 warnings，54.44s |
| 运行时模型目录（真实账号数据，只读） | `['gpt-image-2.5', 'gpt-image-2.6']`，两者均列出 |
| 配置／版本／现有用户数据 | 每轮前后受保护文件 SHA-256 一致 |

**没有把整套 suite 记为通过。** 剩余 6 项来自未修改的模块／测试：

- 3 项 POSIX `0o600` 权限位断言，在 Windows 得到 `0o666`：[egress 权限测试:155](<test/test_egress_and_recovery.py#L155>)、[日志权限测试:296](<test/test_log_pagination.py#L296>)、[注册日志权限测试:223](<test/test_resin_log_diagnostics.py#L223>)。
- [时区测试:32](<test/test_egress_locale.py#L32>)固定断言洛杉矶九月的 PDT `-07`；本次十一月执行为 PST `-08`。
- [注册 bootstrap 测试](<test/test_gpt_register.py>)的临时 SQLite 文件清理触发 Windows WinError 32（仍有打开句柄）。
- [日志读取期间替换测试:411](<test/test_log_pagination.py#L411>)在 Windows 替换打开文件触发 WinError 5。

这些问题不顺手扩大到本批外修复，也未通过伪造时间／权限、忽略断言或改成“passed”掩盖。

首次扩展测试另有 3 项回环 curl 夹具被过严的测试入口阻挡；核对夹具后已仅放行字面回环 IP、禁用环境代理及重定向、拒绝真实服务端口，3 项实测通过。UTF-8 复跑使此前被编码错误遮住的注册日志权限断言显现。

```powershell
& '.verify\transport-update\.venv\Scripts\python.exe' -X utf8 -B '.verify\transport-update\run_tests.py' --syntax test/test_sse_idle_timeout.py test/test_image_storage_service.py test/test_sentinel_headers.py test/test_pow_retry.py test/test_image_recovery_regressions.py test/test_web_image_transport.py
```

入口先导入隔离 test 包；禁外部 stdlib socket／curl_cffi 传输，原生 HTTP 仅允许已核对的回环夹具且不跟随重定向；真实服务 v1 模块在 collection 前排除。不刷新／注册真实账号，不消费配额，不调用真实 WebDAV 或删除远端会话。没有实际生成图片验证 2.6 或任何特定账号的权限。

## 备份与回滚

本轮模型改名另有独立备份：[.bak/model-rename-20261106_023948](<.bak/model-rename-20261106_023948/>)，覆盖 `utils/image_models.py`、`utils/helper.py`、`services/openai_backend_api.py`、`services/protocol/openai_v1_models.py` 及 6 个测试文件。单文件回滚示例：

```powershell
Copy-Item -LiteralPath '.bak\model-rename-20261106_023948\utils\image_models.py' -Destination 'utils\image_models.py' -Force
```

改名前请一并回滚 `utils/helper.py`（其导入 `WEB_IMAGE_MODEL_26`），否则会 ImportError。

12 个原有计划文件备份已校验 SHA-256：[备份清单](<.bak/20261105_223753/manifest.json>)。旧文档可能仍含代理凭据，须保持本地忽略，勿提交或分享；必要时由用户自行轮换。

[整体回滚脚本](<.verify/transport-update/rollback_update.py>)默认只打印将操作的完整路径；校验全部备份与交付指纹，拒绝覆盖交付后的新改动。预览无误后，显式 `--confirm` 才恢复 12 原文件并删除 2 新代码文件，不动配置／用户数据／验证环境：

```powershell
& '.verify\transport-update\.venv\Scripts\python.exe' -X utf8 -B '.verify\transport-update\rollback_update.py'
# 确认预览路径、旧凭据风险，且没有后续改动之后才执行：
& '.verify\transport-update\.venv\Scripts\python.exe' -X utf8 -B '.verify\transport-update\rollback_update.py' --confirm
```

不建议只恢复一半协议文件而保留另一半的新依赖。本批没有执行实际回滚。

## 历史记录（不是本次执行结果）

上次记录的 Linux 网络命名空间测试是 1278 passed、17 skipped、101 subtests passed；两项警告涉及 SQLAlchemy 弃用接口及 Cookie 属性。上次前端 TypeScript 检查通过。本批未修改前端，没有把历史前端检查或旧测试统计重新记为本次验证。

发布前须确认没有未提交密钥、没有误改 [config.json](<config.json>)、[VERSION](<VERSION>) 或用户数据，也不要把本地抓取素材、环境与旧备份作为发布产物。
