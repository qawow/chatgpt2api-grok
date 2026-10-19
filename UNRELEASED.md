# 未发布改动

基于 1.8.13，本批次保留现有用户设置和数据。

## 修复与优化

- Grok 刷新网络错误：远端 `refresh network error: HTTPSConnectionPool(host='auth.x.ai', port=443): Read timed out.` 的根因**不是代理不通**（同一代理打该端点 0.4 秒返回）。真实链路是：该 SOCKS 池对**新建** TLS 握手有 25-50% 的 `SSLEOFError: UNEXPECTED_EOF_WHILE_READING` 抖动（复用 keep-alive 连接实测 8/8 成功）；urllib3 把这个抖动包装成 `Max retries exceeded`，正好命中 `_is_egress_connect_error`，于是**一次丢包被当成「出口已死」** —— 拉黑健康出口并回退 `direct`，而直连无路由，挂满整个 30 秒预算，最终抛出的直连超时掩盖了真实原因。修复：新增 `is_transient_tls_error()` 两级分类（握手 EOF 可重试任意方法；连接重置等**阶段不明**的失败仅对幂等方法重试，避免重放一个可能已轮换一次性凭证的 POST），`grok_backend_api._request` 对**同一出口**重试 3 次后才降级，并优先抛出主出口的错误而非回退路径的超时。实测真实代理 12/12 = 100%。
- Grok 死 refresh_token 不再被当成健康号：`_classify_failure` 只认 401/403，而 xAI 的 token 端点对失效 refresh_token 返回 **HTTP 400 + `invalid_grant`**（RFC 6749 §5.2，永久性错误，重试无用），且 `status is not None` 分支在检查文本前就把它判成 `other`；`ensure_fresh_account` 又硬编码 `{401, 403}`，`refresh_accounts` 更是**只写 `last_error`、从不改 `status`**。三处叠加的结果是：一个 `access_token` 已过期 85 天、每次刷新都被拒的账号，在号池里始终显示「正常」并被反复选中。修复：`invalid_grant` 在状态回退分支**之前**判定为 `auth`（仅此一个 OAuth 协议错误码穿透，`_classify_failure(502, "HTTP 403 somewhere in attempts")` 仍为 `other`，既有裸数字防护不变），`ensure_fresh_account` 改走 `_classify_failure`，`refresh_accounts` 按判定结果落 `异常`/`限流`（网络抖动仍保持「正常」）。
- 测试隔离：新增用例曾用 `mock.patch.stopall` 清理，它会连带停掉**其他测试模块**正在生效的 patch，导致 `test_image_recovery_regressions` 随机失败。改为显式 `addCleanup(patcher.stop)`。

## 回归测试

## 清障行为与验证范围

## 本地验证

测试包将 `CHATGPT2API_DATA_DIR` 和 `CHATGPT2API_CONFIG_FILE` 指向临时目录；网络命名空间仅启用回环接口，供本地模拟服务使用，外部网络不可达。

```bash
unshare --net bash -c 'ip link set lo up && exec .venv/bin/python -m pytest -q --tb=short'
```

全套结果：942 passed、17 skipped；另有 101 项 subtests 通过。跳过项依赖真实服务。两项已有警告涉及 SQLAlchemy 弃用接口及安全 Cookie 属性。前端类型检查 `web/node_modules/.bin/tsc --noEmit --incremental false -p web/tsconfig.json` 通过。

验证前后，现有 `config.json`、`VERSION` 和 `data/` 内 JSON/版本文件的内容哈希应保持不变。发布前请确认没有未提交的密钥、没有更新发布标签。
