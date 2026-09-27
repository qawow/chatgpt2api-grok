# 豆包网页生图（非官方）

封装 `https://www.doubao.com/chat/create-image` 使用的 **`POST /chat/completion`**。  
**不是**火山方舟官方 Ark API。需要你自己的豆包登录 Cookie。人机验证请外接打码，本仓库不破解 webmssdk / a_bogus。

## 上游协议（已核对）

| 项 | 值 |
| --- | --- |
| 页面 | https://www.doubao.com/chat/create-image |
| 接口 | `POST https://www.doubao.com/chat/completion?aid=497858&device_platform=web&samantha_web=1&use-olympus-account=1&...` |
| 技能 | `skill.skill_type = 4`（`ImageGeneration`） |
| 消息 | `content_type = 2001`，`content` 为 `{"text": prompt}` JSON 字符串 |
| 未登录 | `{"code":710012001,"msg":"登录已过期，请重新登录"}` |
| 风控签名 | webmssdk 对 `/chat/completion` 计算 `a_bogus`（可在请求里透传） |

## 本服务接口

- `GET /v1/doubao` 帮助
- `GET /v1/doubao/status` 探测登录
- `POST /v1/doubao` 生图
- `POST /v1/images/generations` 且 `model` 以 `doubao` 开头

```bash
curl -s http://127.0.0.1:8000/v1/doubao \
  -H "Authorization: Bearer $CHATGPT2API_AUTH_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"一只橘猫坐在窗台上","cookies":"sessionid=...; ttwid=..."}'
```

OpenAI 形：

```json
{
  "model": "doubao-image",
  "prompt": "一只橘猫",
  "cookies": "sessionid=...",
  "captcha": "可选，外接打码结果",
  "a_bogus": "可选"
}
```

## 鉴权与打码

1. **Cookie（必填）**：浏览器登录 www.doubao.com 后复制 `Cookie` 整串，放到配置 `doubao.cookies` / 环境变量 `DOUBAO_COOKIES`，或每次请求传 `cookies`。
2. **captcha 透传**：请求字段 `captcha`。
3. **外接 webhook**：`CAPTCHA_SOLVE_URL` 或 `doubao.solve_url`。本服务 `POST`：

   ```json
   {"type":"","websiteURL":"https://www.doubao.com/chat/create-image","websiteKey":"","extra":{}}
   ```

   期望返回 `{"token":"..."}` 或 `{"solution":{"token":"..."}}`。
4. **SaaS**：`CAPSOLVER_KEY` / `TWOCAPTCHA_KEY` / `YESCAPTCHA_KEY`（也可用 `DOUBAO_*` 前缀）。按 `captcha_task_type` 指定任务类型。

`a_bogus` 不是验证码，是请求签名。上游若拒绝未签名请求，把网页里算好的 `a_bogus` 原样传入即可。

## 配置

```json
"doubao": {
  "cookies": "sessionid=...; ttwid=...",
  "solve_url": "http://127.0.0.1:9000/solve",
  "timeout_sec": 180
}
```

密钥与 Cookie 在 `GET /api/settings` 中掩码。
