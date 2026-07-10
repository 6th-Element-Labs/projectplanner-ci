#!/usr/bin/env python3
"""ACCESS-3 MCP scoped-token lifecycle regression."""
import json
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="access-token-mcp-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "required"
os.environ["PM_MCP_TOKEN"] = "admin-mcp-token"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _stub_heavy_imports():
    class _FastMCP:
        def __init__(self, *a, **k): pass
        def tool(self, *a, **k):
            return lambda f: f
        def __getattr__(self, n): return lambda *a, **k: None

    def _mk(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m

    _mk("mcp"); _mk("mcp.server")
    _mk("mcp.server.fastmcp", Context=object, FastMCP=_FastMCP)
    _mk("mcp.server.transport_security",
        TransportSecuritySettings=type("TSS", (), {"__init__": lambda self, *a, **k: None}))
    _mk("agent", _task_brief=lambda t, full=False: t, run=lambda *a, **k: {},
        _search_tasks=lambda args, project="maxwell": [],
        board_summary_text=lambda project="maxwell": "")
    for n in ("digest", "intake", "notify", "rag", "signals"):
        _mk(n)


class _Headers:
    def get(self, name, default=""):
        return "Bearer admin-mcp-token" if name.lower() == "authorization" else default


class _Request:
    headers = _Headers()


class _RequestContext:
    request = _Request()


class _Ctx:
    request_context = _RequestContext()


_stub_heavy_imports()
import auth        # noqa: E402
import mcp_server  # noqa: E402


P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    ctx = _Ctx()
    created = json.loads(mcp_server.create_scoped_token(
        ctx, project=P, kind="host", display_name="local host", role="operator"))
    token = created.get("token")
    principal = created.get("principal") or {}
    ok(bool(token) and principal.get("kind") == "host",
       "MCP can create a host token from a role preset")
    ok(created.get("token_returned_once") is True and "token_hash" not in principal,
       "MCP create token returns a redacted principal and one-time token")

    authenticated = auth.authenticate(P, token, ("write:ixp",))
    ok(authenticated["id"] == principal["id"], "MCP-created token authenticates for protocol writes")

    listed = json.loads(mcp_server.list_scoped_tokens(ctx, project=P))
    serialized = json.dumps(listed, sort_keys=True)
    ok(any(p["id"] == principal["id"] for p in listed.get("tokens") or []),
       "MCP can list scoped token principals")
    ok(token not in serialized and "token_hash" not in serialized,
       "MCP token list redacts raw tokens and hashes")

    bad_scope = json.loads(mcp_server.create_scoped_token(
        ctx, project=P, kind="agent", display_name="bad", scopes="read,write:root"))
    ok("unknown scope" in bad_scope.get("error", ""), "MCP token creation rejects unknown scopes")

    revoked = json.loads(mcp_server.revoke_scoped_token(principal["id"], ctx, project=P))
    ok(revoked.get("revoked") is True, "MCP can revoke a scoped token")
    try:
        auth.authenticate(P, token, ("write:ixp",))
        still_authenticates = True
    except PermissionError:
        still_authenticates = False
    ok(not still_authenticates, "MCP-revoked token stops authenticating")

    global_created = json.loads(mcp_server.create_scoped_token(
        ctx, project="*", kind="agent", display_name="global cloud agent", role="admin",
        principal_id="agent-global-cloud-test"))
    global_token = global_created.get("token")
    global_principal = global_created.get("principal") or {}
    ok(global_created.get("project") == "*" and global_principal.get("project") == "*",
       "MCP can mint a global project='*' token")
    helm_auth = auth.authenticate("helm", global_token, ("write:tasks",))
    ok(helm_auth["id"] == global_principal["id"],
       "global token authenticates for writes on another board")
    maxwell_auth = auth.authenticate("maxwell", global_token, ("write:tasks",))
    ok(maxwell_auth["id"] == global_principal["id"],
       "global token authenticates for writes on maxwell")
    mcp_server.revoke_scoped_token(global_principal["id"], ctx, project=P)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
