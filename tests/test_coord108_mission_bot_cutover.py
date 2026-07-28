#!/usr/bin/env python3
"""COORD-108: one scoped Mission Bot path and one bounded launch tape."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT

from coordinator_daemon import DaemonConfig
from scoped_completion_coordinator import ScopedCompletionCoordinator
from switchboard.application.mission_bot.driver import production_mission_ports
from switchboard.domain.mission_bot.dossier import (
    MISSION_TAPE_MAX_BYTES,
    prompt_safe_dossier,
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

    owner = ScopedCompletionCoordinator(
        DaemonConfig(projects=("switchboard",), act=True),
        store_mod=Store(),
        agent_id="codex/COORD-108",
    )
    tick = {"controller": "mission_bot", "execution": {"action": "started"}}
    with patch(
        "switchboard.application.completion_driver.run_completion_tick",
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


def test_scope_authority_is_checked_at_the_work_write_boundary():
    class Store:
        def __init__(self):
            self.target = ""

        def get_project_github_repo(self, _project):
            return ""

        def validate_autopilot_scope_authority(
                self, _authority, **kwargs):
            self.target = kwargs["task_id"]
            return {
                "allowed": False,
                "error": "scope_authority_denied",
                "reason_codes": ["fence_epoch"],
            }

    store = Store()
    ports = production_mission_ports(
        project="switchboard",
        actor="coord108-test",
        agent_id="codex/COORD-108",
        store_mod=store,
        scope_authority={
            "schema": "switchboard.autopilot_scope_authority.v1",
            "scope_id": "scope-coord108",
        },
    )
    try:
        ports.start_task({
            "task_id": "COORD-108-CANARY",
            "role": "implementation",
            "dossier": {"task_id": "COORD-108-CANARY"},
        })
    except RuntimeError as exc:
        assert str(exc) == "scope_authority_denied:fence_epoch"
    else:
        raise AssertionError("stale scope authority reached start_task")
    assert store.target == "COORD-108-CANARY"


def test_scope_authority_cannot_be_omitted_at_the_work_write_boundary():
    ports = production_mission_ports(
        project="switchboard",
        actor="coord108-test",
        agent_id="codex/COORD-108",
        store_mod=object(),
    )
    try:
        ports.start_task({
            "task_id": "COORD-108-CANARY",
            "role": "remediation",
            "head_sha": "a" * 40,
            "dossier": {"task_id": "COORD-108-CANARY"},
        })
    except RuntimeError as exc:
        assert str(exc) == "scope_authority_required"
    else:
        raise AssertionError("missing scope authority reached start_task")


def test_oversized_evidence_becomes_a_bounded_explicit_tape():
    findings = [
        {
            "code": f"failure_{index}",
            "message": "x" * 10000,
            "blocking": True,
            "failing_check_url": f"https://example.invalid/runs/{index}",
            "secret": "must-not-ship",
        }
        for index in range(200)
    ]
    dossier = {
        "schema": "switchboard.mission_dossier.v1",
        "mission": "remediate",
        "reason_code": "required_exact_head_ci_failed",
        "task_id": "COORD-108-CANARY",
        "head_sha": "a" * 40,
        "task": {"title": "Canary", "description": "y" * 100000},
        "github_pr": {"body": "z" * 100000},
        "acceptance_findings": findings,
        "failing_checks": findings,
        "failing_check_url": "https://example.invalid/runs/0",
        "environment": {"GITHUB_TOKEN": "must-not-ship"},
        "evidence_ref": {
            "schema": "switchboard.mission_evidence_ref.v1",
            "decision_id": "decision-coord108",
            "read_via": "list_decision_episodes",
        },
    }
    tape = prompt_safe_dossier(dossier)
    encoded = json.dumps(
        tape, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()
    assert len(encoded) <= MISSION_TAPE_MAX_BYTES
    assert tape["tape_bounds"]["truncated"] is True
    assert tape["tape_bounds"]["source_counts"]["acceptance_findings"] == 200
    assert tape["tape_bounds"]["included_counts"]["acceptance_findings"] == 16
    assert tape["evidence_ref"]["decision_id"] == "decision-coord108"
    assert tape["failing_check_url"] == "https://example.invalid/runs/0"
    assert "must-not-ship" not in encoded.decode()


def test_legacy_dispatch_owner_is_absent():
    source = (Path(ROOT) / "scoped_completion_coordinator.py").read_text()
    assert "run_mission_coordinator_tick" not in source
    assert "mission_coordinator._lifecycle_role" not in source
    assert "route == \"human\"" not in source
    assert "task_execution.start_task" not in source


if __name__ == "__main__":
    test_standalone_first_boot_uses_mission_tick_with_scope_authority()
    test_scope_authority_is_checked_at_the_work_write_boundary()
    test_scope_authority_cannot_be_omitted_at_the_work_write_boundary()
    test_oversized_evidence_becomes_a_bounded_explicit_tape()
    test_legacy_dispatch_owner_is_absent()
    print("COORD-108 Mission Bot cutover: 5 passed")
