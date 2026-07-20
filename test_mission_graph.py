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


def _link(task_id, title, status="Not Started", depends_on=None, terminal=False, role=None):
    detail = {
        "task_id": task_id,
        "title": title,
        "status": status,
        "depends_on": depends_on or [],
        "provenance": {"terminal": terminal} if terminal else {},
    }
    link = {"task_id": task_id, "project_id": HOME, "task_detail": detail}
    if role:
        link["role"] = role
    return link


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
    # UI-32: flat flowchart LR is the default — no workstream boxes; the env
    # escape hatch restores the boxed layout for deployments that want it.
    ok("subgraph" not in graph.get("mermaid", ""),
       "flat by default — no workstream subgraphs")
    os.environ["PM_MISSION_GRAPH_SUBGRAPHS"] = "1"
    try:
        boxed = mission_graph.build_dependency_graph([
            _link("DELIV-2", "foundation", status="Done", terminal=True),
            _link("DELIV-3", "breakdown workflow", status="Done", terminal=True, depends_on=["DELIV-2"]),
            _link("DELIV-7", "coordinator loop", status="Not Started",
                  depends_on=["DELIV-3", "EXTERNAL-1"]),
        ], deliverable_id="demo", project_id=HOME)
        ok('subgraph ws_DELIV["DELIV"]' in boxed.get("mermaid", ""),
           "PM_MISSION_GRAPH_SUBGRAPHS=1 restores workstream boxes")
        ok("subgraph ws_EXTERNAL" not in boxed.get("mermaid", ""),
           "external/singleton workstream stays loose (no one-node box)")
    finally:
        os.environ.pop("PM_MISSION_GRAPH_SUBGRAPHS", None)
    ok("<b>DELIV-2</b>" in graph.get("mermaid", ""),
       "node label puts the id in bold on its own line")
    ok(mission_graph._clean_title("FORGE-2", "FORGE-2: 20-symbol catalog") == "20-symbol catalog",
       "duplicated '<id>:' prefix stripped from title")
    ok(mission_graph._node_label("QA-2", "QA-2: QA-2: parity", "in_progress")
       == "<b>QA-2</b><br/>parity",
       "repeated id prefix collapsed; status word not baked into label text")
    ok(mission_graph._workstream("HELMWEBGPU-6") == "HELMWEBGPU"
       and mission_graph._workstream("QA-L-2") == "QA-L",
       "workstream prefix parsed from task id")
    ok(mission_graph.node_execution_state({"status": "Done", "provenance": {"terminal": True}}) == "done",
       "terminal Done maps to done")
    ok(mission_graph.node_execution_state({"status": "In Review"}) == "in_review",
       "In Review maps to in_review")
    review = mission_graph.build_dependency_graph([
        _link("REVIEW-1", "awaiting merge", status="In Review"),
    ], deliverable_id="review-colors", project_id=HOME)
    ok("classDef reviewNode fill:#ffe083,stroke:#e0a800" in review.get("mermaid", ""),
       "In Review graph node uses the yellow task-status palette")
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

    # ---- unit: context roles stay off the flow map ---------------------------
    ctx = mission_graph.build_dependency_graph([
        _link("FLOW-1", "exec entry", status="In Progress"),
        _link("FLOW-2", "exec next", depends_on=["FLOW-1", "BASE-1", "OUT-1"]),
        _link("BASE-1", "shipped groundwork", status="Done", terminal=True, role="foundation"),
        _link("BASE-2", "unrelated groundwork", status="Done", terminal=True,
              depends_on=["OLD-1"], role="foundation"),
        _link("FROZEN-1", "parked track", role="parked"),
    ], deliverable_id="ctx", project_id=HOME)
    ctx_ids = {n["id"] for n in ctx["nodes"]}
    ok(ctx_ids == {"FLOW-1", "FLOW-2", "BASE-1", "OUT-1"},
       "flow map keeps executable tasks, promotes depended-on context, drops the rest")
    ok(all(cid not in ctx.get("mermaid", "") for cid in ("BASE-2", "FROZEN-1", "OLD-1")),
       "unreferenced foundation/parked links and their upstream deps stay off the mermaid")
    promoted = next(n for n in ctx["nodes"] if n["id"] == "BASE-1")
    ok(not promoted.get("external") and promoted.get("state") == "done",
       "promoted context task renders as a real linked node with its state")
    ok([c["id"] for c in ctx.get("context_nodes") or []] == ["BASE-2", "FROZEN-1"],
       "context_nodes lists the excluded foundation/parked links")
    ok(ctx.get("stats", {}).get("context_task_count") == 2,
       "stats count excluded context links")

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

    # Mixed-case ids: linking normalizes to uppercase, so the task lookup must
    # resolve case-insensitively (CONTRACT-5b regression).
    cased = store.get_task(base["task_id"].lower(), project=HOME)
    ok(bool(cased) and cased.get("task_id") == base["task_id"],
       "get_task resolves case-insensitively to the canonical id")
    relink = store.link_task_to_deliverable(
        deliverable["id"], HOME, base["task_id"].lower(), actor="test", project=HOME)
    ok(not relink.get("error"), "linking with mismatched id casing succeeds")

    # Auto-classified link roles: groundwork already Done at link time defaults
    # to 'foundation' (context row), live tasks to 'contributes' (flow), and an
    # explicit role always wins.
    ground = store.create_task({"workstream_id": "GRAPH", "title": "Shipped groundwork"},
                               actor="test", project=HOME)
    store.mark_task_merged(ground["task_id"], "cafebabe" * 5, pr_number=101, project=HOME)
    store.link_task_to_deliverable(deliverable["id"], HOME, ground["task_id"],
                                   actor="test", project=HOME)
    roles = {l["task_id"]: l.get("role")
             for l in (store.get_deliverable(deliverable["id"], project=HOME)
                       or {}).get("task_links") or []}
    ok(roles.get(ground["task_id"].upper()) == "foundation",
       "Done task linked without a role auto-classifies as foundation")
    ok(roles.get(blocked["task_id"].upper()) == "contributes",
       "live task linked without a role stays contributes")
    g3 = store.get_deliverable_dependency_graph(project=HOME, deliverable_id=deliverable["id"])
    ok(all(n["id"] != ground["task_id"].upper() for n in g3.get("nodes") or [])
       and any(c["id"] == ground["task_id"].upper() for c in g3.get("context_nodes") or []),
       "auto-foundation link lands in context_nodes, not the flow graph")
    explicit = store.create_task({"workstream_id": "GRAPH", "title": "Done but explicit flow"},
                                 actor="test", project=HOME)
    store.mark_task_merged(explicit["task_id"], "deadbeef" * 5, pr_number=102, project=HOME)
    store.link_task_to_deliverable(deliverable["id"], HOME, explicit["task_id"],
                                   data={"role": "contributes"}, actor="test", project=HOME)
    roles2 = {l["task_id"]: l.get("role")
              for l in (store.get_deliverable(deliverable["id"], project=HOME)
                        or {}).get("task_links") or []}
    ok(roles2.get(explicit["task_id"].upper()) == "contributes",
       "explicit role wins over auto-classification")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nmission graph: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
