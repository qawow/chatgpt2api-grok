# grokcli2api-go 接入（设置页）

在 chatgpt2api **设置 → GrokCLI2API** 中对接
[Futureppo/grokcli2api-go](https://github.com/Futureppo/grokcli2api-go)。

## 远程接口（对方）

需在 grokcli2api-go 配置 `GROK_ADMIN_KEY`，管理接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/admin/credentials` | 脱敏列表（**无 token**） |
| POST | `/v1/admin/credentials` | 上传/覆盖 OAuth JSON（body 或 multipart `file`） |
| DELETE | `/v1/admin/credentials/{id}` | 按脱敏 id 删除 |

鉴权：`Authorization: Bearer <GROK_ADMIN_KEY>` 或 `X-Admin-Key`。

默认端口：`8088`。

## 本侧接口

配置存 `data/g2a_config.json`（admin_key 不回显，仅 `has_admin_key`）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST/DELETE | `/api/g2a/servers` | 连接 CRUD |
| POST | `/api/g2a/servers/{id}` | 更新 |
| POST | `/api/g2a/servers/{id}/ping` | 连通探测 |
| GET | `/api/g2a/servers/{id}/credentials` | 远程脱敏列表 |
| POST | `/api/g2a/servers/{id}/push` | 推送本地 Grok 号池 |
| DELETE | `/api/g2a/servers/{id}/credentials/{cid}` | 删远程凭证 |

均需 chatgpt2api **管理员** Bearer。

## 数据方向（重要）

```
本地 Grok 号池 (data/grok_accounts.json)
        │  push
        ▼
grokcli2api-go  /v1/admin/credentials  →  auths/
```

远程列表**不含** access/refresh token，因此：

- ✅ 本地 → 远程上传
- ✅ 远程脱敏列表 / 删除
- ❌ 远程 → 本地拉号（做不到，对方设计如此）

推送体为 cliproxy 兼容 JSON（`type=xai` + token 三件套 + headers）。

## 使用步骤

1. 部署 grokcli2api-go，设置 `GROK_ADMIN_KEY`，确保 `/v1/admin/credentials` 可访问  
2. chatgpt2api 设置 → **GrokCLI2API** → 添加连接  
   - 地址：`http://host:8088`  
   - Admin Key：与远程一致  
3. **探测连通**  
4. 先保证本地 Grok 号池有账号（`/api/grok/accounts` 或 cliproxy 导入）  
5. **推送本地 Grok 号池**  

## 与 CPA / 本地 Grok 池的关系

| 组件 | 作用 |
|---|---|
| `/api/accounts` | ChatGPT 号池 |
| `/api/grok/accounts` | 本地 Grok 号池（本进程调度） |
| `/api/cpa/*` | CLIProxyAPI 管理（ChatGPT 远程 auth-files） |
| `/api/g2a/*` | grokcli2api-go 管理（Grok 远程 credentials） |

三者存储与选号互不串池。

## curl 示例

```bash
# 添加连接
curl -s -X POST http://127.0.0.1:8000/api/g2a/servers \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"name":"local","base_url":"http://127.0.0.1:8088","admin_key":"YOUR_ADMIN"}'

# 推送全部本地 Grok 号
curl -s -X POST http://127.0.0.1:8000/api/g2a/servers/$SID/push \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"access_tokens":[]}'
```

## 代码

| 路径 | 作用 |
|---|---|
| `services/g2a_service.py` | 配置 + Admin 客户端 + 推送 |
| `api/g2a.py` | 管理路由 |
| `web/.../g2a-connections.tsx` | 设置页 UI |
