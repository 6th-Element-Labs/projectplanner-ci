#!/usr/bin/env python3
"""BUG-111 / BUG-175 / BUG-258 / BUG-261: terminal leases use the sole stop clock.

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
        "runtime": "codex", "repo_role": "canonical",
        "branch": f"codex/{task_id}-proof", "upstream": "origin/master",
        "base_sha": "a" * 40, "head_sha": "a" * 40,
        "storage_mode": "worktree", "worktree_path": str(TMP),
        "status": "active", "dirty_status": "clean",
        "policy_profile": "code_strict",
        "hygiene": {"repo_preflight": {"ok": True, "verdict": "pass", "findings": []}},
    }, actor="bug111-test", project=P)["work_session"]
    claim = store.claim_task(
        task_id, f"codex/{task_id}", principal_id=principal_id,
        actor="bug111-test", project=P,
        work_session_id=work_session["work_session_id"],
        session_policy_profile="code_strict", require_work_session=True,
    )
    ok(claim.get("claimed") is True,
       "test runner has an exact active claim and Work Session binding")
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
        "task_id": task_id, "claim_id": claim.get("claim_id"),
        "status": "running", "pid": 111,
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
    ok(closed.get("status") == "active" and not closed.get("completed_at"),
       "terminal task leaves its Work Session active until host stop receipt")

    second = store.heartbeat_host(
        host_id, active_sessions=1,
        capacity={"runtime_profile": {"components": {
            "agent_host_version": "0.2.27",
        }}},
        principal_id=principal_id, actor=host_id, project=P)
    ok((second.get("terminal_runner_cleanup") or {}).get(
        "closed_work_session_count") == 0,
       "a repeated heartbeat still does not complete the Work Session")
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
    ok(expired and expired[0].get("terminalized") is True
       and expired[0].get("reason") == "terminal_lease_surrendered"
       and any(c[:2] == ("kill", runner_id) for c in expire_calls),
       "terminal surrender kills the runner even when heartbeat is still fresh")
    receipt = next(call[2] for call in renew_calls
                   if len(call) == 3 and call[0] == "POST"
                   and call[1] == agent_host.P_HEARTBEAT_RUNNER
                   and call[2].get("status") == "stopped")
    ok(receipt.get("status") == "stopped"
       and (receipt.get("metadata") or {}).get("terminalized_by")
       == "terminal_lease_surrendered"
       and not (receipt.get("metadata") or {}).get("failure_reason"),
       "terminal-task stop receipt is not classified as a heartbeat failure")

    # BUG-261: yield_mission uses completion_owner rather than terminal_task.
    # It is still an explicit lease surrender, even though make_runner_lease_due
    # backdates the heartbeat and the host therefore observes stale=True.
    yielded_metadata = dict(receipt.get("metadata") or {})
    yielded_metadata["lease_surrender"] = {
        "authority": "completion_owner", "reason": "mission yielded: waiting",
    }

    def fake_drain_yielded(_host_id, recover_stale_local=True):
        return [{
            "runner_session_id": runner_id, "host_id": host_id,
            "task_id": task_id, "agent_id": f"codex/{task_id}",
            "alive": True, "stale": True, "status": "running",
            "pid": 111, "runtime": "codex", "metadata": yielded_metadata,
        }]

    yielded_calls = []
    agent_host._drain_runners = fake_drain_yielded
    agent_host.supervisor_action = fake_supervisor
    agent_host._drop_host_bridge = lambda rid: None
    agent_host._try = lambda method, path, body=None: (
        yielded_calls.append((method, path, dict(body or {})))
        or {"runner_session_id": (body or {}).get("runner_session_id"),
            "status": (body or {}).get("status") or "ok"})
    try:
        yielded = agent_host.expire_runner_leases({"host_id": host_id}, now=10_001)
    finally:
        agent_host._drain_runners = original_drain
        agent_host.supervisor_action = original_supervisor
        agent_host._drop_host_bridge = original_drop
        agent_host._try = original_try
    yielded_receipt = next(call[2] for call in yielded_calls
                            if call[0] == "POST"
                            and call[1] == agent_host.P_HEARTBEAT_RUNNER)
    ok(yielded[0].get("reason") == "terminal_lease_surrendered"
       and yielded_receipt.get("status") == "stopped"
       and yielded_receipt.get("metadata", {}).get("terminalized_by")
       == "terminal_lease_surrendered"
       and not yielded_receipt.get("metadata", {}).get("failure_reason"),
       "yielded review stop is an explicit surrender, not heartbeat expiry")

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

    store.upsert_runner_session(yielded_receipt,
                                principal_id=principal_id, actor=host_id, project=P)
    store.upsert_runner_session(yielded_receipt,
                                principal_id=principal_id, actor=host_id, project=P)
    closed_after_receipt = store.get_work_session(work_session["work_session_id"], project=P)
    with _conn(P) as c:
        claim_status = c.execute("SELECT status FROM task_claims WHERE id=?", (
            claim["claim_id"],)).fetchone()[0]
        receipt_events = c.execute(
            "SELECT COUNT(*) FROM activity WHERE task_id=? "
            "AND kind='task.claim.completed_by_terminal_task_receipt'", (task_id,)
        ).fetchone()[0]
        recovery_events = c.execute(
            "SELECT COUNT(*) FROM activity WHERE task_id=? "
            "AND payload LIKE '%orphan_claim_after_runner_lease_expiry%'", (task_id,)
        ).fetchone()[0]
    ok(claim_status == "completed" and closed_after_receipt.get("status") == "completed",
       "host stop receipt finalizes the active claim and Work Session")
    ok(recovery_events == 0,
       "terminal-task receipt creates no false orphan-recovery finding")
    ok(receipt_events == 1,
       "replayed terminal-task receipt is an idempotent cleanup")
    final_heartbeat = store.heartbeat_host(
        host_id, active_sessions=0, principal_id=principal_id,
        actor=host_id, project=P)
    ok((final_heartbeat.get("terminal_runner_cleanup") or {}).get("session_count") == 0,
       "terminalized runners disappear from the lease-due directive")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nBUG-111 terminal runner cleanup: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
