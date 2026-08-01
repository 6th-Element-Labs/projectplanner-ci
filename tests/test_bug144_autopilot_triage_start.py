#!/usr/bin/env python3
"""COORD-108: task Autopilot Start establishes scope before any dispatch."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands import autopilot, mission_journal, task_execution
from switchboard.storage.repositories import autopilot_scopes, tasks


def test_autopilot_start_creates_scope_without_pre_scope_dispatch():
    calls = []
    original_validate = autopilot_scopes.validate_autopilot_target
    original_start = autopilot_scopes.start_autopilot_scope
    original_get_task = tasks.get_task
    original_create_mission = mission_journal.create_mission
    try:
        autopilot_scopes.validate_autopilot_target = lambda **_kw: None
        tasks.get_task = lambda *_args, **_kwargs: {"task_id": "BUG-144"}
        mission_journal.create_mission = lambda *_args, **_kwargs: (
            calls.append("mission") or {"mission": {}}
        )

        def start_scope(**_kw):
            calls.append("scope")
            return {"scope_id": "autopilot-bug144", "scope_type": "task",
                    "task_id": "BUG-144", "status": "active"}

        autopilot_scopes.start_autopilot_scope = start_scope

        result = autopilot.control_autopilot(
            "deliverable-bug144", project="switchboard", action="start",
            scope_type="task", task_project="switchboard", task_id="BUG-144")
        assert calls == ["mission", "scope"]
        assert "task_start" not in result
    finally:
        autopilot_scopes.validate_autopilot_target = original_validate
        autopilot_scopes.start_autopilot_scope = original_start
        tasks.get_task = original_get_task
        mission_journal.create_mission = original_create_mission


def test_invalid_target_leaves_no_active_scope():
    created = []
    original_validate = autopilot_scopes.validate_autopilot_target
    original_start = autopilot_scopes.start_autopilot_scope
    try:
        autopilot_scopes.validate_autopilot_target = lambda **_kw: {
            "error": "BUG intake disposition 'duplicate' is not dispatchable.",
        }
        autopilot_scopes.start_autopilot_scope = lambda **_kw: created.append(True)
        result = autopilot.execute_mapping_result(
            "control_autopilot", "deliverable-bug144", project="switchboard",
            action="start", scope_type="task", task_project="switchboard",
            task_id="BUG-144")
        assert result["error_code"] == "invalid_input"
        assert created == []
    finally:
        autopilot_scopes.validate_autopilot_target = original_validate
        autopilot_scopes.start_autopilot_scope = original_start


def test_start_arms_scope_and_leaves_dependency_wait_to_mission_bot():
    created = []
    original_validate = autopilot_scopes.validate_autopilot_target
    original_start = autopilot_scopes.start_autopilot_scope
    original_get_task = tasks.get_task
    original_create_mission = mission_journal.create_mission
    try:
        autopilot_scopes.validate_autopilot_target = lambda **_kw: None
        tasks.get_task = lambda *_args, **_kwargs: {"task_id": "AUTO-2"}
        mission_journal.create_mission = lambda *_args, **_kwargs: {
            "mission": {}
        }

        def start_scope(**kwargs):
            created.append(kwargs)
            return {"scope_id": "autopilot-waiting", "scope_type": "task",
                    "task_id": "AUTO-2", "status": "active"}

        autopilot_scopes.start_autopilot_scope = start_scope
        result = autopilot.control_autopilot(
            "deliverable-ui27", project="switchboard", action="start",
            scope_type="task", task_project="switchboard", task_id="AUTO-2")
        assert result["command"] == "control_autopilot"
        assert result["scope"]["status"] == "active"
        assert "task_start" not in result
        assert len(created) == 1
    finally:
        autopilot_scopes.validate_autopilot_target = original_validate
        autopilot_scopes.start_autopilot_scope = original_start
        tasks.get_task = original_get_task
        mission_journal.create_mission = original_create_mission


def test_unsupported_runtime_is_refused_before_task_start_or_scope_creation():
    calls = []
    original_validate = autopilot_scopes.validate_autopilot_target
    original_start = autopilot_scopes.start_autopilot_scope
    try:
        autopilot_scopes.validate_autopilot_target = lambda **kw: (
            {"error": "unsupported autopilot runtime", "runtime": kw["runtime"],
             "supported_runtimes": ["codex"]}
            if kw["runtime"] != "codex" else None
        )
        autopilot_scopes.start_autopilot_scope = lambda **_kw: calls.append("scope")

        result = autopilot.execute_mapping_result(
            "control_autopilot", "deliverable-bug144", project="switchboard",
            action="start", scope_type="task", task_project="switchboard",
            task_id="BUG-144", runtime="unsupported-runtime")

        assert result["error_code"] == "invalid_input"
        assert result["runtime"] == "unsupported-runtime"
        assert calls == []
    finally:
        autopilot_scopes.validate_autopilot_target = original_validate
        autopilot_scopes.start_autopilot_scope = original_start


def test_task_start_routes_triage_before_launcher():
    state = {"status": "Triage"}
    launches = []
    original_projection = task_execution._projection
    original_route = tasks.route_bug_for_implementation
    try:
        task_execution._projection = lambda *_a, **_kw: {
            "task": {"task_id": "BUG-144", "workstream": "BUG",
                     "status": state["status"]},
            "active_runner": None, "active_attempt": None,
        }

        def route(*_a, **_kw):
            state["status"] = "Not Started"
            return {"routed": True, "previous_status": "Triage",
                    "next_status": "Not Started"}

        tasks.route_bug_for_implementation = route

        def launch(*_a, **_kw):
            launches.append(state["status"])
            return {"dispatched": True, "action": "started"}

        result = task_execution.start_task(
            "BUG-144", project="switchboard", actor="bug144-test",
            role=" IMPLEMENTATION ", launcher=launch)
        assert launches == ["Not Started"]
        assert result["intake_routing"]["routed"] is True
    finally:
        task_execution._projection = original_projection
        tasks.route_bug_for_implementation = original_route


if __name__ == "__main__":
    test_autopilot_start_creates_scope_without_pre_scope_dispatch()
    test_invalid_target_leaves_no_active_scope()
    test_start_arms_scope_and_leaves_dependency_wait_to_mission_bot()
    test_unsupported_runtime_is_refused_before_task_start_or_scope_creation()
    test_task_start_routes_triage_before_launcher()
    print("BUG-144 autopilot Triage Start: 5 passed")
