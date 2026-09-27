# waifu2x.net 非官方 API

把 [www.waifu2x.net](https://www.waifu2x.net/) 的网页表单封装成本服务的 HTTP API。协议来自公开的 `ui.js` 与 [nunif/waifu2x/web/server.py](https://github.com/nagadomi/nunif/blob/master/waifu2x/web/server.py)，**不是**官方 SDK。

上游限制（与网页一致）：

- 文件 ≤ 5MB
- 只降噪：最多 3000×3000 像素
- 放大：最多约 1500×1500 像素（总像素 2,250,000）
- 放大档位只有 **1.6x / 2x**（4x 在 [unlimited.waifu2x.net](https://unlimited.waifu2x.net/) 浏览器端）

## 上游协议

```
POST https://www.waifu2x.net/api
Content-Type: multipart/form-data

file            图片文件（与 url 二选一）
url             http(s) 图片地址（由 waifu2x.net 去拉）
style           art | art_scan | photo
noise           -1 none, 0 low, 1 medium, 2 high, 3 highest
scale           -1 none, 1 = 1.6x, 2 = 2x
format          0 png, 1 webp
turnstile       Cloudflare Turnstile token（必填，除非 Patreon 还有 meter）
recap           旧 reCAPTCHA，当前关闭
```

成功：`200` + `image/png` 或 `image/webp`。失败常见：

- `403` `Turnstile Error`（本包装把验证码失败从上游 401 映射成 403，避免和 API 密钥无效混淆）
- `400` `Image Load Error` / `Bad Request`
- `413` 文件或像素超限

Turnstile sitekey：`0x4AAAAAABqlY7DKXMzoS81U`（`GET /recaptcha_state.json`）。

## 本服务接口

鉴权与其它 `/v1` 接口相同：`Authorization: Bearer <auth-key>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/waifu2x` | 参数说明 |
| GET | `/v1/waifu2x/status` | 上游验证码 / 登录态 |
| POST | `/v1/waifu2x` | 超分 |
| POST | `/v1/images/upscale` | 同上 |

### multipart（默认返回图片字节）

```bash
curl -sS http://127.0.0.1:8000/v1/waifu2x \
  -H "Authorization: Bearer $KEY" \
  -F "file=@input.png;type=image/png" \
  -F "style=art" \
  -F "noise=medium" \
  -F "scale=2x" \
  --output out.png
```

### JSON（默认 `b64_json`）

```bash
curl -sS http://127.0.0.1:8000/v1/waifu2x \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com/cat.png",
    "style": "art",
    "noise": "low",
    "scale": "1.6x",
    "format": "png",
    "response_format": "b64_json"
  }'
```

也接受 `image` 为 data URL 或裸 base64。`scale=1` 与网站单选框一样表示 **1.6x**；不要放大请传 `none` / `1x` / `-1`。

请求里可带 `turnstile`（从官网 widget 拿到的一次性 token）。

## Turnstile

网页对未登录 / meter 用尽的访客强制 Turnstile。本包装按顺序取 token：

1. 请求字段 `turnstile`
2. 配置的打码平台（Capsolver / 2Captcha / YesCaptcha）
3. Patreon `ses_id`（`recaptcha_state.json` 里 `logged_in && meter > 0` 时网页会跳过验证码）

环境变量（也可写在 `config.json` 的 `waifu2x` 段）：

```
WAIFU2X_CAPSOLVER_KEY=
WAIFU2X_TWOCAPTCHA_KEY=
WAIFU2X_YESCAPTCHA_KEY=
WAIFU2X_SES_ID=
WAIFU2X_BASE_URL=https://www.waifu2x.net
WAIFU2X_TIMEOUT_SEC=180
```

`GET /api/settings` 只回传 `has_*` 与掩码，不会把密钥明文给前端。

请遵守 waifu2x.net 的使用约定：这是个人学习向的协议封装，不要拿去打爆他们的 GPU 队列。作者靠 [Patreon](https://www.patreon.com/nagadomi) / [Fantia](https://fantia.jp/waifu2x) 养服务器。

## CLI

```bash
python scripts/waifu2x_upscale.py input.png -o out.png --style art --noise medium --scale 2x
```
