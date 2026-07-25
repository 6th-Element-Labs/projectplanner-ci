#!/usr/bin/env python3
"""CO-25: start_task refuses when dependencies are unsatisfied.

Regression for BREAKDOWN 3 (DOGFOOD-19 / CO-21): dispatch must not spawn a
wake/runner/Work Session for work that claim_task would refuse.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="co25-start-deps-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from switchboard.application.commands import task_execution  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def make_task(title, *, status="Not Started", depends_on=None):
    return store.create_task(
        {
            "workstream_id": "CO",
            "title": title,
            "status": status,
            "depends_on": depends_on or [],
        },
        actor="co25-test",
        project=P,
    )


def mark_done(task_id, number):
    store.mark_task_pr_opened(
        task_id,
        pr_number=number,
        pr_url=f"https://github.com/example/projectplanner/pull/{number}",
        branch=f"codex/{task_id.lower()}",
        head_sha=f"{number:040d}"[-40:],
        actor="co25-test",
        project=P,
    )
    return store.mark_task_merged(
        task_id,
        merged_sha=f"{number + 1:040d}"[-40:],
        pr_number=number,
        pr_url=f"https://github.com/example/projectplanner/pull/{number}",
        actor="co25-test",
        project=P,
    )


store.init_project_registry()
store.init_db(P)

blocker_a = make_task("CO-25 blocker A", status="In Progress")
blocker_b = make_task("CO-25 blocker B", status="In Review")
blocked = make_task(
    "CO-25 blocked successor",
    depends_on=[blocker_a["task_id"], blocker_b["task_id"]],
)
detail = store.get_task(blocked["task_id"], project=P)
dep_state = detail.get("dependency_state") or {}
ok(dep_state.get("satisfied") is False
   and blocker_a["task_id"] in {row["task_id"] for row in dep_state.get("blocking") or []},
   "fixture has unsatisfied dependencies with named blockers")

launches = []
wakes_before = store.list_wake_intents(task_id=blocked["task_id"], project=P)
runners_before = store.list_runner_sessions(task_id=blocked["task_id"], project=P)
sessions_before = store.list_work_sessions(task_id=blocked["task_id"], project=P)

refusal = {}
try:
    task_execution.start_task(
        blocked["task_id"],
        project=P,
        actor="co25-test",
        launcher=lambda *_a, **_kw: launches.append("launched") or {
            "dispatched": True, "action": "started",
            "wake_id": "wake-should-not-exist",
            "runner_session_id": "run-should-not-exist",
        },
    )
except task_execution.TaskExecutionError as exc:
    refusal = exc.as_dict()

blocking_ids = list(refusal.get("blocking") or refusal.get("blocking_task_ids") or [])
ok(refusal.get("error_code") == "start_refused"
   and refusal.get("start_error") == "dependencies_unsatisfied",
   "unsatisfied deps refuse with typed dependencies_unsatisfied")
ok(blocker_a["task_id"] in blocking_ids and blocker_b["task_id"] in blocking_ids,
   "refusal names the blocking task ids")
ok(launches == [], "launcher is not invoked when dependencies are unsatisfied")
ok(store.list_wake_intents(task_id=blocked["task_id"], project=P) == wakes_before,
   "no wake intent is created for unsatisfied dependencies")
ok(store.list_runner_sessions(task_id=blocked["task_id"], project=P) == runners_before,
   "no runner session is created for unsatisfied dependencies")
ok(store.list_work_sessions(task_id=blocked["task_id"], project=P) == sessions_before,
   "no Work Session is created for unsatisfied dependencies")

ready_dep = make_task("CO-25 satisfied dependency")
mark_done(ready_dep["task_id"], 825)
ready = make_task("CO-25 ready successor", depends_on=[ready_dep["task_id"]])
ready_detail = store.get_task(ready["task_id"], project=P)
ok((ready_detail.get("dependency_state") or {}).get("satisfied") is True,
   "ready fixture has satisfied dependencies")

ready_launches = []
started = task_execution.start_task(
    ready["task_id"],
    project=P,
    actor="co25-test",
    launcher=lambda *_a, **_kw: ready_launches.append("launched") or {
        "dispatched": True, "action": "started",
        "wake_id": "wake-ready", "runner_session_id": "run-ready",
    },
)
ok(ready_launches == ["launched"] and started.get("action") == "started",
   "start_task on a ready task is unchanged")

print(f"\nCO-25 start_task dependency gate: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
