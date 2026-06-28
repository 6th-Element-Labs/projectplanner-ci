#!/usr/bin/env python3
"""Self-contained tests for the MCP agent boot/project resolver.

Run:
    python3 test_agent_bootstrap.py
"""
import json
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="agent-bootstrap-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ.pop("PM_MCP_TOKEN", None)
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
        _search_tasks=lambda args, project="maxwell": [])
    for n in ("digest", "intake", "notify", "rag", "signals"):
        _mk(n)


_stub_heavy_imports()
import store       # noqa: E402
import mcp_server  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def call_boot(**kwargs):
    return json.loads(mcp_server.prepare_agent_session(**kwargs))


try:
    store.init_project_registry()
    store.init_db("helm")
    store.init_db("switchboard")
    created = store.create_project("Vulkan", actor="test")
    ok(created.get("created") is True, "dynamic Vulkan project is created")

    seam = store.create_task({"workstream_id": "SEAM", "title": "renderer seam"},
                             project="vulkan")
    ok(seam["task_id"] == "SEAM-1", "Vulkan has SEAM-1")

    wrong = call_boot(runtime="codex", agent_id="codex/SEAM-1", project="helm",
                      task_id="SEAM-1")
    ok(wrong["ok"] is False and wrong["status"] == "project_task_mismatch",
       "explicit wrong project is rejected")
    ok(wrong["task_matches"] == ["vulkan"] and "project='vulkan'" in wrong["next_step"],
       "wrong-project response points at Vulkan")

    inferred_task = call_boot(runtime="codex", task_id="SEAM-1")
    ok(inferred_task["ok"] is True and inferred_task["selected_project"] == "vulkan",
       "task_id alone selects Vulkan")
    ok(inferred_task["lane"] == "SEAM" and inferred_task["task"]["task_id"] == "SEAM-1",
       "task selection returns lane and task brief")

    explicit = call_boot(runtime="codex", project="vulkan", task_id="SEAM-1")
    ok(explicit["ok"] is True and explicit["selected_project"] == "vulkan",
       "explicit Vulkan project validates")
    ok(any(c["tool"] == "get_task" and c["args"] == {"task_id": "SEAM-1", "project": "vulkan"}
           for c in explicit["first_calls"]), "first calls include project-bound get_task")
    ok('project="vulkan"' in explicit["startup_prompt"] and 'get_task(task_id="SEAM-1", project="vulkan")' in explicit["startup_prompt"],
       "startup prompt tells the agent to stay on Vulkan")

    inferred_lane = call_boot(runtime="codex", lane="SEAM")
    ok(inferred_lane["ok"] is True and inferred_lane["selected_project"] == "vulkan",
       "lane alone selects Vulkan")

    needs_choice = call_boot(runtime="codex")
    ok(needs_choice["ok"] is False and needs_choice["status"] == "choice_required",
       "missing project/task/lane asks the agent to choose")
    ok(any(p["id"] == "vulkan" for p in needs_choice["projects"]),
       "choice response lists dynamic projects")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
