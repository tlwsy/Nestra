"""Small ASGI guards that must run before request parsing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from starlette.responses import JSONResponse


class RequestBodyLimitMiddleware:
    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        max_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive, send) -> None:
        if scope["type"] != "http" or scope.get("method") not in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", ()))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = self.max_bytes + 1
        if declared > self.max_bytes:
            await JSONResponse({"detail": "request body too large"}, 413)(scope, receive, send)
            return

        messages: list[dict[str, Any]] = []
        size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > self.max_bytes:
                await JSONResponse({"detail": "request body too large"}, 413)(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        async def replay() -> dict[str, Any]:
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        await self.app(scope, replay, send)
