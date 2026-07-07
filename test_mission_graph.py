#!/usr/bin/env python3
"""Strategic dependency graph — task-level depends_on DAG for deliverables."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="mission-graph-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mission_graph  # noqa: E402
import store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  mission graph proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

HOME = "qa-graph-home"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _link(task_id, title, status="Not Started", depends_on=None, terminal=False):
    detail = {
        "task_id": task_id,
        "title": title,
        "status": status,
        "depends_on": depends_on or [],
        "provenance": {"terminal": terminal} if terminal else {},
    }
    return {"task_id": task_id, "project_id": HOME, "task_detail": detail}


client = TestClient(app)

try:
    # ---- unit: graph builder ------------------------------------------------
    graph = mission_graph.build_dependency_graph([
        _link("DELIV-2", "foundation", status="Done", terminal=True),
        _link("DELIV-3", "breakdown workflow", status="Done", terminal=True, depends_on=["DELIV-2"]),
        _link("DELIV-7", "coordinator loop", status="Not Started",
              depends_on=["DELIV-3", "EXTERNAL-1"]),
    ], deliverable_id="demo", project_id=HOME)
    ok(graph.get("schema") == mission_graph.GRAPH_SCHEMA, "graph schema v1")
    ok(len(graph.get("nodes") or []) == 4, "graph includes internal + external nodes")
    ok(any(n.get("id") == "EXTERNAL-1" and n.get("external") for n in graph["nodes"]),
       "external depends_on renders stub node")
    ok(len(graph.get("edges") or []) == 3, "depends_on edges materialized")
    ok("DELIV-2" in graph.get("mermaid", "") and "flowchart LR" in graph.get("mermaid", ""),
       "mermaid flowchart LR emitted")
    ok("subgraph" not in graph.get("mermaid", ""),
       "no per-workstream subgraph clusters (clean layered DAG)")
    ok(mission_graph.node_execution_state({"status": "Done", "provenance": {"terminal": True}}) == "done",
       "terminal Done maps to done")
    ok(mission_graph.node_execution_state({"status": "In Review"}) == "in_review",
       "In Review maps to in_review")
    ok(mission_graph.node_execution_state({"status": "Not Started"}) == "todo",
       "Not Started maps to todo")
    ok(mission_graph.node_execution_state({"status": "Done"}) == "done_unproven",
       "Done without merge proof maps to done_unproven")
    ok(mission_graph.node_execution_state({"status": "In Progress"}) == "in_progress",
       "In Progress maps to in_progress")
    ok(mission_graph.node_execution_state({"status": "Blocked"}) == "blocked",
       "Blocked maps to blocked")
    prog = mission_graph.build_dependency_graph([
        _link("WIP-1", "active work", status="In Progress"),
        _link("DONE-1", "shipped", status="Done", terminal=True),
        _link("CLAIM-1", "claimed done", status="Done"),
    ], deliverable_id="colors", project_id=HOME)
    pstats = prog.get("stats", {})
    ok(pstats.get("in_progress_count") == 1 and pstats.get("done_count") == 1
       and pstats.get("done_unproven_count") == 1,
       "stats break out in_progress / done / done_unproven")
    ok("progressNode" in prog.get("mermaid", "") and "doneUnprovenNode" in prog.get("mermaid", ""),
       "mermaid emits in_progress + done_unproven classes")

    # ---- integration: REST + store ------------------------------------------
    store.init_project_registry()
    store.create_project("Graph Home", project_id=HOME, actor="test")
    store.init_db(HOME)
    board = store.create_project_board(
        {"id": "graph-mission", "title": "Graph mission", "kind": "mission", "status": "active"},
        actor="test", project=HOME)
    deliverable = store.create_deliverable(
        {"id": "graph-mission", "board_id": board["id"], "title": "Graph mission", "status": "in_progress"},
        actor="test", project=HOME)
    base = store.create_task({"workstream_id": "GRAPH", "title": "Base"}, actor="test", project=HOME)
    child = store.create_task(
        {"workstream_id": "GRAPH", "title": "Child", "depends_on": [base["task_id"]]},
        actor="test", project=HOME)
    blocker = store.create_task({"workstream_id": "HARDEN", "title": "Outside blocker"},
                                actor="test", project=HOME)
    blocked = store.create_task(
        {"workstream_id": "GRAPH", "title": "Blocked child",
         "depends_on": [child["task_id"], blocker["task_id"]]},
        actor="test", project=HOME)
    for tid in (base["task_id"], child["task_id"], blocked["task_id"]):
        store.link_task_to_deliverable(deliverable["id"], HOME, tid, actor="test", project=HOME)
    store.update_task(base["task_id"], {"status": "Done"}, actor="test", project=HOME)

    rest = client.get(f"/api/deliverables/{deliverable['id']}/dependency_graph", params={"project": HOME})
    ok(rest.status_code == 200, "GET dependency_graph returns 200")
    body = rest.json()
    ok(body.get("schema") == mission_graph.GRAPH_SCHEMA, "REST dependency_graph schema")
    ids = {n["id"] for n in body.get("nodes") or []}
    ok(base["task_id"].upper() in ids and child["task_id"].upper() in ids,
       "linked tasks appear as nodes")
    ok(blocker["task_id"].upper() in ids, "external blocker appears as stub node")
    ok(any(e.get("from") == base["task_id"].upper() and e.get("to") == child["task_id"].upper()
           for e in body.get("edges") or []), "depends_on edge direction is dependency -> dependent")

    stored = store.get_deliverable_dependency_graph(project=HOME, deliverable_id=deliverable["id"])
    ok(stored.get("stats", {}).get("external_node_count", 0) >= 1,
       "store graph stats count external blockers")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nmission graph: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
