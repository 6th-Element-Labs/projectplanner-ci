#!/usr/bin/env python3
"""UI-27 durable scoped Autopilot integration and contract tests."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile


_TMP = tempfile.mkdtemp(prefix="ui27-autopilot-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import coordinator_daemon  # noqa: E402
import store  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_project_registry()
    store.init_db("switchboard")
    created = store.create_project("UI-27", project_id="qa-ui27", actor="test")
    ok(created.get("created"), "test project created with autopilot migration")

    for payload in (
        {"workstream_id": "AUTO", "title": "Ready one"},
        {"workstream_id": "AUTO", "title": "Blocked follow-on", "depends_on": ["AUTO-1"]},
        {"workstream_id": "AUTO", "title": "Ready two"},
    ):
        store.create_task(payload, actor="test", project="qa-ui27")
    store.create_deliverable(
        {"id": "ui27-deliverable", "title": "UI-27 deliverable",
         "status": "approved", "end_state": "All work drains."},
        actor="test", project="qa-ui27")
    for task_id in ("AUTO-1", "AUTO-2", "AUTO-3"):
        store.link_task_to_deliverable(
            "ui27-deliverable", "qa-ui27", task_id,
            data={"role": "contributes", "blocks_deliverable": True},
            actor="test", project="qa-ui27")

    task_scope = store.start_autopilot_scope(
        project="qa-ui27", deliverable_id="ui27-deliverable",
        scope_type="task", task_project="qa-ui27", task_id="AUTO-2",
        actor="test")
    ok(task_scope.get("status") == "active" and task_scope.get("task_id") == "AUTO-2",
       "a dependency-blocked task can be armed durably")
    invalid_runtime = store.start_autopilot_scope(
        project="qa-ui27", deliverable_id="ui27-deliverable",
        scope_type="task", task_project="qa-ui27", task_id="AUTO-1",
        runtime="not-a-runtime", actor="test")
    ok(invalid_runtime.get("error") == "unsupported autopilot runtime",
       "incompatible runtime intent fails before a scope or wake is queued")
    repeat = store.start_autopilot_scope(
        project="qa-ui27", deliverable_id="ui27-deliverable",
        scope_type="task", task_project="qa-ui27", task_id="AUTO-2",
        actor="test")
    ok(repeat.get("scope_id") == task_scope.get("scope_id") and repeat.get("already_started"),
       "repeated task Start is idempotent")
    paused = store.control_autopilot_scope(
        project="qa-ui27", deliverable_id="ui27-deliverable",
        scope_type="task", task_project="qa-ui27", task_id="AUTO-2",
        action="pause", actor="test")
    resumed = store.control_autopilot_scope(
        project="qa-ui27", deliverable_id="ui27-deliverable",
        scope_type="task", task_project="qa-ui27", task_id="AUTO-2",
        action="resume", actor="test")
    ok(paused.get("status") == "paused" and resumed.get("status") == "active",
       "task scope pause/resume is durable")
    stopped = store.control_autopilot_scope(
        project="qa-ui27", deliverable_id="ui27-deliverable",
        scope_type="task", task_project="qa-ui27", task_id="AUTO-2",
        action="stop", actor="test")
    ok(stopped.get("status") == "stopped", "task scope can be stopped independently")
    task_scope = store.start_autopilot_scope(
        project="qa-ui27", deliverable_id="ui27-deliverable",
        scope_type="task", task_project="qa-ui27", task_id="AUTO-2",
        actor="test")

    deliverable_scope = store.start_autopilot_scope(
        project="qa-ui27", deliverable_id="ui27-deliverable",
        scope_type="deliverable", actor="test")
    historical = store.get_autopilot_scope(task_scope["scope_id"], project="qa-ui27")
    ok(deliverable_scope.get("status") == "active" and historical.get("status") == "superseded",
       "deliverable Start supersedes narrower task scopes")
    covered = store.start_autopilot_scope(
        project="qa-ui27", deliverable_id="ui27-deliverable",
        scope_type="task", task_project="qa-ui27", task_id="AUTO-3",
        actor="test")
    ok(covered.get("scope_id") == deliverable_scope.get("scope_id")
       and covered.get("covered") is True,
       "task Start inside a live deliverable run does not create overlap")

    daemon = coordinator_daemon.CoordinatorDaemon(
        coordinator_daemon.DaemonConfig(
            profile_id="autopilot-default", projects=("qa-ui27",), act=True,
            worker_runtime="codex", max_tasks_per_scope_tick=8),
        store_mod=store, instance_id="ui27-test")
    first_wave = daemon._run_scope("qa-ui27", deliverable_scope)
    task_ids = {row.get("task_id") for row in first_wave.get("receipts") or []}
    ok(first_wave.get("candidate_count") == 2 and task_ids == {"AUTO-1", "AUTO-3"},
       "deliverable Start fans out across the complete ready frontier")
    wakes = store.list_wake_intents(project="qa-ui27")
    ok({row.get("task_id") for row in wakes} == {"AUTO-1", "AUTO-3"},
       "each ready frontier task receives its own runtime wake")
    daemon._run_scope("qa-ui27", deliverable_scope)
    ok(len(store.list_wake_intents(project="qa-ui27")) == len(wakes),
       "repeated daemon ticks do not duplicate wakes")

    mission = store.get_mission_status(
        project="qa-ui27", deliverable_id="ui27-deliverable")
    blocked_scope = {**task_scope, "status": "active"}
    blocked_candidates = daemon._scope_candidates(blocked_scope, mission)
    ok(blocked_candidates == [{"task_id": "AUTO-2", "task_project": "qa-ui27",
                               "action": "target_task"}],
       "task scope remains targeted while its dependency is blocked")
    with store._conn("qa-ui27") as connection:
        connection.execute(
            "UPDATE tasks SET status='Done', updated_at=updated_at+1 WHERE task_id='AUTO-1'")
    unblocked = daemon._run_scope("qa-ui27", blocked_scope)
    ok([row.get("task_id") for row in unblocked.get("receipts") or []] == ["AUTO-2"]
       and any(row.get("task_id") == "AUTO-2"
               for row in store.list_wake_intents(project="qa-ui27")),
       "an armed blocked task dispatches automatically when its dependency becomes Done")

    source = open("static/js/mission.js", encoding="utf-8").read()
    app = open("static/app.js", encoding="utf-8").read()
    routes = open("src/switchboard/api/routers/deliverables.py", encoding="utf-8").read()
    ok("Start deliverable" in source and "Start task" in source
       and "data-autopilot-action" in app,
       "Deliverables UI exposes direct deliverable and task Start actions")
    ok("/{deliverable_id}/autopilot" in routes
       and "/tasks/{task_id}/autopilot" in routes,
       "REST surface supports independent deliverable and task controls")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
