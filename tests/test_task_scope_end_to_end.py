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

# --- the coordinator drives a standalone task scope, asking for no mission ---
# Behavioural, not textual: the stub store raises if get_mission_status is ever
# called, so a regression that reintroduces the deliverable lookup fails here.
import scoped_completion_coordinator as scc  # noqa: E402
from coordinator_daemon import DaemonConfig  # noqa: E402


class _MissionWouldFail:
    """A store where asking for a mission is a test failure."""

    def __init__(self, task_detail):
        self.task_detail = task_detail
        self.started = []
        self.updates = []

    def get_mission_status(self, **kwargs):
        raise AssertionError(
            "standalone task scope must not require a deliverable mission")

    def acquire_autopilot_scope_lease(self, scope_id, **kwargs):
        return {"scope_id": scope_id, "generation": 3, "fence_epoch": 1}

    def get_task(self, task_id, project=""):
        return dict(self.task_detail)

    def update_autopilot_scope(self, scope_id, **kwargs):
        self.updates.append((scope_id, kwargs))
        return {"scope_id": scope_id}

    def heartbeat(self, *_a, **_k):
        return {"ok": True}

    def register_agent(self, *_a, **_k):
        return {"ok": True}

    def list_review_remediations(self, **_kwargs):
        return []


in_review = {
    "task_id": tid, "status": "In Review",
    "git_state": {"head_sha": "d" * 40},
    "provenance": {"terminal": False},
    "dependency_state": {"ready": True}, "active_claims": [],
}
store_stub = _MissionWouldFail(in_review)
coordinator = scc.ScopedCompletionCoordinator(
    DaemonConfig(act=True), store_mod=store_stub,
    agent_id="switchboard/scoped-owner/test")

dispatched = {}
import switchboard.application.commands.task_execution as te  # noqa: E402
_real_start = te.start_task
te.start_task = lambda t, **kw: dispatched.update(
    {"task_id": t, **kw}) or {"action": "starting"}
try:
    outcome = coordinator.run_scope(
        P, {"scope_id": scope_id, "scope_type": "task",
            "task_project": P, "task_id": tid, "deliverable_id": ""})
finally:
    te.start_task = _real_start

ok(outcome.get("status") == "dispatched",
   f"a standalone task scope dispatches (got {outcome.get('status')})")
ok(dispatched.get("task_id") == tid,
   "it drives exactly the scope's task")
ok(dispatched.get("role") == "review_merge",
   f"an In Review task gets a review_merge generation (got {dispatched.get('role')})")
ok(dispatched.get("source_sha") == "d" * 40,
   "the review generation binds to the exact PR head")

# A review role with no head must refuse rather than dispatch unbound.
store_stub.task_detail = {**in_review, "git_state": {}}
headless = coordinator.run_scope(
    P, {"scope_id": scope_id, "scope_type": "task",
        "task_project": P, "task_id": tid, "deliverable_id": ""})
ok(headless.get("status") == "dispatch_blocked"
   and headless.get("error") == "review_head_sha_required",
   "a review role with no exact head refuses loudly instead of dispatching")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
