#!/usr/bin/env python3
"""SESSION-8 Work Session health read-model tests."""
import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="session-health-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent  # noqa: E402
import store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  session health REST proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


client = TestClient(app)

try:
    store.init_db(P)
    task = store.create_task(
        {"workstream_id": "SESSION", "title": "unsafe session rollup"},
        actor="test",
        project=P,
    )
    store.register_agent(
        "codex/SESSION-health",
        "codex",
        lane="SESSION",
        task_id=task["task_id"],
        project=P,
    )
    claim = store.claim_task(
        task["task_id"],
        "codex/SESSION-health",
        actor="test",
        project=P,
    )
    preflight = {
        "schema": store.REPO_PREFLIGHT_SCHEMA,
        "verdict": "deny",
        "ok": False,
        "findings": [
            {
                "code": "stale_base",
                "message": "Branch is 2 commits behind origin/master.",
                "failure_class": "stale_base",
                "severity": "high",
                "blocking": True,
            }
        ],
    }
    created = store.create_work_session(
        {
            "task_id": task["task_id"],
            "claim_id": claim["claim_id"],
            "agent_id": "codex/SESSION-health",
            "runtime": "codex",
            "repo_role": "canonical",
            "branch": "codex/SESSION-health",
            "upstream": "origin/master",
            "base_sha": "1111111",
            "head_sha": "2222222",
            "worktree_path": "/tmp/session-health-worktree",
            "storage_mode": "worktree",
            "status": "active",
            "dirty_status": "dirty",
            "conflict_marker_count": 1,
            "hygiene": {"repo_preflight": preflight},
            "policy_profile": "code_strict",
        },
        actor="test",
        project=P,
    )
    session = created["work_session"]
    ok(session["health"]["schema"] == store.WORK_SESSION_HEALTH_SCHEMA,
       "Work Session row includes typed health")
    ok(session["health"]["status"] == "unsafe" and session["health"]["blocking"],
       "dirty/conflicted/failed-preflight session is unsafe")
    codes = {f["code"] for f in session["health"]["findings"]}
    ok({"dirty_work_session", "conflict_markers", "work_session_preflight_failed", "stale_base"} <= codes,
       "health carries typed dirty/conflict/preflight/stale findings")

    detail = store.get_task(task["task_id"], project=P)
    health = detail["session_health"]
    ok(health["schema"] == store.TASK_SESSION_HEALTH_SCHEMA and
       health["status"] == "unsafe" and health["unsafe_session_count"] == 1,
       "task detail exposes unsafe session aggregate")
    ok(any(f["kind"] == "unsafe_session" for f in health["findings"]),
       "task detail exposes unsafe_session findings")
    brief = agent._task_brief(detail, full=True)
    ok((brief.get("session_health") or {}).get("status") == "unsafe",
       "MCP task brief includes session_health")

    listed = store.list_session_health(project=P, task_id=task["task_id"])
    ok(listed["schema"] == "switchboard.session_health_list.v1" and
       listed["unsafe_count"] == 1 and
       listed["task_session_health"]["status"] == "unsafe",
       "list_session_health exposes session and task aggregates")

    one = store.get_work_session_health(session["work_session_id"], project=P)
    ok(one["status"] == "unsafe" and one["work_session_id"] == session["work_session_id"],
       "get_work_session_health returns the session verdict")

    board = store.create_project_board(
        {"id": "session-health-mission", "title": "Session health mission", "kind": "mission"},
        actor="test",
        project=P,
    )
    deliverable = store.create_deliverable(
        {
            "id": "session-health-mission",
            "board_id": board["id"],
            "title": "Session health mission",
            "status": "in_progress",
            "end_state": "Operators can see unsafe sessions live.",
        },
        actor="test",
        project=P,
    )
    store.link_task_to_deliverable(
        deliverable["id"],
        P,
        task["task_id"],
        data={"role": "implementation", "blocks_deliverable": True},
        actor="test",
        project=P,
    )
    mission = store.get_mission_status(project=P, deliverable_id=deliverable["id"])
    ok(any(b.get("kind") == "unsafe_session" and b.get("finding_code") == "dirty_work_session"
           for b in mission.get("blockers") or []),
       "mission status promotes unsafe session to typed blocker")
    ok((mission.get("active_work") or [])[0]["session_health"]["status"] == "unsafe",
       "active work carries session_health into mission cockpit")
    brief_result = store.generate_mission_brief(
        project=P, deliverable_id=deliverable["id"], actor="test", persist=False)
    summary = (brief_result.get("mission_brief") or {}).get("summary_markdown") or ""
    ok("session health: unsafe" in summary,
       "generated mission brief narrates unsafe session health")

    res = client.get(
        f"/ixp/v1/work_sessions/{session['work_session_id']}/health",
        params={"project": P},
    )
    ok(res.status_code == 200 and res.json().get("status") == "unsafe",
       "REST Work Session health endpoint returns unsafe verdict")
    res = client.get(
        "/ixp/v1/session_health",
        params={"project": P, "task_id": task["task_id"], "only_unsafe": "true"},
    )
    ok(res.status_code == 200 and res.json().get("count") == 1,
       "REST session_health list supports task filter and unsafe filter")

    completed = store.update_work_session(
        session["work_session_id"],
        {"status": "completed"},
        actor="test",
        project=P,
    )
    ok(completed["updated"], "completed historical session update succeeds")
    completed_detail = store.get_task(task["task_id"], project=P)
    ok(completed_detail["session_health"]["status"] in {"warning", "healthy"} and
       completed_detail["session_health"]["unsafe_session_count"] == 0,
       "completed historical sessions no longer make task session_health unsafe")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nSession health proof: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
