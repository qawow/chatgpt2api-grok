from __future__ import annotations

import json
from contextlib import asynccontextmanager
from threading import Event, Thread

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from api import accounts, ai, gpt_register, grok_accounts, image_tasks, system
from api.errors import install_exception_handlers
from api.support import (
    resolve_web_asset,
    should_skip_spa_fallback,
    start_account_replenish_watcher,
    start_grok_account_watcher,
    start_log_retention_watcher,
    start_limited_account_watcher,
)
from services.backup_service import backup_service
from services.config import config
from services.image_service import start_image_cleanup_scheduler


class _BodyTooLarge(Exception):
    """Internal signal: an in-flight request body exceeded the limit."""


class RequestBodyLimitMiddleware:
    """Pure-ASGI guard capping request body size (``max_request_body_mb``).

    Covers both ``Content-Length`` (rejected before a byte is read) and
    chunked/streamed bodies — multipart uploads carry no content-length — by
    counting bytes as the app receives them. 0 disables the limit.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from services.config import config as _config

        max_bytes = int(_config.max_request_body_mb) * 1024 * 1024
        if scope.get("type") != "http" or max_bytes <= 0:
            return await self.app(scope, receive, send)

        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    if int(value.strip()) > max_bytes:
                        return await self._reject(send, max_bytes)
                except ValueError:
                    pass
                break

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b"") or b"")
                if received > max_bytes:
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            # Endpoints read the full body before responding, so nothing has
            # been sent yet and we can still answer with a clean 413.
            return await self._reject(send, max_bytes)

    @staticmethod
    async def _reject(send, max_bytes: int) -> None:
        payload = json.dumps(
            {"error": f"request body exceeds {max_bytes // (1024 * 1024)} MB limit"}
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


def create_app() -> FastAPI:
    from utils.curl_tls import sanitize_curl_ssl_env

    sanitize_curl_ssl_env()
    app_version = config.app_version

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop_event = Event()
        thread = start_limited_account_watcher(stop_event)
        grok_thread = start_grok_account_watcher(stop_event)
        replenish_thread = start_account_replenish_watcher(stop_event)
        cleanup_thread = start_image_cleanup_scheduler(stop_event)
        log_trim_thread = start_log_retention_watcher(stop_event)
        backup_service.start()
        config.cleanup_old_images()
        def _warmup_tiktoken() -> None:
            try:
                from utils.tiktoken_encoding import warmup

                warmup()
            except Exception:
                return

        Thread(target=_warmup_tiktoken, daemon=True).start()
        try:
            yield
        finally:
            stop_event.set()
            thread.join(timeout=1)
            grok_thread.join(timeout=1)
            replenish_thread.join(timeout=1)
            cleanup_thread.join(timeout=1)
            log_trim_thread.join(timeout=1)
            backup_service.stop()

    app = FastAPI(title="chatgpt2api", version=app_version, lifespan=lifespan)
    install_exception_handlers(app)
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(ai.create_router())
    app.include_router(accounts.create_router())
    app.include_router(grok_accounts.create_router())
    app.include_router(gpt_register.create_router())
    app.include_router(image_tasks.create_router())
    app.include_router(system.create_router(app_version))

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_web(full_path: str):
        asset = resolve_web_asset(full_path)
        if asset is not None:
            return FileResponse(asset)
        if should_skip_spa_fallback(full_path):
            raise HTTPException(status_code=404, detail="Not Found")
        fallback = resolve_web_asset("")
        if fallback is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(fallback)

    return app
