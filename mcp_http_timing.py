"""ASGI response timing for the standalone MCP HTTP server."""
from __future__ import annotations

import time


class MCPServerTimingMiddleware:
    """Attach server timing and stateless reconnect hints to every HTTP response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        started = time.perf_counter()

        async def send_with_timing(message):
            if message.get("type") == "http.response.start":
                elapsed_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
                headers = list(message.get("headers") or [])
                headers.extend([
                    (b"server-timing", f"app;dur={elapsed_ms:.3f}".encode("ascii")),
                    (b"x-switchboard-server-ms", f"{elapsed_ms:.3f}".encode("ascii")),
                    (b"x-switchboard-mcp-session", b"stateless"),
                ])
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_timing)
