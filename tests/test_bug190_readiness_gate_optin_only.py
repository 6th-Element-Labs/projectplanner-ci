#!/usr/bin/env python3
"""BUG-190: UI-63's readiness refusal fires only for projects that opted in.

Regression for the 2026-07-25 autopilot outage. UI-63 (#864) added a hard
refusal to start_task for any project whose execution readiness was not green.
connect_dispatch, two files over, still supports the unconfigured project on
COORD-47's legacy path ("Once a project opts into execution policy, however,
every field is mandatory and resolution fails closed"). So an unconfigured
project was refused in one file and supported in the next.

The switchboard board itself has never had an execution policy. The refusal was
latent for two hours because BUG-184 was throwing earlier in the pipeline; when
that fix landed, every autopilot dispatch reached the new gate and failed with
"Project execution readiness is blocked." for the rest of the day.

This pins the seam in both directions: an unconfigured project keeps the legacy
path, and an opted-in project still fails closed.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="bug190-readiness-gate-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from switchboard.application.commands import connect_dispatch  # noqa: E402
from switchboard.application.commands import task_execution  # noqa: E402
from switchboard.storage.repositories import (  # noqa: E402
    project_execution_policy,
    project_execution_readiness,
)

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# The exact readiness payload the live switchboard board returns today.
BLOCKED = {
    "schema": "switchboard.project_execution_policy.v1",
    "passed": False,
    "status": "blocked",
    "reason_code": "project_execution_policy_missing",
    "message": ("no project execution policy is configured; runners cannot place, "
                "materialize, or authorize work for this project"),
    "blockers": [{
        "code": "project_execution_policy_missing",
        "category": "configuration",
        "message": "no project execution policy is configured",
        "repair": "Set a project execution policy.",
        "blocking": True,
    }],
}
READY = {"schema": BLOCKED["schema"], "passed": True, "status": "ready",
         "reason_code": "", "message": "", "blockers": []}

store.init_project_registry()
store.init_db(P)
task = store.create_task(
    {"workstream_id": "CO", "title": "BUG-190 dispatch subject", "status": "Not Started"},
    actor="bug190-test", project=P)
TASK_ID = task["task_id"]

saved_policy = project_execution_policy.get_project_execution_policy
saved_readiness = project_execution_readiness.get_project_execution_readiness
saved_enqueue = connect_dispatch.enqueue_task


def run(*, configured, readiness):
    """start_task with launcher=None so the real gate + dispatch seam are exercised."""
    dispatched = []
    project_execution_policy.get_project_execution_policy = (
        lambda _project: {"configured": configured, "activated": configured})
    project_execution_readiness.get_project_execution_readiness = (
        lambda _project: dict(readiness))
    connect_dispatch.enqueue_task = lambda *_a, **_kw: dispatched.append("enqueued") or {
        "dispatched": True, "action": "started",
        "wake_id": "wake-bug190", "runner_session_id": "run-bug190"}
    try:
        result = task_execution.start_task(
            TASK_ID, project=P, actor="bug190-test")
        return {"refusal": {}, "dispatched": dispatched, "result": result}
    except task_execution.TaskExecutionError as exc:
        return {"refusal": exc.as_dict(), "dispatched": dispatched, "result": {}}


try:
    # 1. The outage case. No policy, readiness red -> legacy path, NOT a refusal.
    legacy = run(configured=False, readiness=BLOCKED)
    ok(legacy["refusal"] == {} and legacy["dispatched"] == ["enqueued"],
       "unconfigured project with red readiness still reaches Connect dispatch")
    ok(legacy["result"].get("action") == "started",
       "unconfigured project reports a started dispatch rather than start_refused")

    # 2. The opt-in case. UI-63's fail-closed behaviour is preserved intact.
    strict = run(configured=True, readiness=BLOCKED)
    ok(strict["refusal"].get("error_code") == "start_refused"
       and strict["refusal"].get("start_error") == "project_execution_policy_missing",
       "opted-in project with red readiness still refuses with the typed reason code")
    ok(strict["dispatched"] == [],
       "opted-in refusal happens before any wake is requested")
    ok(bool(strict["refusal"].get("execution_readiness"))
       and bool(strict["refusal"].get("blockers")),
       "refusal carries the authoritative readiness payload and its blockers")

    # 3. An opted-in project that is genuinely ready dispatches normally.
    green = run(configured=True, readiness=READY)
    ok(green["refusal"] == {} and green["dispatched"] == ["enqueued"],
       "opted-in project with green readiness dispatches")
finally:
    project_execution_policy.get_project_execution_policy = saved_policy
    project_execution_readiness.get_project_execution_readiness = saved_readiness
    connect_dispatch.enqueue_task = saved_enqueue

# Task Execution and Connect share the same opt-in boundary. Readiness may
# explain an unconfigured project, but it cannot block the compatibility wake.
dispatch_src = (ROOT / "src/switchboard/application/commands/connect_dispatch.py").read_text()
ok('execution_context.resolve(' in dispatch_src
   and 'if get_project_execution_policy(project).get("activated"):' in dispatch_src,
   "connect_dispatch resolves exact authority only after project opt-in")

print(f"\nBUG-190 readiness gate opt-in only: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
