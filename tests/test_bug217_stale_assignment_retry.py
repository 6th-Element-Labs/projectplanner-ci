"""BUG-217: stale exact-head assignments return to the factory, not a human."""

from __future__ import annotations

from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands import task_execution
from switchboard.connect.execution_assignment import build_execution_assignment
from switchboard.connect.launcher import assignment_note
from switchboard.connect.contract import (
    Ack,
    Assignment,
    LeaseState,
    ResourceLimits,
)


EXPECTED = "a" * 40
LIVE = "b" * 40


contract = build_execution_assignment(
    task_id="BUG-217",
    assignment={"assignment_id": "assignment-217"},
    lifecycle={
        "role": "remediation",
        "execution_id": "exec-217",
        "generation": 3,
        "head_sha": EXPECTED,
        "pr_number": 1041,
        "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/1041",
        "reason_code": "required_exact_head_ci_failed",
        "acceptance_findings": [{
            "summary": "cached diagnosis",
            "failing_check_url": "https://example.test/check/217",
        }],
        "mission_dossier": {
            "task": {"description": "large cached task prose"},
            "prior_attempts": {"attempt_number": 99},
            "acceptance_findings": [{"summary": "duplicated diagnosis"}],
        },
    },
    prior_attempts={"attempt_number": 7},
)
assert "reason_code" not in contract
assert "acceptance_findings" not in contract
assert "launch_hints" not in contract
assert "mission_dossier" not in contract
assert "prior_attempts" not in contract
assert contract["launch_pointer"] == {
    "trigger": "required_exact_head_ci_failed",
    "evidence_url": "https://example.test/check/217",
}
assert len(__import__("json").dumps(contract)) < 1200
assert contract["typed_tools"]["stale_assignment"] == "report_stale_assignment"

ack = Ack(
    lease_id="lease-217",
    runner_id="runner-217",
    assignment=Assignment(
        assignment_id="assignment-217",
        work_ref="task:switchboard:BUG-217",
        workspace_ref="repo:canonical@base",
        runtime="codex",
        provider="openai",
        principal_ref="agent/codex/bug-217",
        limits=ResourceLimits(max_runtime_seconds=3600),
        queued_at=1,
    ),
    host_id="host/217",
    issued_at=1,
    expires_at=2,
    heartbeat_interval_seconds=30,
    last_heartbeat_at=1,
    state=LeaseState.ACTIVE,
)
prompt = assignment_note(ack, contract)
assert "launch_pointer is a non-authoritative starting pointer" in prompt
assert "read the current Switchboard task and execution" in prompt
assert "read all open structured findings for that head" in prompt
assert "read PR reviews, inline comments, and conversation comments" in prompt
assert "report_stale_assignment" in prompt
assert "do not call agent_requires_human for that mechanical refresh" in prompt
assert "large cached task prose" not in prompt
assert "duplicated diagnosis" not in prompt

projection = {
    "active_attempt": {
        "execution_id": "exec-217",
        "role": "remediation",
        "execution": {
            "execution_id": "exec-217",
            "generation": 3,
            "role": "remediation",
            "head_sha": EXPECTED,
        },
    },
    "active_runner": {"runner_session_id": "run-217"},
}
replacement_results = [
    {
        "schema": "switchboard.task_execution.v1",
        "command": "start_task",
        "action": "transitioning",
        "started": False,
        "superseded_execution_id": "run-217",
    },
    {
        "schema": "switchboard.task_execution.v1",
        "command": "start_task",
        "action": "started",
        "started": True,
        "execution_id": "run-218",
    },
    {
        "schema": "switchboard.task_execution.v1",
        "command": "start_task",
        "action": "attach",
        "started": False,
        "attached": True,
        "execution_id": "run-218",
    },
]
completion_run = {
    "schema": "switchboard.completion_run.v1",
    "task_id": "BUG-217",
    "head_sha": LIVE,
    "state": "blocked",
    "route": "remediation",
    "reason_code": "stale_assignment",
    "desired_role": "remediation",
    "evidence_refs": {},
}
with (
    patch.object(task_execution, "_projection", return_value=projection),
    patch.object(
        task_execution.completion_runs_repo,
        "get_active_completion_run",
        return_value={
            "pr_number": 1041,
            "board_status": "In Review",
            "evidence_refs": {},
        },
    ),
    patch.object(
        task_execution.completion_runs_repo,
        "transition_completion_run",
        return_value=completion_run,
    ) as persist,
    patch.object(
        task_execution, "start_task", side_effect=replacement_results,
    ) as start,
):
    results = [
        task_execution.report_stale_assignment(
            "BUG-217",
            project="switchboard",
            actor="agent/codex/bug-217",
            principal_id="direct-session/run-217",
            expected_head=EXPECTED,
            live_head=LIVE,
            pr_number=1041,
            pr_url="https://github.com/6th-Element-Labs/projectplanner/pull/1041",
            evidence_url="https://github.com/6th-Element-Labs/projectplanner/pull/1041",
        )
        for _ in range(3)
    ]

assert all(result["schema"] == "switchboard.stale_assignment.v1"
           for result in results)
assert all(result["outcome"] == "stale_assignment" for result in results)
assert all(result["expected_head"] == EXPECTED for result in results)
assert all(result["live_head"] == LIVE for result in results)
assert all(result["execution_id"] == "exec-217" for result in results)
assert all(result["generation"] == 3 for result in results)
assert all(result["attention_created"] is False for result in results)
assert [result["replacement"]["action"] for result in results] == [
    "transitioning", "started", "attach",
]
assert sum(bool(result["replacement"].get("started")) for result in results) == 1
assert persist.call_count == 3
for persisted in persist.call_args_list:
    decision = persisted.args[0]
    assert decision["head_sha"] == LIVE
    assert decision["state"] == "blocked"
    assert decision["route"] == "remediation"
    assert decision["reason_code"] == "stale_assignment"
    assert decision["desired_role"] == "remediation"
    assert decision["evidence_refs"]["stale_assignment"]["outcome"] == (
        "stale_assignment"
    )
assert start.call_count == 3
for started in start.call_args_list:
    kwargs = started.kwargs
    assert kwargs["role"] == "remediation"
    assert kwargs["source_sha"] == LIVE
    assert kwargs["findings"][0]["outcome"] == "stale_assignment"

# (retired with SIMPLIFY-30) The v1 reducer lifecycle replay that followed is
# deleted with the reducer itself; the durable stale-assignment receipt and the
# single remediation replacement above are the live contract.

print("BUG-217 stale-assignment retry contract: PASS")
