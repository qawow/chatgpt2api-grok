from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "chatgpt2api")

from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from api.app import RequestBodyLimitMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request):
        body = await request.body()
        return {"bytes": len(body)}

    app.add_middleware(RequestBodyLimitMiddleware)
    return app


class RequestBodyLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        # Force a small, deterministic limit regardless of config.json.
        patcher = mock.patch(
            "services.config.ConfigStore.max_request_body_mb",
            new_callable=mock.PropertyMock,
            return_value=1,  # 1 MB
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(_build_app())

    def test_allows_small_body(self) -> None:
        response = self.client.post("/echo", content=b"x" * 1024)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"bytes": 1024})

    def test_rejects_oversized_content_length(self) -> None:
        response = self.client.post("/echo", content=b"x" * (2 * 1024 * 1024))
        self.assertEqual(response.status_code, 413, response.text)
        self.assertIn("exceeds", response.json()["error"])

    def test_rejects_oversized_streamed_body(self) -> None:
        # No single content-length header here; the guard must count bytes as
        # they arrive (this is the multipart / chunked-upload case).
        def stream():
            for _ in range(4):
                yield b"x" * 512 * 1024  # 2 MB total > 1 MB limit

        response = self.client.post("/echo", content=stream())
        self.assertEqual(response.status_code, 413, response.text)

    def test_limit_can_be_disabled(self) -> None:
        with mock.patch(
            "services.config.ConfigStore.max_request_body_mb",
            new_callable=mock.PropertyMock,
            return_value=0,
        ):
            client = TestClient(_build_app())
            response = client.post("/echo", content=b"x" * (2 * 1024 * 1024))
            self.assertEqual(response.status_code, 200, response.text)


if __name__ == "__main__":
    unittest.main()
