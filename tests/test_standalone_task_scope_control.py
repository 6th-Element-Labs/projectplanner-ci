#!/usr/bin/env python3
"""A task started on its own must also be stoppable.

#803 let a task scope exist without a deliverable, so operator Start could arm
one. But control_autopilot still demanded a deliverable_id, and the only REST
route for a task scope was nested under a deliverable. The result was a scope
that could be armed and driven but never paused, resumed or stopped through any
supported path -- with ACT=1 it would keep re-driving a task the operator
believed they had stopped.

A deliverable scope is named by its deliverable; a task scope by its task.
"""
from __future__ import annotations

import os
import sys
import tempfile

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="scopestop-")
os.environ["PM_DB_PATH"] = os.path.join(TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP

from switchboard.application.commands import autopilot as autopilot_command  # noqa: E402
from switchboard.storage.repositories import autopilot_scopes as scopes_repo  # noqa: E402
import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


print("standalone task scope control")

P = "switchboard"
store.init_db(P)
task = store.create_task({"workstream_id": "SIMPLIFY", "title": "stoppable scope"},
                         actor="test", project=P)
tid = task["task_id"]

# Arm a standalone task scope directly (operator Start does this via start_task).
armed = scopes_repo.start_autopilot_scope(
    project=P, scope_type="task", task_project=P, task_id=tid,
    runtime="codex", actor="operator/test")
ok(not armed.get("error") and armed.get("scope_id"),
   "a standalone task scope arms with no deliverable")


def _live():
    return [r for r in scopes_repo.list_autopilot_scopes(
        project=P, status="active,paused", limit=100)
        if str(r.get("task_id") or "").upper() == tid]


ok(len(_live()) == 1, "it is live before we try to stop it")

# The regression: pause/resume/stop by task id alone, no deliverable_id.
paused = autopilot_command.control_autopilot(
    "", project=P, action="pause", scope_type="task",
    task_project=P, task_id=tid, actor="operator/test")
ok(not paused.get("error"),
   f"pause needs only a task id (got {paused.get('error') or 'ok'})")

resumed = autopilot_command.control_autopilot(
    "", project=P, action="resume", scope_type="task",
    task_project=P, task_id=tid, actor="operator/test")
ok(not resumed.get("error"),
   f"resume needs only a task id (got {resumed.get('error') or 'ok'})")

stopped = autopilot_command.control_autopilot(
    "", project=P, action="stop", scope_type="task",
    task_project=P, task_id=tid, actor="operator/test")
ok(not stopped.get("error"),
   f"stop needs only a task id (got {stopped.get('error') or 'ok'})")
ok(_live() == [],
   "the scope is genuinely no longer live -- it cannot re-drive the task")

# A task scope with no task id is still refused: we relaxed the deliverable
# requirement, not identity itself.
try:
    autopilot_command.control_autopilot(
        "", project=P, action="stop", scope_type="task",
        task_project=P, task_id="", actor="operator/test")
    refused_missing_task = False
except autopilot_command.AutopilotError as exc:
    refused_missing_task = "task_id" in str(exc)
ok(refused_missing_task, "a task scope with no task_id is still refused")

# A deliverable scope still requires its deliverable -- unchanged.
try:
    autopilot_command.control_autopilot(
        "", project=P, action="stop", scope_type="deliverable",
        actor="operator/test")
    refused_missing_deliverable = False
except autopilot_command.AutopilotError as exc:
    refused_missing_deliverable = "deliverable_id" in str(exc)
ok(refused_missing_deliverable,
   "a deliverable scope still requires a deliverable_id")

# The UI needs a route that is not nested under a deliverable.
router_src = (ROOT / "src/switchboard/api/routers/deliverables.py").read_text(
    encoding="utf-8")
ok('"/api/tasks/{task_id}/autopilot"' in router_src,
   "a standalone task scope has a REST route not nested under a deliverable")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
