#!/usr/bin/env python3
"""BUG-144: task Autopilot Start routes Triage intake before scope creation."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands import autopilot, task_execution
from switchboard.storage.repositories import autopilot_scopes, tasks


def test_autopilot_crosses_start_boundary_and_creates_scope_after_dispatch():
    calls = []
    original_validate = autopilot_scopes.validate_autopilot_target
    original_start = autopilot_scopes.start_autopilot_scope
    try:
        autopilot_scopes.validate_autopilot_target = lambda **_kw: None

        def start_scope(**_kw):
            calls.append("scope")
            return {"scope_id": "autopilot-bug144", "scope_type": "task",
                    "task_id": "BUG-144", "status": "active"}

        autopilot_scopes.start_autopilot_scope = start_scope

        def start_task(command, task_id, **_kw):
            calls.append((command, task_id))
            return {"command": "start_task", "action": "started", "started": True}

        result = autopilot.control_autopilot(
            "deliverable-bug144", project="switchboard", action="start",
            scope_type="task", task_project="switchboard", task_id="BUG-144",
            task_starter=start_task)
        assert calls == [("start_task", "BUG-144"), "scope"]
        assert result["task_start"]["command"] == "start_task"
    finally:
        autopilot_scopes.validate_autopilot_target = original_validate
        autopilot_scopes.start_autopilot_scope = original_start


def test_start_refusal_leaves_no_active_scope():
    created = []
    original_validate = autopilot_scopes.validate_autopilot_target
    original_start = autopilot_scopes.start_autopilot_scope
    try:
        autopilot_scopes.validate_autopilot_target = lambda **_kw: None
        autopilot_scopes.start_autopilot_scope = lambda **_kw: created.append(True)
        result = autopilot.execute_mapping_result(
            "control_autopilot", "deliverable-bug144", project="switchboard",
            action="start", scope_type="task", task_project="switchboard",
            task_id="BUG-144", task_starter=lambda *_a, **_kw: {
                "refused": True, "error": "bug_intake_not_routable",
                "message": "BUG intake disposition 'duplicate' is not dispatchable.",
            })
        assert result["error_code"] == "structural_blocker"
        assert result["task_start"]["error"] == "bug_intake_not_routable"
        assert created == []
    finally:
        autopilot_scopes.validate_autopilot_target = original_validate
        autopilot_scopes.start_autopilot_scope = original_start


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
            task_id="BUG-144", runtime="unsupported-runtime",
            task_starter=lambda *_a, **_kw: calls.append("task_start"))

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
    test_autopilot_crosses_start_boundary_and_creates_scope_after_dispatch()
    test_start_refusal_leaves_no_active_scope()
    test_unsupported_runtime_is_refused_before_task_start_or_scope_creation()
    test_task_start_routes_triage_before_launcher()
    print("BUG-144 autopilot Triage Start: 4 passed")
