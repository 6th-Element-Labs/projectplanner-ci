#!/usr/bin/env python3
"""UI-3: prove the Work-session health panel REST contract + UI wiring.

Surfaces the MCP-only reads (list_work_sessions, list_session_health, merge_gate,
leases) that the operator UI renders as the board/mission health strip, the per-task
Work Sessions table, and the merge-gate verdict. Builds on SESSION-8's read models.
Same "API + app.js-needle" shape as test_mission_page.py.
"""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="ws-health-panel-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  work-session health panel proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

P = "qa-ws-health"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


client = TestClient(app)

try:
    store.init_project_registry()
    store.create_project("WS Health", project_id=P, actor="test")
    store.init_db(P)

    task = store.create_task(
        {"workstream_id": "UI", "title": "Session health panel"},
        actor="test",
        project=P,
    )
    tid = task["task_id"]
    store.register_agent("codex/ws-health", "codex", lane="UI", task_id=tid, project=P)
    claim = store.claim_task(tid, "codex/ws-health", actor="test", project=P)

    preflight = {
        "schema": store.REPO_PREFLIGHT_SCHEMA,
        "verdict": "deny",
        "ok": False,
        "findings": [{
            "code": "stale_base",
            "message": "Branch is 2 commits behind origin/master.",
            "failure_class": "stale_base",
            "severity": "high",
            "blocking": True,
        }],
    }
    created = store.create_work_session(
        {
            "task_id": tid,
            "claim_id": claim["claim_id"],
            "agent_id": "codex/ws-health",
            "runtime": "codex",
            "repo_role": "canonical",
            "branch": "codex/ui-3-panel",
            "upstream": "origin/master",
            "base_sha": "1111111",
            "head_sha": "2222222",
            "worktree_path": "/tmp/ui-3-worktree",
            "storage_mode": "worktree",
            "status": "active",
            "dirty_status": "dirty",
            "hygiene": {"repo_preflight": preflight},
            "resource_leases": [{"resource_type": "port", "names": ["9111"]}],
            "policy_profile": "code_strict",
        },
        actor="test",
        project=P,
    )
    ok(not created.get("error"), "fixture Work Session created")

    # ---- Per-task Work Sessions table (Dev tab) -----------------------------
    ws = client.get("/ixp/v1/work_sessions", params={"project": P, "task_id": tid})
    ok(ws.status_code == 200, "GET /ixp/v1/work_sessions returns 200")
    ws_body = ws.json()
    rows = ws_body.get("work_sessions") or []
    ok(len(rows) == 1, "work_sessions lists the bound session for the task")
    row = rows[0]
    ok(row.get("branch") == "codex/ui-3-panel" and row.get("worktree_path"),
       "session row carries branch + workspace path for the table")
    ok(row.get("dirty_status") == "dirty" and (row.get("health") or {}).get("status") == "unsafe",
       "session row carries dirty + unsafe health for the state chips")

    # ---- Board/mission health strip -----------------------------------------
    sh = client.get("/ixp/v1/session_health", params={"project": P})
    ok(sh.status_code == 200, "GET /ixp/v1/session_health returns 200")
    sh_body = sh.json()
    ok(sh_body.get("schema") == "switchboard.session_health_list.v1",
       "session_health aggregate schema matches the strip contract")
    ok(sh_body.get("count") == 1 and sh_body.get("unsafe_count") == 1,
       "strip can count active sessions + blocked/unsafe")

    leases = client.get("/ixp/v1/leases", params={"project": P})
    ok(leases.status_code == 200 and isinstance(leases.json().get("leases"), list),
       "GET /ixp/v1/leases returns the held-leases list for the strip")

    # ---- Merge-gate verdict --------------------------------------------------
    mg = client.post("/ixp/v1/merge_gate", json={"project": P, "task_id": tid})
    ok(mg.status_code == 200, "POST /ixp/v1/merge_gate returns 200")
    mg_body = mg.json()
    ok(mg_body.get("schema") == "switchboard.merge_gate.v1",
       "merge_gate verdict schema matches the panel contract")
    ok("status" in mg_body and isinstance(mg_body.get("findings"), list),
       "merge_gate returns a pass/blocked verdict with plain-words findings")

    # ---- index.html shell ----------------------------------------------------
    index = client.get("/")
    ok(index.status_code == 200 and 'id="fleet-dock"' in index.text,
       "index.html mounts the fleet-health dock container")

    # ---- app.js wiring -------------------------------------------------------
    app_js = open(os.path.join(os.path.dirname(__file__), "static", "app.js"),
                  encoding="utf-8").read()
    for needle in (
        "renderFleetDock",       # bottom-right dock entry point
        "_loadFleetDock",        # fetch + scope (project vs deliverable)
        "_renderFleetDock",      # collapsed pill / expanded list
        "_dockReason",           # plain-language reason from health findings
        "_fleetTaskTitle",       # task-id -> title lookup
        "workSessionsPanelHtml",  # per-task Work Sessions table (Dev tab)
        "_loadWorkSessions",
        "_workSessionRow",
        "mergeGatePanelHtml",     # per-task merge-gate verdict + Re-check
        "_loadMergeGate",
        "_initMergeGate",
    ):
        ok(needle in app_js, f"app.js defines {needle}")
    ok("mode: 'deliverable'" in app_js,
       "dock is deliverable-scoped on the mission page")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nWork-session health panel proof: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
