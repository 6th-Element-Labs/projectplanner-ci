#!/usr/bin/env python3
"""COORD-34: runner_session bind contract before Watch/Chat (UI-17)."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401
from scripts.frontend_test_source import read_frontend_source

TMP = tempfile.mkdtemp(prefix="coord34-runner-bind-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  COORD-34 proof requires optional dependency: {exc.name}")
    shutil.rmtree(TMP, ignore_errors=True)
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
        {"workstream_id": "COORD", "title": "COORD-34 bind regression"},
        actor="coord34-test", project=P)
    task_id = task["task_id"]
    host_id = "host/i-coord34"
    runner_id = "run_coord34"
    wake_id = "wake-coord34"
    claim_id = "taskclaim-coord34"
    work_session_id = "worksession-coord34"

    # ---- Store: preclaim may omit claim/work_session ----------------------------
    preclaim = store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": host_id,
        "agent_id": f"claude-code/{task_id}",
        "runtime": "claude-code",
        "task_id": task_id,
        "status": "starting",
        "cwd": "/srv/preclaim",
        "control": {"managed_process": True, "runner_kill": True, "tier": "T3"},
        "metadata": {
            "credential_admission_phase": "preclaim",
            "wake_id": wake_id,
        },
        "heartbeat_ttl_s": 60,
    }, actor=host_id, project=P)
    ok(not preclaim.get("error"), "preclaim registration allowed without claim/work_session")

    # ---- Store: claim-bound incomplete must fail closed -------------------------
    refused = store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": host_id,
        "agent_id": f"claude-code/{task_id}",
        "runtime": "claude-code",
        "task_id": task_id,
        "status": "running",
        "metadata": {
            "credential_admission_phase": "claim_bound",
            "wake_id": wake_id,
            # missing claim_id + work_session_id
        },
        "require_task_bind": True,
    }, actor=f"claude-code/{task_id}", project=P)
    ok(refused.get("error_code") == "runner_bind_incomplete",
       "incomplete claim-bound upsert returns typed runner_bind_incomplete")
    ok(refused.get("refused") is True
       and "claim_id" in (refused.get("missing") or [])
       and "work_session_id" in (refused.get("missing") or []),
       "typed refusal lists missing bind fields")

    # ---- Store: full bind succeeds; list_by_task exposes fields -----------------
    bound = store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": host_id,
        "agent_id": f"claude-code/{task_id}",
        "runtime": "claude-code",
        "task_id": task_id,
        "claim_id": claim_id,
        "status": "running",
        "cwd": "/srv/worktrees/coord34",
        "control": {"managed_process": True, "runner_kill": True, "tier": "T3"},
        "metadata": {
            "credential_admission_phase": "claim_bound",
            "wake_id": wake_id,
            "work_session_id": work_session_id,
        },
        "heartbeat_ttl_s": 1800,
    }, actor=f"claude-code/{task_id}", project=P)
    ok(bound.get("runner_session_id") == runner_id and not bound.get("error"),
       "full bind registration succeeds")

    listed = store.list_runner_sessions(task_id=task_id, project=P)
    ok(len(listed) == 1, "list_runner_sessions(task_id) returns the live runner")
    live = listed[0]
    meta = live.get("metadata") or {}
    ok(live.get("task_id") == task_id
       and live.get("claim_id") == claim_id
       and live.get("host_id") == host_id
       and meta.get("wake_id") == wake_id
       and meta.get("work_session_id") == work_session_id,
       "live runner exposes task/claim/host/wake/work_session bind fields")

    agent_state = store.get_agent_state(task_id, project=P)
    ok(agent_state.get("active_runner_session_id") == runner_id
       or (agent_state.get("switchboard/runner") or {}).get("active_runner_session_id")
       == runner_id,
       "optional agent_state active_runner_session_id pointer is set after bind")

    # ---- Watch gate: resolve_runner_watch --------------------------------------
    watchable = store.resolve_runner_watch(task_id, project=P)
    ok(watchable.get("watchable") is True
       and watchable.get("runner_session_id") == runner_id
       and watchable.get("enough_for_panel") is True,
       "resolve_runner_watch opens panel when bind is complete")

    # Incomplete bind refusal for UI-17: register a second incomplete task row
    bare_task = store.create_task(
        {"workstream_id": "COORD", "title": "COORD-34 missing bind"},
        actor="coord34-test", project=P)
    bare_id = bare_task["task_id"]
    store.upsert_runner_session({
        "runner_session_id": "run_coord34_incomplete",
        "host_id": host_id,
        "agent_id": "claude-code/incomplete",
        "runtime": "claude-code",
        "task_id": bare_id,
        "status": "starting",
        "metadata": {"credential_admission_phase": "preclaim", "wake_id": "wake-bare"},
    }, actor=host_id, project=P)
    refused_watch = store.resolve_runner_watch(bare_id, project=P)
    ok(refused_watch.get("error_code") == "runner_bind_incomplete"
       and refused_watch.get("watchable") is False
       and refused_watch.get("refused") is True,
       "UI-17 Watch gate returns typed refusal when bind is incomplete")

    empty_watch = store.resolve_runner_watch("COORD-NO-RUNNER", project=P)
    ok(empty_watch.get("error_code") == "runner_bind_incomplete"
       and empty_watch.get("refused") is True,
       "Watch gate refuses when no runner sessions exist for the task")

    # ---- REST: for_watch + dedicated watch path --------------------------------
    rest = client.get("/ixp/v1/runner_sessions", params={
        "project": P, "task_id": task_id, "for_watch": True})
    ok(rest.status_code == 200 and rest.json().get("watchable") is True,
       "GET /ixp/v1/runner_sessions?for_watch=1 returns watchable bind")

    rest_watch = client.get("/ixp/v1/runner_sessions/watch", params={
        "project": P, "task_id": bare_id})
    ok(rest_watch.status_code == 200
       and rest_watch.json().get("error_code") == "runner_bind_incomplete",
       "GET /ixp/v1/runner_sessions/watch refuses incomplete bind")

    rest_list = client.get("/ixp/v1/runner_sessions", params={
        "project": P, "task_id": task_id})
    ok(rest_list.status_code == 200
       and any(s.get("claim_id") == claim_id
               for s in (rest_list.json().get("sessions") or [])),
       "plain list_runner_sessions(task_id) is enough to locate panel target")

    # ---- Mission coordinator wake annotates awaiting bind ----------------------
    import mission_coordinator  # noqa: E402
    src = Path(ROOT) / "mission_coordinator.py"
    text = src.read_text(encoding="utf-8")
    ok("watch_gate" in text and "awaiting_runner_bind" in text
       and "watch_requires" in text,
       "mission_coordinator wake path documents awaiting_runner_bind gate")

    # ---- UI needles for UI-17 Watch open ---------------------------------------
    app_js = read_frontend_source(str(ROOT))
    for needle in (
        "openRunnerWatch",
        "runner-watch-open",
        "runner_bind_incomplete",
        "/ixp/v1/runner_sessions/watch",
        "Watch / Chat",
    ):
        ok(needle in app_js, f"app.js exposes UI-17 needle {needle}")

    mcp_src = (Path(ROOT) / "src/switchboard/mcp/tools/runner.py").read_text(encoding="utf-8")
    ok("resolve_runner_watch" in mcp_src
       and "runner_bind_incomplete" in mcp_src,
       "MCP runner tools advertise resolve_runner_watch + typed bind error")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nCOORD-34 runner bind proof: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
