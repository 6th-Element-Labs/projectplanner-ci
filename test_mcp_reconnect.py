#!/usr/bin/env python3
"""BUG-38 — MCP HTTP timing and stateless reconnect contract tests."""
import asyncio

from mcp_http_timing import MCPServerTimingMiddleware


def check(condition, message):
    if not condition:
        raise AssertionError(message)


async def fake_app(scope, receive, send):
    await send({
        "type": "http.response.start",
        "status": 503,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": b'{"error":"overloaded"}'})


async def exercise_http():
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    middleware = MCPServerTimingMiddleware(fake_app)
    await middleware({"type": "http", "method": "POST", "path": "/mcp"}, receive, send)
    return messages


messages = asyncio.run(exercise_http())
start = messages[0]
headers = dict(start["headers"])
check(start["status"] == 503, "middleware preserves fast error status")
check(b"server-timing" in headers and headers[b"server-timing"].startswith(b"app;dur="),
      "every MCP HTTP response carries Server-Timing")
check(float(headers[b"x-switchboard-server-ms"]) >= 0,
      "every MCP HTTP response carries machine-readable server elapsed time")
check(headers[b"x-switchboard-mcp-session"] == b"stateless",
      "response advertises the fresh-session reconnect contract")


async def exercise_lifespan():
    called = []

    async def app(scope, receive, send):
        called.append(scope["type"])

    middleware = MCPServerTimingMiddleware(app)
    await middleware({"type": "lifespan"}, None, None)
    return called


check(asyncio.run(exercise_lifespan()) == ["lifespan"],
      "non-HTTP ASGI scopes pass through unchanged")

print("MCP reconnect contract tests passed")
