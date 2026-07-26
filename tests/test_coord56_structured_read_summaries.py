#!/usr/bin/env python3
"""COORD-56: status reads stay small and full fidelity remains explicit."""
from __future__ import annotations

import json
import inspect

from path_setup import ROOT  # noqa: F401

from switchboard.mcp.tools import read_summaries
from switchboard.mcp.tools import autopilot as autopilot_tools
from switchboard.mcp.tools import runner as runner_tools
from switchboard.mcp.tools import task_execution as task_execution_tools
from switchboard.mcp.tools import tasks as task_tools


CEILING = 16_000
HUGE = "x" * 250_000


def encoded(value):
    return json.dumps(value, sort_keys=True)


execution = {
    "schema": "switchboard.task_execution.v1",
    "project": "switchboard",
    "task_id": "COORD-56",
    "execution_id": "run_1",
    "lifecycle_phase": "running",
    "running": True,
    "starting": False,
    "available_commands": ["get_task_execution", "send_message"],
    "execution": {
        "activity": [{"payload": HUGE}],
        "task": {
            "task_id": "COORD-56", "title": "compact", "status": "In Progress",
            "description": HUGE,
        },
        "active_runner": {
            "generation": 1,
            "runner": {
                "runner_session_id": "run_1", "status": "running",
                "metadata": {"log_tail": HUGE},
            },
        },
    },
}
autopilot = {
    "schema": "switchboard.autopilot.v1",
    "deliverable_id": "d1",
    "scopes": [{"scope_id": "scope_1", "status": "active", "history": HUGE}],
}
task_session = {
    "schema": "switchboard.task_session.v1",
    "task_id": "COORD-56",
    "status": "running",
    "runner": {"runner_session_id": "run_1", "status": "running", "log_tail": HUGE},
    "activity": HUGE,
}
runners = [{
    "runner_session_id": "run_1", "task_id": "COORD-56", "status": "running",
    "metadata": {"log_tail": HUGE}, "command": [HUGE],
}]

summaries = (
    read_summaries.task_execution(execution),
    read_summaries.autopilot(autopilot),
    read_summaries.task_session(task_session),
    read_summaries.runner_sessions(runners),
)
for summary in summaries:
    assert len(encoded(summary)) < CEILING

assert len(encoded(execution)) > 250_000
assert len(encoded(autopilot)) > 250_000
assert len(encoded(task_session)) > 250_000
assert len(encoded(runners)) > 250_000

for function in (
    task_execution_tools.get_task_execution,
    autopilot_tools.get_autopilot,
    task_tools.get_task_session,
    runner_tools.list_runner_sessions,
):
    full = inspect.signature(function).parameters["full"]
    assert full.default is False
    assert full.annotation in (bool, "bool")

print("COORD-56 structured-read summary tests passed")
