#!/usr/bin/env python3
"""COORD-111: restarting a stopped v4 scope must re-arm its caught-up pager."""
from __future__ import annotations

import os
import tempfile

from path_setup import ROOT as _ROOT  # noqa: F401


TEMP = tempfile.TemporaryDirectory()
os.environ["PM_DB_PATH"] = os.path.join(TEMP.name, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(TEMP.name, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(TEMP.name, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(TEMP.name, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TEMP.name

from switchboard.application.commands import autopilot  # noqa: E402
from switchboard.storage.repositories.mission_journal import (  # noqa: E402
    default_mission_journal_repository as journal,
)
import store  # noqa: E402


PROJECT = "switchboard"


def test_scope_restart_publishes_one_exact_rearm_event():
    store.init_db(PROJECT)
    task = store.create_task(
        {"workstream_id": "COORD", "title": "v4 scope re-arm"},
        actor="test", project=PROJECT,
    )
    task_id = task["task_id"]

    first = autopilot.control_autopilot(
        "", project=PROJECT, action="start", scope_type="task",
        task_project=PROJECT, task_id=task_id, actor="operator/test",
    )
    first_scope = first["scope"]
    first_events = journal.list_events(task_id, project=PROJECT)
    assert [event["event_type"] for event in first_events] == ["mission_started"]

    item = journal.get_item(task_id, project=PROJECT)
    journal.update_item(
        task_id,
        project=PROJECT,
        state="ACTIVE",
        requested_role=item["requested_role"],
        expected_version=item["version"],
        handled_through=item["latest_sequence"],
    )
    autopilot.control_autopilot(
        "", project=PROJECT, action="stop", scope_type="task",
        task_project=PROJECT, task_id=task_id, actor="operator/test",
    )

    restarted = autopilot.control_autopilot(
        "", project=PROJECT, action="start", scope_type="task",
        task_project=PROJECT, task_id=task_id, actor="operator/test",
    )
    restarted_scope = restarted["scope"]
    assert restarted_scope["scope_id"] != first_scope["scope_id"]

    events = journal.list_events(task_id, project=PROJECT)
    assert [event["event_type"] for event in events] == [
        "mission_started", "mission_started",
    ]
    rearm = events[-1]
    expected_payload = {
        "scope_id": restarted_scope["scope_id"],
        "scope_generation": restarted_scope["generation"],
        "start_ref": restarted_scope["scope_id"],
    }
    if restarted_scope["fence_epoch"] > 0:
        expected_payload["scope_fence"] = restarted_scope["fence_epoch"]
    assert rearm["payload"] == expected_payload
    assert rearm["idempotency_key"] == (
        f"mission_started:{task_id}:scope:{restarted_scope['scope_id']}"
    )

    replay = autopilot.control_autopilot(
        "", project=PROJECT, action="start", scope_type="task",
        task_project=PROJECT, task_id=task_id, actor="operator/test",
    )
    assert replay["scope"]["scope_id"] == restarted_scope["scope_id"]
    assert len(journal.list_events(task_id, project=PROJECT)) == 2


if __name__ == "__main__":
    test_scope_restart_publishes_one_exact_rearm_event()
    print("COORD-111 scope re-arm event: PASS")
