# 360智图网页生图（非官方）

封装 [image.360.com](https://image.360.com/) 工具 iframe（`/tools/saas_app`，图查查 / 360AI 图片）的 **`/api/v1/zhitu`**。  
文生图配置接口**免登录**；真正 `create` 需要 QHPass 登录，且常要会员或下载豆（errno `20603`）。

## 上游协议（已核对）

来自 `saas_main.js`（`!5e01f801`）+ 实探：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/zhitu/text/to/image/v2/config` | 模型/比例/风格，公开 200 |
| GET | `/api/v1/zhitu/text/to/image/templates` | 模板 |
| POST JSON | `/api/v1/zhitu/text/to/image/create` | 创建任务 |
| POST JSON | `/api/v1/zhitu/text/to/image/query` | `{record_id}` 轮询 |
| GET | `/v1/sale/user_status` | 未登录 `errno:20601` |
| POST | `/api/v1/zhitu/trial` | 试用，未登录 20601 |

create 字段（`ki()` JSON POST）：

```json
{
  "prompt": "一只橘猫",
  "promptText": "一只橘猫",
  "feature": "tools_text2image",
  "api_user": "chacha",
  "ratio": "1:1",
  "style": "auto",
  "model": "jimeng",
  "photoNums": 1
}
```

缺字段时 `errno:-1 参数检查失败`；补齐后无登录/无豆为 `20603 付费功能`。

图生图：`/api/v1/zhitu/image/to/image/create`（另需 `s3_key`）。  
超分：`POST multipart /api/v1/enlarge`（图做 RSA/`sk` 加密，本封装暂不实现）。

`task_result.status`：`0` 创建 / `1` 排队 / `2` 生成中 / `3` 成功 / `4` 失败 / `5` 超时 / `6` 风控（当验证码）。

公开模型（v2/config）：`jimeng` 即梦3.0、`jimeng40` 即梦4.0、`jimeng45` 即梦4.5、`hunyuan` 混元、`tongyi` 通义、`wanx21plus` 万相2.1Plus。比例默认 `1:1`。

## 本服务接口

- `GET /v1/zhitu360` 帮助
- `GET /v1/zhitu360/status` 配置 + 登录态
- `GET /v1/zhitu360/models` 上游 v2/config
- `POST /v1/zhitu360` 创建并默认轮询
- `POST /v1/zhitu360/query` `{record_id}`
- `POST /v1/images/generations` 且 `model` 为上表模型或 `zhitu360` / `360智图`

```bash
curl -s http://127.0.0.1:8000/v1/zhitu360 \
  -H "Authorization: Bearer $CHATGPT2API_AUTH_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"一只橘猫","model":"jimeng","ratio":"1:1","cookies":"Q=...; T=..."}'
```

`wait: false` 只返回 `record_id`，再调 `/v1/zhitu360/query`。

## 鉴权与打码

1. **Cookie**：360 登录后的 `Q` / `T` / QHPass 整串 → `zhitu360.cookies` / `ZHITU360_COOKIES` / 请求 `cookies`。
2. **captcha 透传**：请求字段 `captcha`。
3. **外接 webhook**：`CAPTCHA_SOLVE_URL` 或 `zhitu360.solve_url`（JSON 同 [豆包文档](./doubao.md)）。
4. **SaaS**：`CAPSOLVER_KEY` / `TWOCAPTCHA_KEY` / `YESCAPTCHA_KEY`（可用 `ZHITU360_*` 前缀）。

风控 `status=6 hit_risk` 映射 403 `captcha_required`。

## 配置

```json
"zhitu360": {
  "base_url": "https://image.360.com",
  "cookies": "Q=...; T=...",
  "solve_url": "http://127.0.0.1:9000/solve",
  "timeout_sec": 180,
  "poll_interval_sec": 2
}
```
