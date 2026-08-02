#!/usr/bin/env python3
"""COORD-108: one scoped Mission Bot path and one bounded launch tape."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT

from coordinator_daemon import DaemonConfig
from switchboard.application.mission_bot_v4.coordinator import (
    V4ScopedCompletionCoordinator,
)


def test_standalone_first_boot_uses_mission_tick_with_scope_authority():
    authority = {
        "schema": "switchboard.autopilot_scope_authority.v1",
        "scope_id": "scope-coord108",
        "lease_id": "lease-coord108",
        "holder_agent_id": "codex/COORD-108",
        "generation": 1,
        "fence_epoch": 1,
        "expires_at": 9999999999,
        "task_id": "COORD-108-CANARY",
        "task_project": "switchboard",
        "deliverable_id": "",
    }

    class Store:
        def __init__(self):
            self.updates = []

        def get_task(self, *_args, **_kwargs):
            return {"task_id": "COORD-108-CANARY", "status": "Not Started"}

        def update_autopilot_scope(self, scope_id, **kwargs):
            self.updates.append((scope_id, kwargs))
            return {"scope_id": scope_id}

        def complete_completion_wake_for_tick(self, *_args, **_kwargs):
            return {"status": "not_found"}

    owner = V4ScopedCompletionCoordinator(
        DaemonConfig(projects=("switchboard",), act=True),
        store_mod=Store(),
        agent_id="codex/COORD-108",
    )
    tick = {"controller": "mission_bot", "execution": {"action": "started"}}
    with patch(
        "switchboard.application.mission_bot_v4.run_scoped_mission_tick",
        return_value=tick,
    ) as run:
        result = owner._run_standalone_task_scope(
            "switchboard",
            {
                "scope_id": "scope-coord108",
                "scope_type": "task",
                "task_project": "switchboard",
                "task_id": "COORD-108-CANARY",
            },
            authority,
        )
    assert result["status"] == "completion_tick"
    assert run.call_args.kwargs["scope_authority"] == authority
    assert run.call_args.kwargs["store_mod"] is owner.store


def test_legacy_dispatch_owner_is_absent():
    source = (Path(ROOT) / "scoped_completion_coordinator.py").read_text()
    assert "run_mission_coordinator_tick" not in source
    assert "mission_coordinator._lifecycle_role" not in source
    assert "route == \"human\"" not in source
    assert "task_execution.start_task" not in source


if __name__ == "__main__":
    test_standalone_first_boot_uses_mission_tick_with_scope_authority()
    test_legacy_dispatch_owner_is_absent()
    print("COORD-108 Mission Bot cutover: 2 passed")
