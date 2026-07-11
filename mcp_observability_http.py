"""Plain-HTTP operator endpoint for the MCP observability snapshot (GET /observability).

Complements mcp_http_timing: any operator or monitor can scrape the per-tool
lock-wait + write-latency metrics without an MCP handshake, the same way it can read
server-timing headers. The endpoint is read-only and requires no auth (consistent with
open MCP reads and the web app's /health probes); the snapshot deliberately carries no
arguments, results, or tokens — only tool names, counts, and latency percentiles.

Optional query params: ?tool=<name> filters to one tool, ?slow_limit=<n> caps the
slow-call log (both forwarded to MCPObservability.snapshot).
"""
from __future__ import annotations

import json
from typing import Callable
from urllib.parse import parse_qs


class MCPObservabilityEndpoint:
    """ASGI shim that answers GET /observability from a snapshot provider, else passes through."""

    def __init__(self, app, snapshot: Callable[..., dict], path: str = "/observability"):
        self.app = app
        self.snapshot = snapshot
        self.path = path

    async def __call__(self, scope, receive, send):
        if (scope.get("type") == "http"
                and scope.get("method") == "GET"
                and scope.get("path") == self.path):
            return await self._respond(scope, send)
        return await self.app(scope, receive, send)

    async def _respond(self, scope, send):
        params = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
        tool = (params.get("tool") or [""])[0]
        try:
            slow_limit = int((params.get("slow_limit") or ["50"])[0])
        except (TypeError, ValueError):
            slow_limit = 50
        try:
            payload = self.snapshot(tool=tool, slow_limit=slow_limit)
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            status = 200
        except Exception as exc:  # never let a diagnostics endpoint take the server down
            body = json.dumps({"error": "observability_snapshot_failed",
                               "detail": type(exc).__name__}).encode("utf-8")
            status = 500
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"cache-control", b"no-store"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
