"""Transport-level authentication for the standalone MCP HTTP server (BUG-46).

Read tools historically bypassed auth entirely — only writes called `_require_write` — so
project lists, tasks, descriptions, activity, repo topology, agent state, documents, and
board summaries were readable by anyone who could reach `/mcp`. This ASGI middleware closes
that hole by requiring an authenticated bearer principal for every MCP HTTP request, so reads
honor the same `PM_AUTH_MODE` that writes already do.

Scope of this layer is deliberately narrow — *authentication only*: it proves the caller is a
real, non-revoked principal before any tool runs. Per-project and per-scope *authorization*
still lives inside each tool (authenticate()/_require_write). In `dev-open` mode (local dev
and the hermetic test suite) it passes through unchanged, matching write behavior.
"""
from __future__ import annotations

from typing import Any

import auth
from switchboard.mcp.authorization import transport_principal_scope

_UNAUTHORIZED_BODY = (
    b'{"jsonrpc":"2.0","id":null,"error":'
    b'{"code":-32001,"message":"unauthorized: provide Authorization: Bearer <token>"}}'
)


def _bearer_from_scope(scope: dict) -> str:
    for key, value in scope.get("headers") or []:
        if key == b"authorization":
            try:
                return auth._bearer_from_header(value.decode("latin-1"))
            except Exception:
                return ""
    return ""


class MCPAuthMiddleware:
    """Require an authenticated bearer principal for every MCP HTTP request."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope, receive, send):
        # Non-HTTP scopes (lifespan, websocket) are not the exposed read surface.
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        # dev-open intentionally opens the surface for local/test runs, exactly as writes are.
        if auth.auth_mode() == auth.DEV_OPEN:
            return await self.app(scope, receive, send)
        principal = auth.principal_for_token_any_project(_bearer_from_scope(scope))
        if principal:
            # Preserve the authenticated principal across FastMCP dispatch and
            # AnyIO's worker-thread hop. The dispatcher performs project
            # authorization and installs the immutable ProjectContext.
            with transport_principal_scope(principal):
                return await self.app(scope, receive, send)
        return await self._reject(send)

    async def _reject(self, send):
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
                (b"content-length", str(len(_UNAUTHORIZED_BODY)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})
