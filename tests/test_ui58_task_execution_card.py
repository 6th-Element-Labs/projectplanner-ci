#!/usr/bin/env python3
"""UI-58: server-derived flags the task modal card needs from get_task_execution.

The primary-runner card must show Resume review ONLY when there is a dead review
runner to replace, and Start otherwise — a distinction the browser used to make
by reading /ixp/v1/runner_sessions/watch and inspecting sessions[] itself. To
move the card onto the execution projection (so the browser stops choosing
runner ids), the server must expose that distinction. These tests pin the two
derived booleans to the REAL runner/task state, not to the command succeeding.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="ui58-card-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

from path_setup import ROOT  # noqa: E402,F401

import store  # noqa: E402
from switchboard.application.commands import task_execution  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def new_task(title, status=""):
    task_id = store.create_task({"workstream_id": "UI", "title": title},
                                actor="ui58", project=P)["task_id"]
    if status:
        store.update_task(task_id, {"status": status}, actor="ui58", project=P)
    return task_id


def runner(task_id, runner_id, status):
    return store.upsert_runner_session({
        "runner_session_id": runner_id, "host_id": "host/mac",
        "agent_id": f"codex/{task_id}", "runtime": "codex", "task_id": task_id,
        "claim_id": f"claim-{runner_id}", "status": status,
        "control": {"managed_process": True, "runner_kill": True, "tier": "T3",
                    "runner_open": True, "runner_inject": True},
        "metadata": {"wake_id": f"wake-{runner_id}", "work_session_id": f"ws-{runner_id}"},
        "heartbeat_at": time.time(), "heartbeat_ttl_s": 180,
    }, actor="ui58", project=P)


try:
    store.init_project_registry()
    store.init_db(P)

    # 1) In Review + a dead runner -> resumable_review, not running.
    review_dead = new_task("in review, dead runner", status="In Review")
    runner(review_dead, "run_review_dead", "failed")
    v = task_execution.get_task_execution(review_dead, project=P)
    ok(v["running"] is False, "an In-Review task with a dead runner is not running")
    ok(v.get("resumable_review") is True,
       "In Review + a dead runner is resumable_review=True")
    ok(v.get("has_ended_session") is True,
       "a dead runner counts as an ended session")

    # 2) In Review + NO session -> NOT resumable (Resume review would fail).
    review_fresh = new_task("in review, no session", status="In Review")
    v = task_execution.get_task_execution(review_fresh, project=P)
    ok(v.get("resumable_review") is False,
       "In Review with no session is NOT resumable_review (nothing to replace)")
    ok(v.get("has_ended_session") is False,
       "a task with no session has no ended session")

    # 3) A terminal runner outside review -> ended session, Start (not resumable).
    ended = new_task("runner ended, not in review")
    runner(ended, "run_ended", "completed")
    v = task_execution.get_task_execution(ended, project=P)
    ok(v["running"] is False and v.get("has_ended_session") is True,
       "a completed runner is an ended session")
    ok(v.get("resumable_review") is False,
       "an ended session outside In Review is not resumable_review")

    # 4) A live runner -> running, never resumable_review.
    live = new_task("live runner")
    runner(live, "run_live", "running")
    v = task_execution.get_task_execution(live, project=P)
    ok(v["running"] is True and v["execution_id"] == "run_live",
       "a live runner is running with its execution id")
    ok(v.get("resumable_review") is False and v.get("has_ended_session") is False,
       "a live runner is neither resumable nor an ended session")

    # 5) A fresh task with no runner -> ready: no session, not running.
    fresh = new_task("never started")
    v = task_execution.get_task_execution(fresh, project=P)
    ok(v["running"] is False and v.get("has_ended_session") is False
       and v.get("resumable_review") is False,
       "a never-started task is ready (no session, not running, not resumable)")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nUI-58 task execution card flags: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
