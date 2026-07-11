#!/usr/bin/env python3
"""BUG-44: an idle narrator cannot continuously starve the live web box."""
from pathlib import Path
import sys

import jobs
import narrate

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


originals = {
    "project_ids": jobs.store.project_ids,
    "init_db": jobs.store.init_db,
    "pending": jobs.store.list_pending_narrations,
    "run_pending": narrate.run_pending,
    "run_deliverables": narrate.run_deliverables,
}

try:
    projects = ["idle", "busy", "race"]
    pending = {"idle": [], "busy": [{"task_id": "BUG-44"}], "race": []}
    task_calls = []
    deliverable_calls = []
    jobs.store.project_ids = lambda: projects
    jobs.store.init_db = lambda project: None
    jobs.store.list_pending_narrations = lambda project: pending[project]
    narrate.run_pending = lambda project: task_calls.append(project) or (
        [{"task_id": "BUG-44"}] if project in ("busy", "race") else [])
    narrate.run_deliverables = lambda project: deliverable_calls.append(project) or []

    jobs.narrate_pending()
    ok(task_calls == projects, "task narration queue still drains for every project")
    ok(deliverable_calls == ["busy", "race"],
       "idle project skips scans while a just-enqueued race still refreshes deliverables")

    timer = Path("deploy/projectplanner-narrate.timer").read_text(encoding="utf-8")
    active_lines = [line.strip() for line in timer.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
    ok("OnUnitInactiveSec=45s" in active_lines,
       "narrator cadence starts after the previous drain completes")
    ok(not any(line.startswith("OnUnitActiveSec=") for line in active_lines),
       "timer cannot become immediately due while an overlong drain is active")

    service = Path("deploy/projectplanner-narrate.service").read_text(encoding="utf-8")
    ok("Slice=projectplanner-batch.slice" in service and "IOSchedulingClass=idle" in service,
       "background narrator is resource-bounded below interactive traffic")
    ok("TimeoutStartSec=5min" in service,
       "runaway narration drains have a finite service timeout")
finally:
    jobs.store.project_ids = originals["project_ids"]
    jobs.store.init_db = originals["init_db"]
    jobs.store.list_pending_narrations = originals["pending"]
    narrate.run_pending = originals["run_pending"]
    narrate.run_deliverables = originals["run_deliverables"]

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
