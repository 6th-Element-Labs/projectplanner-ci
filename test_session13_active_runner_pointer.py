#!/usr/bin/env python3
"""SESSION-13: active runner pointer lifecycle and authoritative fallback."""
import os
import shutil
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="session-13-runner-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def runner(task_id, runner_id, host_id, status="running", heartbeat_at=None):
    return store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": host_id,
        "agent_id": f"codex/{task_id}",
        "runtime": "codex",
        "task_id": task_id,
        "claim_id": f"claim-{runner_id}",
        "status": status,
        "cwd": f"/tmp/{runner_id}",
        "control": {"managed_process": True, "runner_kill": True, "tier": "T3"},
        "metadata": {"wake_id": f"wake-{runner_id}",
                     "work_session_id": f"worksession-{runner_id}"},
        "heartbeat_at": heartbeat_at or time.time(),
        "heartbeat_ttl_s": 60,
    }, actor="session-13-test", project=P)


try:
    store.init_db(P)
    task = store.create_task(
        {"workstream_id": "SESSION", "title": "active runner pointer"},
        actor="session-13-test", project=P)
    task_id = task["task_id"]

    first = runner(task_id, "runner-first", "host/one")
    state = store.get_agent_state(task_id, project=P)
    nested = state.get("switchboard/runner") or {}
    ok(first.get("runner_session_id") == "runner-first"
       and state.get("active_runner_session_id") == "runner-first"
       and state.get("active_runner_host_id") == "host/one"
       and nested.get("host_id") == "host/one",
       "successful bound registration stores session and real host pointers")

    resolved = store.resolve_task_active_runner(
        task_id, agent_state=state, project=P)
    ok(resolved.get("active") is True
       and resolved.get("source") == "agent_state_pointer"
       and (resolved.get("session") or {}).get("runner_session_id") == "runner-first",
       "Mission resolves a valid agent_state pointer first")

    with store._conn(P) as c:
        c.execute("UPDATE tasks SET agent_state='{}', updated_at=? WHERE task_id=?",
                  (time.time(), task_id))
    fallback = store.resolve_task_active_runner(task_id, agent_state={}, project=P)
    ok(fallback.get("active") is True
       and fallback.get("source") == "runner_sessions_fallback"
       and (fallback.get("session") or {}).get("runner_session_id") == "runner-first",
       "Mission falls back to authoritative runner_sessions when pointer is absent")

    # Recreate the pointer, then prove an older exit cannot erase a newer runner.
    runner(task_id, "runner-first", "host/one")
    runner(task_id, "runner-second", "host/two")
    runner(task_id, "runner-first", "host/one", status="exited")
    state = store.get_agent_state(task_id, project=P)
    ok(state.get("active_runner_session_id") == "runner-second"
       and state.get("active_runner_host_id") == "host/two",
       "terminal update for an older runner preserves the newer pointer")

    runner(task_id, "runner-second", "host/two", status="exited")
    state = store.get_agent_state(task_id, project=P)
    ok("active_runner_session_id" not in state
       and "active_runner_host_id" not in state
       and "switchboard/runner" not in state,
       "terminal runner exit clears its matching convenience pointer")

    stale = runner(task_id, "runner-stale", "host/stale",
                   heartbeat_at=time.time() - 120)
    state = store.get_agent_state(task_id, project=P)
    resolution = store.resolve_task_active_runner(task_id, agent_state=state, project=P)
    ok(stale.get("stale") is True
       and state.get("active_runner_session_id") != "runner-stale"
       and resolution.get("active") is False,
       "stale registration is never promoted as the active runner")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
