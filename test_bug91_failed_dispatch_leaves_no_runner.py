#!/usr/bin/env python3
"""BUG-91: a dispatch attempt that never started leaves no live task runner.

Reproduces the exact production shape that made browser Watch/Chat refuse with
"Runner session is stale; Watch/Chat refused until a live bind exists":

  wake-81212df255344dd3 (SEG-2, mode co_fleet, require_runner_bind true)
    claimed_at   1784491912.9  by host/i-0c0f00f13dac0714d
    run_59bde1543e5a402f registered 4s later  (a run_agent.py *wrapper*, not Codex)
    completed_at 1784491929.2  result {"started": false,
                                       "failure_class": "capacity_unavailable",
                                       "error": "capacity exhausted for co-general: cap=4"}

Nothing terminalized the wrapper row, so it aged into stale -> expired while
remaining the newest row the browser could find for SEG-2.  One wake accumulated
three such rows across six dispatch attempts; the wake's own runner_session_id
stayed NULL, so no server-side join said which runner was current.
"""
import os
import shutil
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="bug-91-dispatch-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
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


def wrapper_runner(task_id, runner_id, wake_id, host_id):
    """The row a supervised run_agent.py wrapper publishes at launch.

    Deliberately carries no claim_id / work_session_id: the AWS worker runs
    without --auto-work-session, so a code_strict task never reaches a claim.
    """
    return store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": host_id,
        "agent_id": f"codex/{task_id}",
        "runtime": "codex",
        "task_id": task_id,
        "status": "running",
        "cwd": "/var/lib/switchboard-co/runtime/bootstrap",
        "control": {"managed_process": True, "runner_kill": True, "tier": "T3"},
        "metadata": {
            "wake_id": wake_id,
            "wake_mode": "claim_next",
            "command": ["python", "adapters/run_agent.py", "--runtime", "codex",
                        "--lanes", "SEG", "--work-module", "claude_personal_worker:run"],
        },
        "heartbeat_at": time.time(),
        "heartbeat_ttl_s": 60,
    }, actor="bug-91-test", project=P)


try:
    store.init_db(P)
    task = store.create_task(
        {"workstream_id": "SEG", "title": "failed dispatch leaves no runner"},
        actor="bug-91-test", project=P)
    task_id = task["task_id"]

    wake = store.request_wake(
        {"runtime": "codex", "lane": "SEG", "task_id": task_id},
        task_id=task_id,
        policy={"mode": "co_fleet", "require_runner_bind": True},
        reason="BUG-91 regression", actor="bug-91-test", project=P)
    wake_id = wake["wake_id"]
    ok(bool(wake_id), "a co_fleet wake is created for the task")

    registered = wrapper_runner(task_id, "run_wrapper_attempt", wake_id, "host/i-0cap")
    ok(registered.get("runner_session_id") == "run_wrapper_attempt",
       "the supervised wrapper publishes a runner row while the dispatch is still in flight")

    live = store.get_runner_session("run_wrapper_attempt", project=P)
    ok(str(live.get("status") or "") == "running" and live.get("stale") is False,
       "before the dispatch verdict the wrapper row looks like a live task session")

    # ---- the dispatch verdict: capacity exhausted, nothing ever started -----
    store.complete_wake(
        wake_id,
        result={"schema": "switchboard.co_fleet_receipt.v1",
                "started": False,
                "failure_class": "capacity_unavailable",
                "reason": "capacity exhausted for co-general: cap=4",
                "error": "capacity exhausted for co-general: cap=4",
                "escalated": True},
        actor="bug-91-test", project=P)

    closed = store.get_runner_session("run_wrapper_attempt", project=P)
    ok(str(closed.get("status") or "").lower() == "failed",
       "a started:false dispatch immediately terminalizes the wrapper row it raced")
    ok((closed.get("metadata") or {}).get("terminalized_by") == "wake_failure",
       "the terminalized row records why it was closed instead of silently aging out")
    ok("capacity exhausted for co-general: cap=4"
       in str((closed.get("metadata") or {}).get("failure_reason") or ""),
       "the runner row carries the real dispatch failure, not a generic expiry")

    # ---- and it is no longer offered to the browser as a task session ------
    verdict = store.assert_runner_watchable(closed)
    ok(verdict.get("watchable") is not True,
       "the terminalized wrapper row is not watchable")

    watch = store.resolve_runner_watch(task_id, include_stale=True, project=P)
    ok(watch.get("watchable") is not True,
       "Watch/Chat still refuses — there genuinely is no live session for this task")

    # Evidence is superseded, never deleted: the row remains discoverable.
    sessions = store.list_runner_sessions(task_id=task_id, include_stale=True, project=P)
    ok(any(s.get("runner_session_id") == "run_wrapper_attempt" for s in sessions),
       "the failed attempt is retained as auditable history, not deleted")

    # ---- a retry supersedes the prior attempt without touching the winner ---
    wake_two = store.request_wake(
        {"runtime": "codex", "lane": "SEG", "task_id": task_id},
        task_id=task_id,
        policy={"mode": "co_fleet", "require_runner_bind": True},
        reason="BUG-91 retry", actor="bug-91-test", project=P)
    retry_id = wake_two["wake_id"]
    wrapper_runner(task_id, "run_retry_wrapper", retry_id, "host/i-0retry")
    wrapper_runner(task_id, "run_retry_winner", retry_id, "host/i-0retry")

    store.complete_wake(
        retry_id,
        result={"schema": "switchboard.co_fleet_receipt.v1", "started": False,
                "reason": "registration timeout for host/i-0retry",
                "failure_class": "failed_gate"},
        runner_session_id="run_retry_winner",
        actor="bug-91-test", project=P)

    superseded = store.get_runner_session("run_retry_wrapper", project=P)
    kept = store.get_runner_session("run_retry_winner", project=P)
    ok(str(superseded.get("status") or "").lower() == "failed",
       "a retry terminalizes the other in-flight rows from its own attempt")
    ok(str(kept.get("status") or "").lower() != "failed",
       "the runner a dispatch explicitly bound is never terminalized by its own receipt")

    # ---- an unrelated task's runner is never collateral damage -------------
    other = store.create_task(
        {"workstream_id": "SEG", "title": "unrelated live runner"},
        actor="bug-91-test", project=P)
    other_wake = store.request_wake(
        {"runtime": "codex", "lane": "SEG", "task_id": other["task_id"]},
        task_id=other["task_id"],
        policy={"mode": "co_fleet", "require_runner_bind": True},
        reason="BUG-91 bystander", actor="bug-91-test", project=P)
    wrapper_runner(other["task_id"], "run_bystander", other_wake["wake_id"], "host/i-0other")
    store.complete_wake(
        wake_id,
        result={"started": False, "reason": "replay of the same failure"},
        actor="bug-91-test", project=P)
    bystander = store.get_runner_session("run_bystander", project=P)
    ok(str(bystander.get("status") or "").lower() == "running",
       "terminalization is scoped to the failing wake — another wake's runner is untouched")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nBUG-91 failed dispatch leaves no runner: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
