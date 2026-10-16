# 未发布改动

基于 1.8.13，本批次保留现有用户设置和数据。

## 修复与优化

（本批次尚无改动。上一批次的完整说明见 [CHANGELOG.md](./CHANGELOG.md) 的 1.8.13 条目。）

## 回归测试

## 清障行为与验证范围

## 本地验证

测试包将 `CHATGPT2API_DATA_DIR` 和 `CHATGPT2API_CONFIG_FILE` 指向临时目录；网络命名空间仅启用回环接口，供本地模拟服务使用，外部网络不可达。

```bash
unshare --net bash -c 'ip link set lo up && exec .venv/bin/python -m pytest -q --tb=short'
```

全套结果：942 passed、17 skipped；另有 101 项 subtests 通过。跳过项依赖真实服务。两项已有警告涉及 SQLAlchemy 弃用接口及安全 Cookie 属性。前端类型检查 `web/node_modules/.bin/tsc --noEmit --incremental false -p web/tsconfig.json` 通过。

验证前后，现有 `config.json`、`VERSION` 和 `data/` 内 JSON/版本文件的内容哈希应保持不变。发布前请确认没有未提交的密钥、没有更新发布标签。
