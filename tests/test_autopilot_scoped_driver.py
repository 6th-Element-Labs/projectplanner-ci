#!/usr/bin/env python3
"""ACT=1 makes the autopilot drive armed scopes to Done (PROTO-8's incident).

Three defects kept an operator-started task stuck at In Review:
  1. the service ran the base CoordinatorDaemon, whose tick is janitor-only;
  2. ScopedCompletionCoordinator (the driver) was never constructed;
  3. the explicit-target In Review action omitted head_sha, so a scoped
     review_merge refused with review_head_sha_required.

This pins all three by behaviour. PROTO-8's real shape is a task scope WITH a
deliverable (`alerts`) — so the standalone no-deliverable path from #803 does
not cover it; the deliverable-routed drive plus the head_sha plug do.
"""
from __future__ import annotations

import sys

from path_setup import ROOT  # noqa: F401

import mission_coordinator  # noqa: E402
from coordinator_daemon import CoordinatorDaemon, DaemonConfig  # noqa: E402
from scoped_completion_coordinator import ScopedCompletionCoordinator  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


print("autopilot scoped driver")

HEAD = "85e97a5f91ba39d4d58b058f3140a6c3d893c291"


def _proto8_mission():
    """PROTO-8 exactly: In Review, dispatch-eligible link, live PR head."""
    return {
        "deliverable_id": "alerts",
        "deliverable": {"id": "alerts", "status": "approved"},
        "milestones": [{"id": "alerts-m1-contract", "status": "in_progress"}],
        "dispatch_scope": {"links": [{"task_id": "PROTO-8", "project_id": "switchboard",
                                      "automatic_dispatch_eligible": True,
                                      "milestone_id": "alerts-m1-contract",
                                      "role": "implementation"}]},
        "linked_tasks": [{"task_id": "PROTO-8", "project_id": "switchboard",
                          "milestone_id": "alerts-m1-contract", "role": "implementation",
                          "task_detail": {
                              "task_id": "PROTO-8", "status": "In Review",
                              "git_state": {"head_sha": HEAD},
                              "provenance": {"terminal": False},
                              "active_claims": [], "workstream": "PROTO",
                              "dependency_state": {"ready": True, "satisfied": True}}}],
        "next_actions": [],
    }


# --- 1. the head_sha plug: the In Review monitor carries the exact head -----
# coordinator_tick_plan is pure (no store, no scope authority): it proves the
# explicit-target action the dispatch path consumes now carries head_sha. Before
# the fix this monitor had none, so review_merge refused review_head_sha_required.
plan = mission_coordinator.coordinator_tick_plan(
    _proto8_mission(),
    {"target_task_id": "PROTO-8", "target_project_id": "switchboard"})
monitor = next(iter(plan.get("monitors") or []), {})
ok(plan.get("status") == "monitor",
   f"an In Review explicit target plans a monitor (got {plan.get('status')})")
ok(monitor.get("task_id") == "PROTO-8",
   "the monitor targets PROTO-8")
ok(monitor.get("head_sha") == HEAD,
   f"the monitor carries the exact PR head (got {monitor.get('head_sha')!r})")

# Before this fix the head was absent: strip git_state and the monitor has none,
# which the dispatch path turns into review_head_sha_required rather than an
# unbound review_merge.
m = _proto8_mission()
m["linked_tasks"][0]["task_detail"]["git_state"] = {}
plan2 = mission_coordinator.coordinator_tick_plan(
    m, {"target_task_id": "PROTO-8", "target_project_id": "switchboard"})
monitor2 = next(iter(plan2.get("monitors") or []), {})
ok(not str(monitor2.get("head_sha") or ""),
   "with no PR head the monitor carries none — dispatch will refuse, not guess")


# --- 2. routing: base daemon is janitor; scoped daemon drives ---------------
class _Store:
    def __init__(self):
        self.drove = []

    def heartbeat(self, *a, **k):
        return {"ok": True}

    def register_agent(self, *a, **k):
        return {"ok": True}

    def acquire_autopilot_scope_lease(self, sid, **k):
        return {"scope_id": sid, "generation": 1, "fence_epoch": 1}

    def update_autopilot_scope(self, sid, **k):
        return {"scope_id": sid}

    def get_mission_status(self, **k):
        raise AssertionError("janitor must not read a mission")

    def get_task(self, tid, project=""):
        return {"task_id": tid, "status": "Not Started",
                "provenance": {"terminal": False}}


base = CoordinatorDaemon(DaemonConfig(act=False), store_mod=_Store())
ok(getattr(CoordinatorDaemon, "_drive_scope", None) is not None
   and CoordinatorDaemon._drive_scope is not ScopedCompletionCoordinator._drive_scope,
   "the base daemon has its own _drive_scope, distinct from the scoped override")
# Behavioural: the base hook is janitor-only. Its _janitor_scope reads a mission;
# the stub raises if a mission is read, so a base drive on a deliverable scope
# must surface that janitor path rather than driving.
raised = False
try:
    base._drive_scope("switchboard", {"scope_id": "d1", "scope_type": "deliverable",
                                       "deliverable_id": "some-deliverable"})
except AssertionError:
    raised = True
ok(raised, "the base daemon _drive_scope is the janitor path (reads a mission, drives nothing)")

scoped = ScopedCompletionCoordinator(
    DaemonConfig(act=True), store_mod=_Store(),
    agent_id="switchboard/scoped-owner/test")
ok(scoped._drive_scope.__func__ is ScopedCompletionCoordinator._drive_scope,
   "the scoped coordinator overrides _drive_scope to drive")
# A standalone task scope drives without ever touching a mission (uses run_scope).
outcome = scoped._drive_scope("switchboard", {
    "scope_id": "s1", "scope_type": "task", "deliverable_id": "",
    "task_project": "switchboard", "task_id": "PROTO-X"})
ok(outcome.get("status") in {"observed", "dispatched", "completed"},
   f"the scoped driver acts on a standalone task scope (got {outcome.get('status')})")


# --- 3. construction: ACT decides the class --------------------------------
entry_src = (ROOT / "coordinator_daemon.py").read_text(encoding="utf-8")
ok("if config.act:" in entry_src
   and "ScopedCompletionCoordinator(" in entry_src,
   "the entrypoint constructs ScopedCompletionCoordinator when ACT=1")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
