#!/usr/bin/env python3
"""Manual MCP Start and Autopilot share Task Execution's launch door."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands import task_execution
from switchboard.application.mission_bot_v4 import runtime as mission_runtime
from switchboard.mcp.tools import task_execution as mcp_task_execution


def source(relative: str) -> str:
    return (Path(ROOT) / relative).read_text(encoding="utf-8")


def test_mcp_and_autopilot_delegate_to_task_execution_start() -> None:
    mcp_source = source("src/switchboard/mcp/tools/task_execution.py")
    runtime_source = source(
        "src/switchboard/application/mission_bot_v4/runtime.py")
    assert '_run("start_task"' in mcp_source
    assert "task_execution.start_task(" in runtime_source
    for adapter_source in (mcp_source, runtime_source):
        assert "connect_dispatch.enqueue_task(" not in adapter_source
        assert "request_wake(" not in adapter_source


def test_autopilot_preserves_the_same_project_bound_start_command() -> None:
    calls = []
    original_start = task_execution.start_task
    try:
        def shared_start(task_id, **kwargs):
            calls.append((task_id, kwargs["project"], kwargs.get("role")))
            return {"action": "started", "started": True}

        task_execution.start_task = shared_start
        ports = mission_runtime.production_ports(
            actor="surface-test",
            agent_id="codex/policy-optional-surface",
            scope_project="future-board-created-after-deploy",
            store_mod=SimpleNamespace(
                validate_autopilot_scope_authority=lambda *_a, **_kw: {
                    "allowed": True,
                },
                get_task=lambda *_a, **_kw: {"task_id": "FUTURE-1"},
                task_has_live_execution=lambda *_a, **_kw: False,
                list_wake_intents=lambda **_kw: [],
            ),
        )
        result = ports.start_task(
            "FUTURE-1",
            project="future-board-created-after-deploy",
            role="implementation",
            scope_authority={"scope_id": "scope-future"},
        )
        assert result["started"] is True
        assert calls == [(
            "FUTURE-1", "future-board-created-after-deploy", "implementation",
        )]
    finally:
        task_execution.start_task = original_start


def test_mcp_preserves_the_same_project_bound_start_command() -> None:
    calls = []
    original_execute = mcp_task_execution.task_execution_command.execute_mapping_result
    original_services = mcp_task_execution._SERVICES
    try:
        def shared_execute(command, task_id, **kwargs):
            calls.append((command, task_id, kwargs["project"], kwargs.get("role")))
            return {"action": "started", "started": True}

        mcp_task_execution.task_execution_command.execute_mapping_result = shared_execute
        mcp_task_execution._SERVICES = mcp_task_execution.TaskExecutionToolServices(
            dumps=lambda value: value,
            require_write=lambda _ctx, _project: {"id": "operator/test"},
        )
        result = mcp_task_execution.start_task(
            "FUTURE-1", object(), project="future-board-created-after-deploy",
            role="implementation", runtime="codex")
        assert result["started"] is True
        assert calls == [(
            "start_task", "FUTURE-1", "future-board-created-after-deploy",
            "implementation",
        )]
    finally:
        mcp_task_execution.task_execution_command.execute_mapping_result = original_execute
        mcp_task_execution._SERVICES = original_services


if __name__ == "__main__":
    test_mcp_and_autopilot_delegate_to_task_execution_start()
    test_autopilot_preserves_the_same_project_bound_start_command()
    test_mcp_preserves_the_same_project_bound_start_command()
    print("Policy-optional Start surfaces: 3 passed")
