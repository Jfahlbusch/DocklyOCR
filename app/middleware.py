"""Starlette ASGI middleware for DocklyOCR."""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Receive, Scope, Send


class ContentLengthLimitMiddleware:
    """Enforce a Content-Length-based upload size limit.

    Returns ``413 Payload Too Large`` early when the client sends a
    ``Content-Length`` header exceeding ``max_bytes``. This is a fast short
    circuit: the request body is never read or buffered. Chunked requests
    (no ``Content-Length``) fall through to the route, which must enforce
    the limit itself after reading.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    if int(value) > self.max_bytes:
                        await _send_413(send, self.max_bytes)
                        return
                except ValueError:
                    # Malformed header — let the app handle it.
                    pass
                break

        await self.app(scope, receive, send)


async def _send_413(send: Send, max_bytes: int) -> None:
    body = json.dumps(
        {
            "error": "Payload too large",
            "max_bytes": max_bytes,
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
