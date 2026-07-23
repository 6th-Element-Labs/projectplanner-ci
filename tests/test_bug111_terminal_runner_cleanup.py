#!/usr/bin/env python3
"""BUG-111: terminal tasks release host runners and Work Sessions."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="bug111-terminal-cleanup-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from adapters import agent_host  # noqa: E402
from db.connection import _conn  # noqa: E402


P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


try:
    store.init_db(P)
    task = store.create_task({
        "workstream_id": "BUG", "title": "terminal runner cleanup proof",
        "status": "Not Started", "ui_impact": "no",
    }, actor="bug111-test", project=P)
    task_id = task["task_id"]
    host_id = "host/bug111-mac"
    principal_id = "principal/bug111-host"
    runner_id = "run_bug111_terminal"
    work_session = store.create_work_session({
        "agent_id": f"codex/{task_id}", "task_id": task_id,
        "repo_role": "canonical", "branch": f"codex/{task_id}-proof",
        "storage_mode": "worktree", "worktree_path": str(TMP),
        "status": "active", "dirty_status": "clean",
        "policy_profile": "code_strict",
        "hygiene": {"repo_preflight": {"ok": True, "verdict": "pass", "findings": []}},
    }, actor="bug111-test", project=P)["work_session"]
    store.register_host({
        "host_id": host_id, "agent_host_version": "0.2.25",
        "runtimes": [{"runtime": "codex", "lanes": ["BUG"]}],
        "limits": {"max_sessions": 8},
        "capacity": {"active_sessions": 1, "headroom": 7},
        "heartbeat_ttl_s": 60,
    }, principal_id=principal_id, actor=host_id, project=P)
    store.upsert_runner_session({
        "runner_session_id": runner_id, "host_id": host_id,
        "agent_id": f"codex/{task_id}", "runtime": "codex",
        "task_id": task_id, "status": "running", "pid": 111,
        "metadata": {"work_session_id": work_session["work_session_id"]},
    }, principal_id=principal_id, actor=host_id, project=P)
    with _conn(P) as c:
        c.execute("UPDATE tasks SET status='Done' WHERE task_id=?", (task_id,))

    heartbeat = store.heartbeat_host(
        host_id, active_sessions=1,
        capacity={"runtime_profile": {"components": {
            "agent_host_version": "0.2.27",
        }}},
        principal_id=principal_id, actor=host_id, project=P)
    ok(heartbeat.get("agent_host_version") == "0.2.27",
       "heartbeat runtime-profile version repairs the stale top-level host version")
    ok("terminal_runner_cleanup" not in heartbeat,
       "terminal task does not produce an automatic cleanup directive")
    closed = store.get_work_session(work_session["work_session_id"], project=P)
    ok(closed.get("status") == "active" and not closed.get("completed_at"),
       "terminal task alone does not close its active Work Session")

    second = store.heartbeat_host(
        host_id, active_sessions=1,
        capacity={"runtime_profile": {"components": {
            "agent_host_version": "0.2.27",
        }}},
        principal_id=principal_id, actor=host_id, project=P)
    ok("terminal_runner_cleanup" not in second,
       "a repeated heartbeat still emits no cleanup directive")
    with _conn(P) as c:
        activity_count = c.execute(
            "SELECT COUNT(*) FROM activity WHERE task_id=? "
            "AND kind='runner.terminal_cleanup_requested'", (task_id,),
        ).fetchone()[0]
    ok(activity_count == 0,
       "terminal status creates no automatic-kill audit event")

    calls = []
    original_supervisor, original_try = agent_host.supervisor_action, agent_host._try

    def fake_supervisor(action, selected_runner, options=None):
        calls.append((action, selected_runner, dict(options or {})))
        if action == "health":
            return {"alive": True, "status": "running"}
        return {"alive": False, "status": "killed"}

    def fake_try(method, path, body=None):
        calls.append((method, path, dict(body or {})))
        return {"runner_session_id": (body or {}).get("runner_session_id"),
                "status": (body or {}).get("status")}

    agent_host.supervisor_action = fake_supervisor
    agent_host._try = fake_try
    try:
        outcomes = agent_host.converge_terminal_task_runners(
            {"host_id": host_id}, {"terminal_runner_cleanup": {"sessions": [{
                "runner_session_id": runner_id, "task_id": task_id,
                "task_status": "Done", "reason": "legacy directive",
            }]}})
    finally:
        agent_host.supervisor_action, agent_host._try = original_supervisor, original_try
    terminal_posts = [body for method, path, body in calls
                      if method == "POST" and path == agent_host.P_HEARTBEAT_RUNNER]
    ok(outcomes and not outcomes[0].get("killed")
       and outcomes[0].get("error") == "lease expiry is the only kill authority",
       "the Agent Host refuses terminal-task automatic kill authority")
    ok(not terminal_posts,
       "terminal-task observation does not publish a false terminal runner state")

    store.upsert_runner_session({
        "runner_session_id": runner_id, "host_id": host_id,
        "task_id": task_id, "status": "expired",
        "metadata": {"work_session_id": work_session["work_session_id"],
                     "terminalized_by": "runner_lease_expiry",
                     "failure_reason": "runner heartbeat lease expired"},
    }, principal_id=principal_id, actor=host_id, project=P)
    archived = store.get_work_session(work_session["work_session_id"], project=P)
    ok(archived.get("status") == "archived" and archived.get("completed_at"),
       "lease expiry archives the bound Work Session with terminal evidence")

    store.upsert_runner_session({
        "runner_session_id": runner_id, "host_id": host_id,
        "task_id": task_id, "status": "completed",
        "metadata": {"terminalized_by": "terminal_task"},
    }, principal_id=principal_id, actor=host_id, project=P)
    final_heartbeat = store.heartbeat_host(
        host_id, active_sessions=0, principal_id=principal_id,
        actor=host_id, project=P)
    ok("terminal_runner_cleanup" not in final_heartbeat,
       "completed runner state does not revive the retired directive")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nBUG-111 terminal runner cleanup: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
