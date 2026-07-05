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
os.environ["PM_AUTH_MODE"] = "dev-open"
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

    seam = store.create_task({
        "workstream_id": "SEAM",
        "workstream_name": "Renderer seam",
        "title": "renderer seam",
        "description": "Define the Vulkan renderer seam from the board, not Helm docs.",
        "entry_criteria": "Vulkan project selected",
        "exit_criteria": "Command stream contract is clear",
        "deliverable": "Board-owned renderer seam contract",
    }, project="vulkan")
    ok(seam["task_id"] == "SEAM-1", "Vulkan has SEAM-1")
    store.create_task({
        "workstream_id": "SEAM",
        "workstream_name": "Renderer seam",
        "title": "adapter contract",
        "depends_on": ["SEAM-1"],
        "deliverable": "Adapter contract",
    }, project="vulkan")

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
    ok(inferred_task["project_contract"]["source_of_truth"] == "switchboard_project_contract" and
       inferred_task["project_contract"]["project_hierarchy"]["scope"] == "project",
       "boot response includes a project-level project contract")
    ok("Do not assume repo-local docs" in inferred_task["project_contract"]["local_docs_policy"],
       "project contract warns against repo-local doc assumptions")

    explicit = call_boot(runtime="codex", project="vulkan", task_id="SEAM-1")
    ok(explicit["ok"] is True and explicit["selected_project"] == "vulkan",
       "explicit Vulkan project validates")
    ok(explicit["project_contract"]["assigned_task"]["deliverable"] == "Board-owned renderer seam contract",
       "project contract includes task deliverable")
    ok(len(explicit["project_contract"]["lane"]["tasks"]) == 2,
       "project contract includes lane task list")
    ok(any(c["tool"] == "get_task" and c["args"] == {"task_id": "SEAM-1", "project": "vulkan"}
           for c in explicit["first_calls"]), "first calls include project-bound get_task")
    ok(any(c["tool"] == "get_project_contract" and c["args"]["project"] == "vulkan"
           for c in explicit["first_calls"]), "first calls include project contract read")
    ok('project="vulkan"' in explicit["startup_prompt"] and 'get_task(task_id="SEAM-1", project="vulkan")' in explicit["startup_prompt"],
       "startup prompt tells the agent to stay on Vulkan")
    ok("project_contract" in explicit["startup_prompt"] and "docs/EPICS.md" in explicit["startup_prompt"],
       "startup prompt tells agents not to assume Helm EPICS docs")

    contract = json.loads(mcp_server.get_project_contract(project="vulkan", task_id="SEAM-1"))
    ok(contract["ok"] is True and contract["project"] == "vulkan",
       "get_project_contract works for dynamic projects")
    ok(contract["lane"]["id"] == "SEAM" and contract["assigned_task"]["task_id"] == "SEAM-1",
       "get_project_contract resolves lane from task")

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
