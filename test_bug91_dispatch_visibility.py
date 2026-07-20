#!/usr/bin/env python3
"""BUG-91: when no runner exists, say why the dispatch failed.

"Runner session is stale; Watch/Chat refused until a live bind exists" was true
and useless: it described the debris a failed dispatch left behind rather than
the failure. The dispatcher already knew the answer -- "capacity exhausted for
co-general: cap=4" -- and recorded it on the wake.

Also covers the stale-pending policy. One SEG-2 wake sat pending for 30.7 hours
across six dispatch attempts while silently accumulating runner rows; a queued
wake that old must read as needs_attention, not as a task with no runner.
"""
import os
import shutil
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="bug-91-visibility-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402
from switchboard.storage.repositories import runner as runner_repo  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def new_task(title):
    return store.create_task({"workstream_id": "SEG", "title": title},
                             actor="bug-91-test", project=P)["task_id"]


def new_wake(task_id, attempt=0, require_bind=True):
    return store.request_wake(
        {"runtime": "codex", "lane": "SEG", "task_id": task_id},
        task_id=task_id,
        policy={"mode": "co_fleet", "require_runner_bind": require_bind,
                "dispatch_attempt": attempt},
        reason="BUG-91 visibility", actor="bug-91-test", project=P)["wake_id"]


try:
    store.init_db(P)

    # ---- a capacity failure names itself, with no runner row at all --------
    capped = new_task("capacity failure is visible")
    wake_id = new_wake(capped, attempt=6)
    store.complete_wake(
        wake_id,
        result={"schema": "switchboard.co_fleet_receipt.v1", "started": False,
                "failure_class": "capacity_unavailable",
                "reason": "capacity exhausted for co-general: cap=4",
                "escalated": True},
        actor="bug-91-test", project=P)

    watch = store.resolve_runner_watch(capped, include_stale=True, project=P)
    ok(watch.get("watchable") is not True, "Watch still refuses when nothing started")
    ok("capacity exhausted for co-general: cap=4" in str(watch.get("message") or ""),
       "the refusal names the real dispatch failure instead of 'No runner sessions are registered'")
    dispatch = watch.get("dispatch") or {}
    ok(dispatch.get("failure_class") == "capacity_unavailable",
       "the typed failure class travels to the browser")
    ok(dispatch.get("dispatch_attempt") == 6,
       "the operator can see this was the sixth attempt, not a one-off")
    ok(dispatch.get("wake_id") == wake_id,
       "the refusal points at the exact wake that failed")

    # ---- a registration timeout says so too --------------------------------
    timed_out = new_task("registration timeout is visible")
    store.complete_wake(
        new_wake(timed_out),
        result={"started": False, "failure_class": "failed_gate",
                "reason": "registration timeout for host/i-065275fba65d3ff0b"},
        actor="bug-91-test", project=P)
    timeout_watch = store.resolve_runner_watch(timed_out, include_stale=True, project=P)
    ok("registration timeout for host/i-065275fba65d3ff0b"
       in str(timeout_watch.get("message") or ""),
       "a registration timeout is reported verbatim, not as a stale runner")

    # ---- a wake queued too long reads as needs_attention -------------------
    stuck = new_task("queued far too long")
    new_wake(stuck, attempt=6)
    fresh = runner_repo.latest_dispatch_outcome(stuck, project=P)
    ok(fresh.get("state") == "queued",
       "a wake queued a moment ago is simply 'queued', not an alarm")

    later = time.time() + 30.7 * 3600  # the exact SEG-2 pending interval
    aged = runner_repo.latest_dispatch_outcome(stuck, project=P, now=later)
    ok(aged.get("state") == "needs_attention",
       "a wake queued 30.7h reads as needs_attention rather than silently pending")
    ok(aged.get("waiting_seconds", 0) >= 30 * 3600,
       "the age of the queued wake is reported so the delay is visible")
    ok("30.7h" in str(aged.get("message") or ""),
       "the message states how long the task has been waiting")
    ok("6 dispatch attempts" in str(aged.get("message") or ""),
       "the message states how many attempts have already been burned")

    # ---- a task that genuinely never ran keeps the honest empty answer -----
    untouched = new_task("never dispatched at all")
    quiet = store.resolve_runner_watch(untouched, include_stale=True, project=P)
    ok((quiet.get("dispatch") or {}) == {}
       and "No runner sessions are registered" in str(quiet.get("message") or ""),
       "a task nobody ever dispatched still gets the plain no-runner answer")

    # ---- a successful dispatch reports no failure -------------------------
    # A require_runner_bind wake cannot be completed without a real bind tuple
    # (the system correctly refuses), so prove the success path on a plain wake.
    good = new_task("dispatch succeeded")
    completed = store.complete_wake(
        new_wake(good, require_bind=False),
        result={"started": True}, runner_session_id="run_good",
        agent_id="codex/good", actor="bug-91-test", project=P)
    ok(str(completed.get("status") or "") == "completed",
       "a plain successful dispatch completes its wake")
    ok(runner_repo.latest_dispatch_outcome(good, project=P) == {},
       "a completed dispatch reports no failure at all")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nBUG-91 dispatch visibility: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
