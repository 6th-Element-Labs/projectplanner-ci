#!/usr/bin/env python3
"""UI-66: honest autopilot coverage per task, batched for the Fleet dock.

The Fleet dock is the exception surface, but it cannot say whether autopilot is
driving the thing you are looking at. Two real failures forced this read:

  * 2026-07-26, scope autopilot-1c45f78ff02e4d76: the deploy restart killed the
    scope's holder at 22:59, the row kept status="active", and every read
    surface reported a dead autopilot as running for 45+ minutes;
  * COORD-76 is linked to no deliverable, so no deliverable page exists from
    which autopilot could ever be armed or even seen for it.

`autopilot_coverage` answers, for a batch of task ids: which live scope covers
this task (a deliverable scope via task links, or a standalone task scope),
with an honest liveness verdict — `armed` (started, no holder yet), `live`
(holder heartbeating), `stale` (holder lease expired: the restart-killed case),
`paused`, or none at all.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from path_setup import ROOT, SRC  # noqa: F401

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="ui66-coverage-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(Path(TMP) / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"
Path(TMP, "projects").mkdir(parents=True)

import store  # noqa: E402
from execution_readiness_fixture import configure_ready_project  # noqa: E402

store.init_project_registry()
store.init_db("maxwell")
configure_ready_project("maxwell", actor="test")

from switchboard.application.commands import autopilot as autopilot_command  # noqa: E402
from switchboard.storage.repositories import autopilot_scopes as scopes_repo  # noqa: E402

passed = 0
failed = 0


def ok(condition: bool, label: str) -> None:
    global passed, failed
    if condition:
        passed += 1
        print("PASS  %s" % label)
    else:
        failed += 1
        print("FAIL  %s" % label)


# Seed: UI66-1 linked to a deliverable; UI66-2 deliberately unlinked (the
# COORD-76 shape); UI66-3 unlinked, for the task-scope case.
store.create_task({"workstream_id": "UI66", "title": "linked task"},
                  actor="test", project="maxwell")
store.create_task({"workstream_id": "UI66", "title": "unlinked task"},
                  actor="test", project="maxwell")
store.create_task({"workstream_id": "UI66", "title": "task-scope task"},
                  actor="test", project="maxwell")
store.create_deliverable({
    "id": "ui66-deliv", "title": "Coverage deliverable",
    "status": "approved", "end_state": "Coverage is honest.",
}, actor="test", project="maxwell")
store.link_task_to_deliverable(
    "ui66-deliv", "maxwell", "UI66-1",
    data={"role": "contributes", "blocks_deliverable": True},
    actor="test", project="maxwell")


def coverage(*task_ids):
    result = autopilot_command.execute_mapping_result(
        "autopilot_coverage", list(task_ids), project="maxwell")
    if result.get("error"):
        raise AssertionError(f"coverage refused: {result}")
    return result["coverage"]


# 1. Nothing armed anywhere: every task reports uncovered, not an error.
cov = coverage("UI66-1", "UI66-2")
ok(cov["UI66-1"]["coverage"] == "none" and cov["UI66-1"]["covered"] is False,
   "linked task with no scope reports coverage none")
ok(cov["UI66-2"]["coverage"] == "none",
   "unlinked task with no scope reports coverage none")

# 2. Deliverable scope armed: the linked task is covered by it; freshly
# started means no holder yet -> liveness "armed", never "live".
started = scopes_repo.start_autopilot_scope(
    project="maxwell", deliverable_id="ui66-deliv", scope_type="deliverable",
    runtime="codex", actor="test")
ok(not started.get("error"), f"deliverable scope starts ({started.get('error')})")
scope_id = started.get("scope_id")
cov = coverage("UI66-1", "UI66-2")
ok(cov["UI66-1"]["coverage"] == "deliverable"
   and cov["UI66-1"]["deliverable_id"] == "ui66-deliv",
   "linked task is covered by the deliverable scope")
ok(cov["UI66-1"]["liveness"] == "armed",
   "scope with no holder yet reads armed, not live")
ok(cov["UI66-1"]["scope_id"] == scope_id, "coverage names the scope id")
ok(cov["UI66-2"]["coverage"] == "none",
   "unlinked task stays uncovered by the deliverable scope")

# 3. A heartbeating holder reads live. Use the REAL lease path: register the
# holder, acquire the scope lease.
store.register_agent("coordinator/ui66", "codex", project="maxwell",
                     ttl_s=600)
now = time.time()
lease = scopes_repo.acquire_autopilot_scope_lease(
    scope_id, holder_agent_id="coordinator/ui66", project="maxwell",
    ttl_seconds=120)
ok(not lease.get("error"), f"holder acquires the scope lease ({lease.get('error')})")
cov = coverage("UI66-1")
ok(cov["UI66-1"]["liveness"] == "live", "heartbeating holder reads live")

# 4. An expired holder lease reads stale — the restart-killed shape. status
# stays "active" in storage; the read must not repeat that lie. Renew the
# lease with `now` in the past so expires_at lands behind the clock, exactly
# what a killed holder leaves on the row.
stale = scopes_repo.acquire_autopilot_scope_lease(
    scope_id, holder_agent_id="coordinator/ui66", project="maxwell",
    ttl_seconds=60, now=now - 3600)
ok(not stale.get("error"), f"stale renewal setup ({stale.get('error')})")
cov = coverage("UI66-1")
ok(cov["UI66-1"]["liveness"] == "stale",
   "expired holder lease reads stale while status is still active")
ok(cov["UI66-1"]["covered"] is True,
   "stale coverage still names the scope (visible, not hidden)")

# 5. Paused reads paused.
paused = scopes_repo.control_autopilot_scope(
    action="pause", project="maxwell", deliverable_id="ui66-deliv",
    scope_type="deliverable", actor="test")
ok(not paused.get("error"), f"pause works ({paused.get('error')})")
cov = coverage("UI66-1")
ok(cov["UI66-1"]["liveness"] == "paused", "paused scope reads paused")

# 6. Standalone task scope covers an unlinked task.
task_scope = scopes_repo.start_autopilot_scope(
    project="maxwell", scope_type="task", task_project="maxwell",
    task_id="UI66-3", runtime="codex", actor="test")
ok(not task_scope.get("error"), f"task scope starts ({task_scope.get('error')})")
cov = coverage("UI66-3", "UI66-2")
ok(cov["UI66-3"]["coverage"] == "task",
   "unlinked task with its own scope reports coverage task")
ok(cov["UI66-2"]["coverage"] == "none", "other unlinked task still none")

# 7. Batch shape: one call, every requested id present.
cov = coverage("UI66-1", "UI66-2", "UI66-3")
ok(set(cov.keys()) == {"UI66-1", "UI66-2", "UI66-3"},
   "batch answers every requested task id")

print(f"\nUI-66 autopilot coverage: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
