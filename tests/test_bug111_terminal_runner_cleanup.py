#!/usr/bin/env python3
"""BUG-111 / BUG-175: terminal tasks make runner leases due for the sole stop clock.

SIMPLIFY-17 retired process-kill-from-task-status. Terminal tasks must still
reclaim capacity by making the renewable runner lease due (force-stale + fence)
so expire_runner_leases remains the only automatic kill authority. Renewals
must not resurrect a due lease.
"""
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
os.environ["PM_RUNNER_DIR"] = str(TMP / "runner-state")

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
        "heartbeat_ttl_s": 180,
        "metadata": {
            "work_session_id": work_session["work_session_id"],
            "wake_id": f"wake-{runner_id}",
            "native_host_execution": True,
        },
    }, principal_id=principal_id, actor=host_id, project=P)
    with _conn(P) as c:
        c.execute("UPDATE tasks SET status='Done' WHERE task_id=?", (task_id,))

    heartbeat = store.heartbeat_host(
        host_id, active_sessions=1,
        capacity={"runtime_profile": {"components": {
            "agent_host_version": "0.2.27",
        }}},
        principal_id=principal_id, actor=host_id, project=P)
    cleanup = heartbeat.get("terminal_runner_cleanup") or {}
    ok(heartbeat.get("agent_host_version") == "0.2.27",
       "heartbeat runtime-profile version repairs the stale top-level host version")
    ok(cleanup.get("session_count") == 1
       and cleanup.get("sessions", [{}])[0].get("runner_session_id") == runner_id
       and cleanup.get("sessions", [{}])[0].get("action") == "make_lease_due",
       "terminal task produces a lease-due cleanup directive")
    due = store.get_runner_session(runner_id, project=P)
    ok(due.get("stale") is True
       and (due.get("metadata") or {}).get("lease_surrender", {}).get("authority")
       == "terminal_task",
       "terminal task makes the exact runner lease due without process kill")
    closed = store.get_work_session(work_session["work_session_id"], project=P)
    ok(closed.get("status") == "completed" and closed.get("completed_at"),
       "terminal task completes its active Work Session")

    second = store.heartbeat_host(
        host_id, active_sessions=1,
        capacity={"runtime_profile": {"components": {
            "agent_host_version": "0.2.27",
        }}},
        principal_id=principal_id, actor=host_id, project=P)
    ok((second.get("terminal_runner_cleanup") or {}).get(
        "closed_work_session_count") == 0,
       "a repeated heartbeat does not complete the Work Session twice")
    with _conn(P) as c:
        activity_count = c.execute(
            "SELECT COUNT(*) FROM activity WHERE task_id=? "
            "AND kind='runner.terminal_cleanup_requested'", (task_id,),
        ).fetchone()[0]
    ok(activity_count == 1,
       "repeated cleanup delivery records only one request audit event")

    # Host must not renew a due lease — that was the zombie amplifier.
    renew_calls = []
    original_drain, original_try = agent_host._drain_runners, agent_host._try

    def fake_drain(_host_id, recover_stale_local=True):
        return [{
            "runner_session_id": runner_id, "host_id": host_id,
            "task_id": task_id, "agent_id": f"codex/{task_id}",
            "alive": True, "stale": True, "status": "running",
            "pid": 111, "runtime": "codex",
            "metadata": {
                "work_session_id": work_session["work_session_id"],
                "wake_id": f"wake-{runner_id}",
                "native_host_execution": True,
                "lease_surrender": {"authority": "terminal_task"},
            },
        }]

    def fake_try(method, path, body=None):
        renew_calls.append((method, path, dict(body or {})))
        return {"runner_session_id": (body or {}).get("runner_session_id"),
                "status": (body or {}).get("status") or "ok"}

    agent_host._drain_runners = fake_drain
    agent_host._try = fake_try
    try:
        renewed = agent_host.renew_live_direct_runners({"host_id": host_id})
    finally:
        agent_host._drain_runners, agent_host._try = original_drain, original_try
    renew_posts = [body for method, path, body in renew_calls
                   if method == "POST" and path == agent_host.P_HEARTBEAT_RUNNER
                   and (body or {}).get("status") == "running"]
    ok(not renew_posts and renewed == [],
       "Agent Host refuses to renew a terminal-task due runner lease")

    # Lease expiry remains the only kill authority. A concurrent renew can
    # refresh heartbeat_at after surrender is stamped, so the reaper must kill
    # on lease_surrender even when the row is not yet stale.
    expire_calls = []
    original_supervisor = agent_host.supervisor_action
    original_drop = agent_host._drop_host_bridge

    def fake_supervisor(action, selected_runner, options=None):
        expire_calls.append((action, selected_runner, dict(options or {})))
        return {"alive": False, "status": "killed"}

    def fake_drain_surrendered_fresh(_host_id, recover_stale_local=True):
        return [{
            "runner_session_id": runner_id, "host_id": host_id,
            "task_id": task_id, "agent_id": f"codex/{task_id}",
            "alive": True, "stale": False, "status": "running",
            "pid": 111, "runtime": "codex",
            "metadata": {
                "work_session_id": work_session["work_session_id"],
                "wake_id": f"wake-{runner_id}",
                "native_host_execution": True,
                "lease_surrender": {"authority": "terminal_task"},
            },
        }]

    agent_host._drain_runners = fake_drain_surrendered_fresh
    agent_host.supervisor_action = fake_supervisor
    agent_host._drop_host_bridge = lambda rid: expire_calls.append(("drop", rid))
    agent_host._try = fake_try
    try:
        expired = agent_host.expire_runner_leases({"host_id": host_id}, now=10_000)
    finally:
        agent_host._drain_runners = original_drain
        agent_host.supervisor_action = original_supervisor
        agent_host._drop_host_bridge = original_drop
        agent_host._try = original_try
    ok(expired and expired[0].get("expired") is True
       and any(c[:2] == ("kill", runner_id) for c in expire_calls),
       "lease expiry kills a surrendered runner even when heartbeat is still fresh")

    # Compatibility: host still refuses legacy kill directives.
    refuse_calls = []
    agent_host.supervisor_action = lambda action, selected_runner, options=None: (
        refuse_calls.append((action, selected_runner, dict(options or {}))) or
        ({"alive": True, "status": "running"} if action == "health"
         else {"alive": False, "status": "killed"}))
    agent_host._try = fake_try
    try:
        outcomes = agent_host.converge_terminal_task_runners(
            {"host_id": host_id}, {"terminal_runner_cleanup": {"sessions": [{
                "runner_session_id": runner_id, "task_id": task_id,
                "task_status": "Done", "reason": "legacy directive",
            }]}})
    finally:
        agent_host.supervisor_action = original_supervisor
        agent_host._try = original_try
    ok(outcomes and not outcomes[0].get("killed")
       and outcomes[0].get("error") == "lease expiry is the only kill authority",
       "legacy terminal-task kill directives stay refused")

    store.upsert_runner_session({
        "runner_session_id": runner_id, "host_id": host_id,
        "task_id": task_id, "status": "expired",
        "metadata": {"work_session_id": work_session["work_session_id"],
                     "terminalized_by": "runner_lease_expiry",
                     "failure_reason": "runner heartbeat lease expired"},
    }, principal_id=principal_id, actor=host_id, project=P)
    final_heartbeat = store.heartbeat_host(
        host_id, active_sessions=0, principal_id=principal_id,
        actor=host_id, project=P)
    ok((final_heartbeat.get("terminal_runner_cleanup") or {}).get("session_count") == 0,
       "terminalized runners disappear from the lease-due directive")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nBUG-111 terminal runner cleanup: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
