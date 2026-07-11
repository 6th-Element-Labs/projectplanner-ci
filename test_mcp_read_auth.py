"""BUG-46 — MCP transport authentication.

Reads used to bypass auth entirely, exposing project/task/activity data to any anonymous
caller of /mcp. These tests pin the fix: MCPAuthMiddleware requires an authenticated bearer
principal for every MCP HTTP request when PM_AUTH_MODE=required, passes through in dev-open,
and never gates non-HTTP scopes.
"""
import asyncio
import json
import os

os.environ["PM_MCP_TOKEN"] = "bug46-valid-token"

import auth  # noqa: E402
from mcp_auth import MCPAuthMiddleware  # noqa: E402


class _StubApp:
    """Inner ASGI app that records whether it was reached and returns 200."""

    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _drive(mw, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.new_event_loop().run_until_complete(mw(scope, receive, send))
    return sent


def _http_scope(bearer=None):
    headers = []
    if bearer is not None:
        headers.append((b"authorization", f"Bearer {bearer}".encode("ascii")))
    return {"type": "http", "headers": headers}


def _status(sent):
    for message in sent:
        if message.get("type") == "http.response.start":
            return message.get("status")
    return None


def test_anonymous_read_is_rejected():
    os.environ["PM_AUTH_MODE"] = "required"
    app = _StubApp()
    sent = _drive(MCPAuthMiddleware(app), _http_scope(bearer=None))
    assert not app.called, "anonymous request must not reach any tool"
    assert _status(sent) == 401, f"expected 401, got {_status(sent)}"
    body = b"".join(m.get("body", b"") for m in sent if m.get("type") == "http.response.body")
    assert json.loads(body)["error"]["message"].startswith("unauthorized"), body


def test_invalid_token_is_rejected():
    os.environ["PM_AUTH_MODE"] = "required"
    app = _StubApp()
    sent = _drive(MCPAuthMiddleware(app), _http_scope(bearer="not-a-real-token"))
    assert not app.called, "invalid token must not reach any tool"
    assert _status(sent) == 401, f"expected 401, got {_status(sent)}"


def test_valid_token_passes_through():
    os.environ["PM_AUTH_MODE"] = "required"
    app = _StubApp()
    sent = _drive(MCPAuthMiddleware(app), _http_scope(bearer="bug46-valid-token"))
    assert app.called, "an authenticated principal must reach the tool app"
    assert _status(sent) == 200, f"expected 200, got {_status(sent)}"


def test_dev_open_mode_passes_without_token():
    os.environ["PM_AUTH_MODE"] = "dev-open"
    app = _StubApp()
    sent = _drive(MCPAuthMiddleware(app), _http_scope(bearer=None))
    assert app.called, "dev-open must not gate reads (local/test parity with writes)"
    assert _status(sent) == 200


def test_non_http_scope_is_never_gated():
    os.environ["PM_AUTH_MODE"] = "required"
    app = _StubApp()
    _drive(MCPAuthMiddleware(app), {"type": "lifespan"})
    assert app.called, "lifespan/websocket scopes must pass through untouched"


def test_helper_rejects_empty_and_accepts_env_token():
    assert auth.principal_for_token_any_project("") is None
    assert auth.principal_for_token_any_project("   ") is None
    principal = auth.principal_for_token_any_project("bug46-valid-token")
    assert principal and principal.get("id") == "env-mcp-token", principal


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\nPASS  {len(tests)} tests")
