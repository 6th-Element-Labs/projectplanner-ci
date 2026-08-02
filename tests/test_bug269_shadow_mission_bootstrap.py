#!/usr/bin/env python3
"""BUG-269: v4 mission inboxes exist before Autopilot scopes are visible."""
from __future__ import annotations

from path_setup import ROOT as _ROOT  # noqa: F401

from switchboard.application.commands import autopilot, mission_journal
from switchboard.storage.repositories import autopilot_scopes, deliverables, tasks


PROJECT = "switchboard"


def _scope(**kwargs):
    return {
        "scope_id": "autopilot-bug269",
        "scope_type": kwargs.get("scope_type"),
        "status": "active",
        "generation": 1,
        "fence_epoch": 0,
    }


def test_initial_role_uses_only_persisted_pr_identity():
    assert mission_journal.initial_requested_role({}) == "implementation"
    assert mission_journal.initial_requested_role({
        "status": "Blocked",
        "git_state": {"pr_number": 1227},
    }) == "review_merge"
    assert mission_journal.initial_requested_role({
        "status": "In Review",
        "git_state": {},
    }) == "implementation"


def test_task_journal_is_bootstrapped_before_scope_visibility():
    calls = []
    originals = (
        autopilot_scopes.validate_autopilot_target,
        autopilot_scopes.start_autopilot_scope,
        tasks.get_task,
        mission_journal.create_mission,
        mission_journal.ensure_scope_start_event,
    )
    try:
        autopilot_scopes.validate_autopilot_target = lambda **_kwargs: None
        tasks.get_task = lambda *_args, **_kwargs: {
            "task_id": "QA-117", "git_state": {},
        }
        mission_journal.create_mission = lambda task_id, **kwargs: (
            calls.append(("mission", kwargs["project"], task_id,
                          kwargs["requested_role"])) or {"mission": {}}
        )
        mission_journal.ensure_scope_start_event = lambda task_id, **kwargs: (
            calls.append(("rearm", kwargs["project"], task_id,
                          kwargs["scope_id"])) or {"created": False}
        )
        autopilot_scopes.start_autopilot_scope = lambda **kwargs: (
            calls.append(("scope", kwargs["project"], kwargs["task_id"]))
            or _scope(**kwargs)
        )

        result = autopilot.control_autopilot(
            "", project=PROJECT, action="start", scope_type="task",
            task_project=PROJECT, task_id="qa-117", runtime="codex",
        )
        assert result["scope"]["status"] == "active"
        assert calls == [
            ("mission", PROJECT, "QA-117", "implementation"),
            ("scope", PROJECT, "qa-117"),
            ("rearm", PROJECT, "QA-117", "autopilot-bug269"),
        ]
    finally:
        (
            autopilot_scopes.validate_autopilot_target,
            autopilot_scopes.start_autopilot_scope,
            tasks.get_task,
            mission_journal.create_mission,
            mission_journal.ensure_scope_start_event,
        ) = originals


def test_deliverable_bootstraps_nonterminal_tasks_and_skips_proven_done():
    calls = []
    originals = (
        deliverables.get_mission_status,
        autopilot_scopes.start_autopilot_scope,
        mission_journal.create_mission,
        mission_journal.ensure_scope_start_event,
    )
    try:
        deliverables.get_mission_status = lambda **_kwargs: {
            "linked_tasks": [
                {
                    "task_id": "QA-118",
                    "project_id": PROJECT,
                    "task_detail": {"status": "Ready", "git_state": {}},
                },
                {
                    "task_id": "QA-119",
                    "project_id": PROJECT,
                    "task_detail": {
                        "status": "In Review",
                        "git_state": {"pr_url": "https://example/pr/119"},
                    },
                },
                {
                    "task_id": "QA-120",
                    "project_id": PROJECT,
                    "task_detail": {
                        "status": "Done",
                        "provenance": {"terminal": True},
                        "git_state": {"pr_number": 120},
                    },
                },
            ],
        }
        mission_journal.create_mission = lambda task_id, **kwargs: (
            calls.append(("mission", task_id, kwargs["requested_role"]))
            or {"mission": {}}
        )
        mission_journal.ensure_scope_start_event = lambda task_id, **kwargs: (
            calls.append(("rearm", task_id, kwargs["scope_id"]))
            or {"created": False}
        )
        autopilot_scopes.start_autopilot_scope = lambda **kwargs: (
            calls.append(("scope", kwargs["deliverable_id"])) or _scope(**kwargs)
        )

        autopilot.control_autopilot(
            "v4-deliverable", project=PROJECT, action="start",
            scope_type="deliverable", runtime="codex",
        )
        assert calls == [
            ("mission", "QA-118", "implementation"),
            ("mission", "QA-119", "review_merge"),
            ("scope", "v4-deliverable"),
            ("rearm", "QA-118", "autopilot-bug269"),
            ("rearm", "QA-119", "autopilot-bug269"),
        ]
    finally:
        (
            deliverables.get_mission_status,
            autopilot_scopes.start_autopilot_scope,
            mission_journal.create_mission,
            mission_journal.ensure_scope_start_event,
        ) = originals


def test_bootstrap_failure_never_publishes_scope():
    scope_calls = []
    originals = (
        autopilot_scopes.validate_autopilot_target,
        autopilot_scopes.start_autopilot_scope,
        tasks.get_task,
        mission_journal.create_mission,
    )
    try:
        autopilot_scopes.validate_autopilot_target = lambda **_kwargs: None
        tasks.get_task = lambda *_args, **_kwargs: {"task_id": "QA-117"}
        mission_journal.create_mission = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(RuntimeError("journal unavailable"))
        )
        autopilot_scopes.start_autopilot_scope = (
            lambda **_kwargs: scope_calls.append(True)
        )
        try:
            autopilot.control_autopilot(
                "", project=PROJECT, action="start", scope_type="task",
                task_project=PROJECT, task_id="QA-117",
            )
        except autopilot.AutopilotError as exc:
            assert exc.code == "mission_bootstrap_failed"
            assert exc.details["mission_task_id"] == "QA-117"
            assert exc.details["cause"] == "RuntimeError: journal unavailable"
        else:
            raise AssertionError("journal failure must fail closed")
        assert scope_calls == []

        envelope = autopilot.execute_mapping_result(
            "control_autopilot", "", project=PROJECT, action="start",
            scope_type="task", task_project=PROJECT, task_id="QA-117",
        )
        assert envelope["error_code"] == "mission_bootstrap_failed"
        assert envelope["failure_class"] == "missing_data"
        assert autopilot.error_status(envelope) == 503
        assert scope_calls == []
    finally:
        (
            autopilot_scopes.validate_autopilot_target,
            autopilot_scopes.start_autopilot_scope,
            tasks.get_task,
            mission_journal.create_mission,
        ) = originals


if __name__ == "__main__":
    test_initial_role_uses_only_persisted_pr_identity()
    test_task_journal_is_bootstrapped_before_scope_visibility()
    test_deliverable_bootstraps_nonterminal_tasks_and_skips_proven_done()
    test_bootstrap_failure_never_publishes_scope()
    print("BUG-269 v4 mission bootstrap: PASS")
