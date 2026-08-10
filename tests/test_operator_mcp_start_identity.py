#!/usr/bin/env python3
"""Operator MCP Start does not require a synthetic live launcher agent."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="operator-mcp-start-"))
os.environ["PM_MAXWELL_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)

import store  # noqa: E402
from switchboard.application.commands import task_execution  # noqa: E402
from switchboard.application.commands import submit_bug as submit_bug_command  # noqa: E402
from switchboard.mcp.tools import task_execution as mcp_task_execution  # noqa: E402
from switchboard.mcp.tools import ops as mcp_ops  # noqa: E402


def test_authorized_mcp_operator_launch_needs_no_live_agent() -> None:
    project = "maxwell"
    store.init_db(project)
    task = store.create_task(
        {"workstream_id": "TEST", "title": "operator MCP launch"},
        actor="test-seed", project=project)
    launches: list[str] = []

    result = task_execution.start_task(
        task["task_id"], project=project,
        actor="env-mcp-token", principal_id="env-mcp-token",
        operator_launch_authorized=True,
        launcher=lambda task_id, **_kwargs: (
            launches.append(task_id) or {"dispatched": True}),
    )

    assert result["action"] == "started"
    assert launches == [task["task_id"]]


def test_mcp_start_stamps_operator_launch_authority_after_write_gate() -> None:
    calls = []
    original_execute = mcp_task_execution.task_execution_command.execute_mapping_result
    original_services = mcp_task_execution._SERVICES
    try:
        def capture(command, task_id, **kwargs):
            calls.append((command, task_id, kwargs))
            return {"action": "started", "started": True}

        mcp_task_execution.task_execution_command.execute_mapping_result = capture
        mcp_task_execution._SERVICES = mcp_task_execution.TaskExecutionToolServices(
            dumps=lambda value: value,
            require_write=lambda _ctx, _project: {
                "id": "env-mcp-token", "actor": "env-mcp-token"},
        )

        result = mcp_task_execution.start_task(
            "TEST-1", object(), project="maxwell", runtime="codex")

        assert result["started"] is True
        assert calls[0][2]["operator_launch_authorized"] is True
    finally:
        mcp_task_execution.task_execution_command.execute_mapping_result = original_execute
        mcp_task_execution._SERVICES = original_services


def test_mcp_retry_stamps_operator_launch_authority_after_write_gate() -> None:
    calls = []
    original_execute = mcp_task_execution.task_execution_command.execute_mapping_result
    original_services = mcp_task_execution._SERVICES
    try:
        def capture(command, task_id, **kwargs):
            calls.append((command, task_id, kwargs))
            return {"action": "repair_routed", "repair_task_id": "BUG-2"}

        mcp_task_execution.task_execution_command.execute_mapping_result = capture
        mcp_task_execution._SERVICES = mcp_task_execution.TaskExecutionToolServices(
            dumps=lambda value: value,
            require_write=lambda _ctx, _project: {
                "id": "env-mcp-token", "actor": "env-mcp-token"},
        )

        result = mcp_task_execution.retry_task(
            "TEST-1", object(), project="maxwell", runtime="codex")

        assert result["repair_task_id"] == "BUG-2"
        assert calls[0][2]["operator_launch_authorized"] is True
    finally:
        mcp_task_execution.task_execution_command.execute_mapping_result = original_execute
        mcp_task_execution._SERVICES = original_services


def test_mcp_submit_bug_stamps_operator_launch_authority_after_write_gate() -> None:
    calls = []
    original_execute = submit_bug_command.execute_mapping_result
    original_services = mcp_ops._SERVICES
    try:
        def capture(data, **kwargs):
            calls.append((data, kwargs))
            return {"submitted": True, "bug": {"task_id": "BUG-3"}}

        submit_bug_command.execute_mapping_result = capture
        mcp_ops._SERVICES = mcp_ops.OpsToolServices(
            dumps=lambda value: value,
            require_write=lambda _ctx, _project, _scopes: {
                "id": "env-mcp-token", "actor": "env-mcp-token"},
        )

        result = mcp_ops.submit_bug(
            "TEST-1", "observed", "expected", "repro", "{}", "high",
            "execution", object(), project="maxwell")

        assert result["bug"]["task_id"] == "BUG-3"
        assert calls[0][1]["operator_launch_authorized"] is True
    finally:
        submit_bug_command.execute_mapping_result = original_execute
        mcp_ops._SERVICES = original_services


if __name__ == "__main__":
    test_authorized_mcp_operator_launch_needs_no_live_agent()
    test_mcp_start_stamps_operator_launch_authority_after_write_gate()
    test_mcp_retry_stamps_operator_launch_authority_after_write_gate()
    test_mcp_submit_bug_stamps_operator_launch_authority_after_write_gate()
    print("Operator MCP Start identity: 4 passed")
