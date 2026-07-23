#!/usr/bin/env python3
"""Starting one task must carry it to Done, with or without a deliverable.

ADR-0008 W1/W2. Start is the arming surface: it must grant coordination
authority as well as capacity. Before this, the web Start button requested
capacity and armed nothing, so a task got a runner, reached In Review, and
stalled -- nothing was authorised to drive its review/remediation/merge rounds.

A task scope also had to name a deliverable, so a task linked to none could
never be driven at all. Deliverables group outcomes; requiring one to finish a
single task forces bookkeeping onto every ad-hoc start.
"""
from __future__ import annotations

import os
import sys

from path_setup import ROOT  # noqa: F401

from switchboard.storage.repositories import autopilot_scopes as scopes_repo  # noqa: E402
import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


print("task scope end to end")

P = "switchboard"
store.init_db(P)
task = store.create_task(
    {"workstream_id": "SIMPLIFY", "title": "standalone task scope"},
    actor="test", project=P)
tid = task["task_id"]

# --- a task scope may stand alone -------------------------------------------
started = scopes_repo.start_autopilot_scope(
    project=P, scope_type="task", task_project=P, task_id=tid,
    runtime="codex", actor="operator/test")
ok(not started.get("error"),
   f"a task scope starts with no deliverable (got {started.get('error') or 'ok'})")
ok(started.get("scope_type") == "task" and str(started.get("task_id") or "").upper() == tid,
   "the scope targets exactly that task")
scope_id = started.get("scope_id")

# --- arming is idempotent ---------------------------------------------------
again = scopes_repo.start_autopilot_scope(
    project=P, scope_type="task", task_project=P, task_id=tid,
    runtime="codex", actor="operator/test")
ok(again.get("scope_id") == scope_id,
   "starting the same task again reuses its scope rather than forking a second")

# --- an unknown task is refused, so a typo cannot arm a phantom scope -------
bogus = scopes_repo.start_autopilot_scope(
    project=P, scope_type="task", task_project=P, task_id="NOPE-9999",
    runtime="codex", actor="operator/test")
ok(bool(bogus.get("error")),
   "a task scope for an unknown task is refused")

# --- a deliverable scope still requires its deliverable ---------------------
missing = scopes_repo.start_autopilot_scope(
    project=P, scope_type="deliverable", deliverable_id="",
    runtime="codex", actor="operator/test")
ok(bool(missing.get("error")),
   "a deliverable scope still requires a deliverable")

# --- the scope is discoverable so the coordinator can pick it up ------------
live = scopes_repo.list_autopilot_scopes(project=P, status="active,paused", limit=500)
mine = [r for r in live
        if str(r.get("task_id") or "").upper() == tid
        and str(r.get("scope_type") or "") == "task"]
ok(len(mine) == 1, f"exactly one live scope drives this task (found {len(mine)})")
ok(not str(mine[0].get("deliverable_id") or ""),
   "that scope carries no deliverable anchor")

# --- the coordinator resolves a standalone task scope without a mission ----
import scoped_completion_coordinator as scc  # noqa: E402

ok(hasattr(scc.ScopedCompletionCoordinator, "_run_standalone_task_scope"),
   "the coordinator has a standalone task-scope drive path")
src = (ROOT / "scoped_completion_coordinator.py").read_text(encoding="utf-8")
ok("_run_standalone_task_scope(project, scope, authority)" in src
   and src.index("_run_standalone_task_scope(project, scope, authority)")
   < src.index("get_mission_status"),
   "it branches before get_mission_status, so no deliverable is required")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
