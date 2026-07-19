"""chatgpt2api 管理 API 客户端（号池导入）。"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ChatGPT2APIError(RuntimeError):
    """调用 chatgpt2api 失败。"""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class ChatGPT2APIClient:
    """最小管理端客户端：导入 / 列出账号。

    鉴权: Authorization: Bearer <auth-key>
    默认读环境变量:
      CHATGPT2API_BASE_URL  (默认 http://127.0.0.1:8000)
      CHATGPT2API_AUTH_KEY  (默认 chatgpt2api，与上游 config.json 一致)
    """

    def __init__(
        self,
        base_url: str | None = None,
        auth_key: str | None = None,
        *,
        timeout: float = 60.0,
    ):
        self.base_url = (
            (base_url or os.environ.get("CHATGPT2API_BASE_URL") or "http://127.0.0.1:8000")
            .strip()
            .rstrip("/")
        )
        self.auth_key = (
            auth_key
            or os.environ.get("CHATGPT2API_AUTH_KEY")
            or os.environ.get("CHATGPT2API_AUTHKEY")
            or "chatgpt2api"
        ).strip()
        self.timeout = float(timeout)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        data = None
        headers = {
            "Authorization": f"Bearer {self.auth_key}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                if not raw.strip():
                    return {}
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {"data": parsed}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            try:
                parsed_body: Any = json.loads(detail) if detail else None
            except json.JSONDecodeError:
                parsed_body = detail
            raise ChatGPT2APIError(
                f"chatgpt2api {method.upper()} {path} -> HTTP {exc.code}: {detail[:300]}",
                status=exc.code,
                body=parsed_body,
            ) from exc
        except URLError as exc:
            raise ChatGPT2APIError(
                f"无法连接 chatgpt2api ({self.base_url}): {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise ChatGPT2APIError(
                f"连接 chatgpt2api 超时 ({self.base_url}, {self.timeout}s)"
            ) from exc

    def ping(self) -> dict[str, Any]:
        """用列出账号探测服务与鉴权是否可用。"""
        return self.list_accounts()

    def list_accounts(self) -> dict[str, Any]:
        return self._request("GET", "/api/accounts")

    def add_accounts(
        self,
        accounts: list[dict[str, Any]] | None = None,
        *,
        tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /api/accounts — 导入后会触发 refresh。"""
        payload: dict[str, Any] = {
            "accounts": list(accounts or []),
            "tokens": list(tokens or []),
        }
        if not payload["accounts"] and not payload["tokens"]:
            raise ValueError("accounts 与 tokens 不能同时为空")
        return self._request("POST", "/api/accounts", body=payload)

    def add_account(self, account: dict[str, Any]) -> dict[str, Any]:
        return self.add_accounts([account])
