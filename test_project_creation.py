#!/usr/bin/env python3
"""Self-contained test for dynamic project creation.

Run:
    python3 test_project_creation.py
"""
import json
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="project-create-")
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
        _search_tasks=lambda args, project="maxwell": [],
        board_summary_text=lambda project="maxwell": "")
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


try:
    store.init_project_registry()
    store.init_db("switchboard")

    created = store.create_project("Vulkan", actor="test")
    ok(created.get("created") is True, "store.create_project creates a dynamic project")
    ok(created["project"]["id"] == "vulkan", "project id is slugified to vulkan")
    ok("vulkan" in store.project_ids(), "dynamic project appears in project_ids")
    ok(any(p["id"] == "vulkan" and p["label"] == "Vulkan" for p in store.projects()),
       "dynamic project appears in project switcher payload")
    ok(store.get_meta("project", project="vulkan") == "Vulkan",
       "dynamic project metadata is initialized")

    repo_created = store.create_project(
        "Chart Renderer", actor="test", github_repo="OpenCPN/OpenCPN")
    ok(repo_created.get("created") is True and
       repo_created["project"]["github_repo"] == "OpenCPN/OpenCPN",
       "store.create_project can set github_repo in one step")
    ok(store.get_project_github_repo("chart-renderer") == "OpenCPN/OpenCPN",
       "dynamic project resolves its configured github_repo")
    bad_repo = store.create_project(
        "Bad Repo", actor="test", github_repo="not-a-valid-repo")
    ok("error" in bad_repo and "owner/name" in bad_repo["error"] and
       "bad-repo" not in store.project_ids(),
       "invalid github_repo fails closed without creating a project")

    duplicate = store.create_project("Vulkan", actor="test")
    ok(duplicate.get("created") is False and duplicate["project"]["id"] == "vulkan",
       "duplicate create is idempotent")
    duplicate_with_repo = store.create_project(
        "Vulkan", actor="test", github_repo="StevenRidder/vulkan-renderer")
    ok(duplicate_with_repo.get("created") is False and
       duplicate_with_repo["project"]["github_repo"] == "StevenRidder/vulkan-renderer",
       "duplicate create can attach github_repo to an existing dynamic project")

    task = store.create_task({"workstream_id": "VKPLAN", "title": "root seam"}, project="vulkan")
    ok(task["task_id"] == "VKPLAN-1", "normal task creation works on dynamic project")
    ok(not any(t["task_id"] == "VKPLAN-1" for t in store.list_tasks(project="switchboard")),
       "dynamic tasks do not leak into switchboard")
    payload = store.board_payload(project="vulkan")
    ok(payload["rollups"]["total_tasks"] == 1 and payload["rollups"]["total_workstreams"] == 1,
       "dynamic project board rollups are computed from live tasks")
    ok(payload["rollups"]["status_counts"].get("Not Started") == 1 and
       payload["rollups"]["workstream_counts"].get("VKPLAN") == 1,
       "dynamic project rollups expose status and workstream counts")

    listed = json.loads(mcp_server.list_projects())
    ok(any(p["id"] == "vulkan" for p in listed["projects"]),
       "MCP list_projects includes dynamic project")
    mcp_created = json.loads(mcp_server.create_project(
        "Vulkan Renderer", None, project_id="vkrender",
        github_repo="StevenRidder/OpenCPN"))
    ok(mcp_created.get("created") is True and mcp_created["project"]["id"] == "vkrender",
       "MCP create_project creates a second dynamic project")
    ok(store.get_project_github_repo("vkrender") == "StevenRidder/OpenCPN",
       "MCP create_project wires github_repo in one step")
    mcp_task = json.loads(mcp_server.create_task("VKPLAN", "MCP-root", None, project="vkrender"))
    ok(mcp_task["task_id"] == "VKPLAN-1",
       "MCP create_task can write to a freshly created dynamic project")
    mcp_summary = mcp_server.board_summary(project="vkrender")
    ok('"total_tasks": 1' in mcp_summary and '"VKPLAN": 1' in mcp_summary,
       "MCP board_summary reports live rollups for dynamic projects")

    reserved = store.create_project("Helm", project_id="helm", actor="test")
    ok("error" in reserved and "reserved" in reserved["error"],
       "built-in project ids are reserved")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
