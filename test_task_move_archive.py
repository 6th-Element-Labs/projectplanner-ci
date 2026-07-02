#!/usr/bin/env python3
"""Regression tests for audited task move/archive tools.

Run:
    python3 test_task_move_archive.py
"""
import json
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="task-move-archive-")
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


try:
    store.init_project_registry()
    store.init_db("switchboard")
    store.create_project("Vulkan", actor="test")
    store.init_db("vulkan")

    root = store.create_task({"workstream_id": "ROOT", "title": "source-only dep"},
                             actor="test", project="switchboard")
    moved = store.create_task({
        "workstream_id": "MOVE",
        "title": "leaked renderer task",
        "depends_on": [root["task_id"]],
    }, actor="test", project="switchboard")
    store.add_comment(moved["task_id"], "test", "preserve this activity",
                      project="switchboard")
    store.update_task(moved["task_id"], {"status": "In Review"}, actor="test",
                      project="switchboard")
    git = store.mark_task_default_branch_commit(
        moved["task_id"], "abcdef1234567890", branch="master",
        subject="MOVE-1 evidence", actor="test", project="switchboard")
    spend = store.report_usage("agent_reported", "reported", task_id=moved["task_id"],
                               agent_id="agent/test", model="mock", cost_usd=0.12,
                               total_tokens=42, project="switchboard")
    outcome = store.record_outcome("feature", "Moved task outcome",
                                   task_id=moved["task_id"], status="verified",
                                   actor="test", project="switchboard")
    kpi = store.create_kpi("Delivered value", "points", "increase",
                           baseline_value=0, current_value=0, target_value=10,
                           actor="test", project="switchboard")
    link = store.link_outcome_to_kpi(outcome["id"], kpi["id"], contribution=2,
                                    confidence="measured", actor="test",
                                    project="switchboard")

    refused = json.loads(mcp_server.move_task(
        moved["task_id"], "switchboard", "vulkan", None, reason="wrong board"))
    ok(refused.get("error") == "destination is missing dependency id(s)",
       "move_task refuses dangling destination dependencies by default")
    ok(store.get_task(moved["task_id"], project="switchboard") is not None,
       "refused move leaves source task in place")

    moved_res = json.loads(mcp_server.move_task(
        moved["task_id"], "switchboard", "vulkan", None,
        reason="wrong board", dependency_policy="clear"))
    ok(moved_res.get("moved") is True, "move_task succeeds with explicit dependency clear")
    ok(moved_res.get("cleared_dependencies") == [root["task_id"]],
       "move_task reports cleared dependency edges")
    ok(store.get_task(moved["task_id"], project="switchboard") is None,
       "move_task removes source active task")
    dest_task = store.get_task(moved["task_id"], project="vulkan")
    ok(dest_task is not None and dest_task["depends_on"] == [],
       "move_task creates destination task and clears requested deps")
    ok(any((a.get("payload") or {}).get("text") == "preserve this activity"
           for a in dest_task.get("activity", [])),
       "move_task preserves activity history")
    ok(dest_task["git_state"].get("head_sha") == git["git_state"].get("head_sha"),
       "move_task preserves git provenance")
    tally = store.task_tally(moved["task_id"], project="vulkan")
    ok(round(tally["spend"]["cost_usd"], 2) == round(spend["cost_usd"], 2),
       "move_task preserves task spend")
    ok(any(o["id"] == outcome["id"] for o in tally["outcome_records"]),
       "move_task preserves task outcomes")
    ok(any(o["id"] == outcome["id"] and o["project"] == "vulkan"
           for o in tally["outcome_records"]),
       "move_task rewrites outcome project field")
    ok(any(group["kpi_id"] == kpi["id"] for group in tally["kpis"]),
       "move_task preserves KPI context for moved outcome links")
    ok(any(item["id"] == link["id"] for group in tally["kpis"]
           for item in group["links"]),
       "move_task preserves outcome-to-KPI link rows")
    archived = store.get_archived_task(moved_res["archive_id"], project="switchboard")
    ok(archived and archived["operation"] == "move_out",
       "move_task writes a source archive record")

    conflict = store.create_task({"workstream_id": "MOVE", "title": "source conflict"},
                                 actor="test", project="switchboard")
    store.create_task({"workstream_id": "MOVE", "title": "dest conflict"},
                      actor="test", project="vulkan")
    conflict_res = store.move_task(conflict["task_id"], "switchboard", "vulkan",
                                   actor="test", dependency_policy="clear")
    ok(conflict_res.get("error") == "destination task id already exists",
       "move_task reports destination task-id conflicts explicitly")
    ok(store.get_task(conflict["task_id"], project="switchboard") is not None,
       "conflicted move leaves source task in place")

    archived_task = store.create_task({"workstream_id": "ARCH", "title": "archive me"},
                                      actor="test", project="switchboard")
    store.add_comment(archived_task["task_id"], "test", "archived activity",
                      project="switchboard")
    archive_res = json.loads(mcp_server.archive_task(
        archived_task["task_id"], None, project="switchboard", reason="cleanup"))
    ok(archive_res.get("archived") is True, "archive_task archives through MCP")
    ok(store.get_task(archived_task["task_id"], project="switchboard") is None,
       "archive_task removes active task row")
    ok(store.get_archived_task(archive_res["archive_id"], project="switchboard") is not None,
       "archive_task persists archived snapshot")

    held = store.create_task({"workstream_id": "HELD", "title": "has a lease"},
                             actor="test", project="switchboard")
    store.claim_files("agent/test", ["store.py"], task_id=held["task_id"],
                      project="switchboard")
    held_res = store.archive_task(held["task_id"], actor="test", project="switchboard")
    ok(held_res.get("error") == "task has active claims or leases",
       "archive_task refuses tasks with active file leases")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
