#!/usr/bin/env python3
"""BUG-91 safety case: a late failing attempt must not kill a good one.

Dispatch attempts against a single wake overlap and finish out of order. This is
not hypothetical -- wake-81212df255344dd3 accumulated three runner rows across two
hosts and 31 hours, all carrying the same wake_id, over six dispatch attempts.

So: attempt A establishes a real claim-bound session and starts working. Slower
attempt B then reports `started: false` for the SAME wake. B's receipt must clear
only its own unbound wrapper debris. If it terminalizes A, the cleanup intended to
remove misleading rows instead kills live work -- strictly worse than the bug it
was written to fix.

The first version of terminalize_wake_runners_in DID kill A: `keep` only protects
the runner named in the receipt, and the CO-fleet failure path
(co_fleet.fail_wake) sends no runner_session_id at all.
"""
import os
import shutil
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="bug-91-ooo-")
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


def bound_runner(task_id, runner_id, wake_id, host_id):
    """What a dispatch attempt that actually got somewhere looks like."""
    return store.upsert_runner_session({
        "runner_session_id": runner_id, "host_id": host_id,
        "agent_id": f"codex/{task_id}", "runtime": "codex", "task_id": task_id,
        "claim_id": f"claim-{runner_id}", "status": "running", "cwd": "/work",
        "control": {"managed_process": True, "runner_kill": True, "tier": "T3",
                    "runner_open": True, "runner_inject": True},
        "metadata": {"wake_id": wake_id, "work_session_id": f"ws-{runner_id}",
                     "credential_admission_phase": "claim_bound",
                     "pty": True, "stream_bind": "127.0.0.1", "stream_port": 45123},
        "heartbeat_at": time.time(), "heartbeat_ttl_s": 180,
    }, actor="bug-91-test", project=P)


def wrapper_runner(task_id, runner_id, wake_id, host_id):
    """What a run_agent.py wrapper looks like: no claim, no Work Session."""
    return store.upsert_runner_session({
        "runner_session_id": runner_id, "host_id": host_id,
        "agent_id": f"codex/{task_id}", "runtime": "codex", "task_id": task_id,
        "status": "running", "cwd": "/var/lib/switchboard-co/runtime/bootstrap",
        "control": {"managed_process": True, "tier": "T3"},
        "metadata": {"wake_id": wake_id, "wake_mode": "claim_next"},
        "heartbeat_at": time.time(), "heartbeat_ttl_s": 60,
    }, actor="bug-91-test", project=P)


try:
    store.init_db(P)
    task_id = store.create_task(
        {"workstream_id": "SEG", "title": "out-of-order dispatch attempts"},
        actor="bug-91-test", project=P)["task_id"]
    wake_id = store.request_wake(
        {"runtime": "codex", "lane": "SEG", "task_id": task_id},
        task_id=task_id,
        policy={"mode": "co_fleet", "require_runner_bind": True, "dispatch_attempt": 6},
        reason="BUG-91 out-of-order", actor="bug-91-test", project=P)["wake_id"]

    # Attempt A got a real session going on one host...
    bound_runner(task_id, "run_A_working", wake_id, "host/i-0attemptA")
    # ...while attempt B only ever produced a wrapper on another host.
    wrapper_runner(task_id, "run_B_wrapper", wake_id, "host/i-0attemptB")

    ok(store.get_runner_session("run_A_working", project=P).get("status") == "running",
       "attempt A is a live claim-bound session before B reports anything")

    # B finishes last, and it failed. co_fleet.fail_wake sends no runner_session_id.
    store.complete_wake(
        wake_id,
        result={"schema": "switchboard.co_fleet_receipt.v1", "started": False,
                "failure_class": "capacity_unavailable",
                "reason": "capacity exhausted for co-general: cap=4"},
        actor="bug-91-test", project=P)

    a = store.get_runner_session("run_A_working", project=P)
    b = store.get_runner_session("run_B_wrapper", project=P)

    ok(str(a.get("status") or "").lower() == "running",
       "a later attempt's failure does NOT terminalize the good attempt's live session")
    ok((a.get("metadata") or {}).get("terminalized_by") is None,
       "the good session is not even marked as touched by the failing receipt")
    ok(str(b.get("status") or "").lower() == "failed",
       "the failing attempt still clears its own unbound wrapper debris")

    # And the operator can still watch the work that is genuinely running.
    watch = store.resolve_runner_watch(task_id, include_stale=True, project=P)
    ok(watch.get("watchable") is True
       and watch.get("runner_session_id") == "run_A_working",
       "Watch/Chat still resolves to the live session after the other attempt failed")

    # The reverse order must hold too: a wrapper registered AFTER the good
    # session, then a failure, still leaves the good session alone.
    wrapper_runner(task_id, "run_C_late_wrapper", wake_id, "host/i-0attemptC")
    store.complete_wake(
        wake_id,
        result={"started": False, "reason": "registration timeout for host/i-0attemptC",
                "failure_class": "failed_gate"},
        actor="bug-91-test", project=P)
    ok(str(store.get_runner_session("run_A_working", project=P).get("status")).lower()
       == "running",
       "a second late failure still leaves the live session untouched")
    ok(str(store.get_runner_session("run_C_late_wrapper", project=P).get("status")).lower()
       == "failed",
       "the late wrapper is cleared like any other debris")

    # A claim-bound session is still terminalizable by its OWN receipt, so this
    # guard cannot strand a session forever.
    store.complete_wake(
        wake_id,
        result={"started": False, "reason": "the bound session itself failed"},
        runner_session_id="run_A_working",
        actor="bug-91-test", project=P)
    ok(str(store.get_runner_session("run_A_working", project=P).get("status")).lower()
       == "running",
       "naming a claim-bound runner in a receipt does not terminalize it either — "
       "only its own process lifecycle may, which the host supervisor owns")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nBUG-91 out-of-order attempts: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
