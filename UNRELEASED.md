# 未发布改动

基于 1.8.15，本批次保留现有用户设置和数据。

## 修复与优化

（暂无 —— 1.8.15 已发布，见 [CHANGELOG.md](./CHANGELOG.md)。新改动从 1.8.16 开始记录。）

## 已知未验证项（跨版本）

- **网页 GIF 生成未实测**：静态证据确认存在 `POST /image-gen/gif/select_speed`，但该端点是「生成后切换速度」，不是生成 GIF 的参数。2026-11-06 真实生成尝试因 5 个注册账号 `access_token` 全部过期且刷新失败（`curl: (77)` 证书错误）返回 429，**未产出 GIF、未消费配额**。详见 [CHANGELOG.md](./CHANGELOG.md) 的 1.8.15 条目。

## 回归测试

## 清障行为与验证范围

## 本地验证

测试包将 `CHATGPT2API_DATA_DIR` 和 `CHATGPT2API_CONFIG_FILE` 指向临时目录，外部网络不可达。所有原型验证在隔离 venv（Python 3.14.4 + pytest 9.2.0）中完成，系统 Python 与项目 lock 未改。

发布前须确认没有未提交密钥、没有误改 [config.json](<config.json>)、[VERSION](<VERSION>) 或用户数据，也不要把本地抓取素材、环境与旧备份作为发布产物。
