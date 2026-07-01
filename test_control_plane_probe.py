#!/usr/bin/env python3
"""Regression smoke for Switchboard control-plane latency instrumentation."""
import json
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="control-plane-probe-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _stub_mcp_imports():
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


_stub_mcp_imports()
import store  # noqa: E402
import mcp_server  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    TestClient = None
    app = None
    _FASTAPI_SKIP = exc.name
else:
    _FASTAPI_SKIP = ""

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_db(P)
    store.create_task({"workstream_id": "BUG", "title": "latency probe"}, project=P)
    store.register_host({
        "host_id": "host/probe",
        "runtimes": [{"runtime": "codex", "lanes": ["BUG"], "capabilities": ["mcp"]}],
        "limits": {"max_sessions": 1},
    }, project=P)

    probe = store.control_plane_probe(project=P, lane="BUG")
    ok(probe["project"] == P and probe["lane"] == "BUG", "store probe preserves project and lane")
    ok(probe["server_elapsed_ms"] >= 0, "store probe reports server elapsed time")
    names = {c["name"] for c in probe["checks"]}
    ok({"activity_cursor", "list_agent_hosts", "get_lane_delta_empty"}.issubset(names),
       "store probe compares multiple cheap control-plane checks")
    ok(all(c["payload_bytes"] > 0 for c in probe["checks"]),
       "store probe reports per-check payload size")
    ok("outside Switchboard" in probe["interpretation"],
       "probe tells operators how to separate bridge/client time")

    mcp_probe = json.loads(mcp_server.control_plane_probe(project=P, lane="BUG"))
    ok(mcp_probe["mcp_framing"]["stateless_http"] is True,
       "MCP probe includes framing metadata")
    ok(mcp_probe["mcp_framing"]["approx_tool_payload_bytes"] > 0,
       "MCP probe reports serialized tool payload size")

    if TestClient is None:
        print(f"  SKIP  FastAPI endpoint smoke requires optional dependency: {_FASTAPI_SKIP}")
    else:
        client = TestClient(app)
        res = client.get("/ixp/v1/control_plane_probe", params={"project": P, "lane": "BUG"})
        ok(res.status_code == 200, "REST probe endpoint returns 200")
        ok("app;dur=" in res.headers.get("server-timing", ""),
           "REST responses include Server-Timing")
        ok(float(res.headers["x-switchboard-server-ms"]) >= 0,
           "REST responses include X-Switchboard-Server-Ms")
        ok(res.json()["checks"][0]["name"] == "activity_cursor",
           "REST probe returns the compact probe payload")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
