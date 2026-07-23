#!/usr/bin/env python3
"""WATCH-17: review remediation uses the stock-host Connect path."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

import ast
from pathlib import Path

import mission_coordinator
from switchboard.application.commands import connect_dispatch, task_execution


def load_host_eligibility():
    """Load the exact host selector without importing optional host dependencies."""
    source = (Path(ROOT) / "adapters" / "agent_host.py").read_text()
    tree = ast.parse(source)
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_csv", "eligible_runtime"}
    ]
    namespace = {"MESSAGE_ONLY_LANE": "__message_only__"}
    exec(compile(ast.Module(body=selected, type_ignores=[]),
                 "agent_host.py", "exec"), namespace)
    return namespace["eligible_runtime"]


class RemediationStore:
    @staticmethod
    def list_review_remediations(**_kwargs):
        return [{
            "status": "queued",
            "acceptance_criteria": [{
                "id": "WATCH17-RED",
                "repair_requirement": "Repair the exact red-review blocker.",
                "class": "auto",
            }],
        }]


captured: list[dict] = []
saved_request = connect_dispatch.coordination_repo.request_wake
saved_projection = task_execution._projection
saved_live_executions = task_execution.runner_repo.task_live_executions


def request_wake(**kwargs):
    captured.append(kwargs)
    return {"wake_id": "wake-watch17", "status": "pending"}


try:
    connect_dispatch.coordination_repo.request_wake = request_wake
    task_execution.runner_repo.task_live_executions = lambda *_args, **_kwargs: []
    task_execution._projection = lambda *_args, **_kwargs: {
        "task": {
            "task_id": "WATCH-17",
            "_wsId": "WATCH",
            "updated_at": 17.0,
            "exit_criteria": (
                '{"findings":[{"id":"WATCH17-RED",'
                '"repair_requirement":"Repair the exact red-review blocker."}]}'
            ),
        },
    }
    role = mission_coordinator._lifecycle_role(
        RemediationStore(), "switchboard", "WATCH-17")
    started = task_execution.start_task(
        "WATCH-17", project="switchboard", actor="switchboard/coordinator",
        role=role, source_sha="a" * 40)
finally:
    connect_dispatch.coordination_repo.request_wake = saved_request
    task_execution._projection = saved_projection
    task_execution.runner_repo.task_live_executions = saved_live_executions


assert role == "remediation"
assert started["started"] is True and started["wake_id"] == "wake-watch17"
assert len(captured) == 1
wake = captured[0]
assert wake["source"] == "connect"
assert wake["policy"]["mode"] == "connect"
assert wake["policy"]["assignment"]["work_ref"] == "task:switchboard:WATCH-17"
assert wake["selector"]["capabilities"] == [
    "execution_lease_v2", "runner_lease_enforcement"]

# This is the unextended inventory shipped by the personal Agent Host.  The
# former code_review_remediation selector rejected it forever; Connect must be
# claimable without adding a one-off host capability.
stock_inventory = {
    "policy": {"allow_work": True, "lane_mode": "all_project_lanes"},
    "runtimes": [{
        "runtime": "codex",
        "provider": "openai",
        "lanes": [],
        "capabilities": [
            "docs", "python", "github", "tests",
            "execution_lease_v2", "runner_lease_enforcement"],
        "policy": {},
    }],
}
claimable_wake = {
    "selector": wake["selector"],
    "policy": wake["policy"],
}
claimed_runtime = load_host_eligibility()(claimable_wake, stock_inventory)
assert claimed_runtime is stock_inventory["runtimes"][0]

print("WATCH-17 review remediation Connect claimability: PASS")
