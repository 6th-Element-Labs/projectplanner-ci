#!/usr/bin/env python3
"""COORD-128: scoped Autopilot assignments execute without planning approval loops."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from switchboard.connect import (  # noqa: E402
    Ack,
    Assignment,
    HostRuntimeConfig,
    LeaseState,
    ResourceLimits,
    build_launch_spec,
)
from switchboard.connect.execution_assignment import SCHEMA  # noqa: E402


def ack_for(runtime: str, provider: str) -> Ack:
    assignment = Assignment(
        assignment_id=f"assignment-{runtime}",
        principal_ref=f"agent/{runtime}/coord128",
        work_ref="task:switchboard:COORD-128",
        runtime=runtime,
        provider=provider,
        workspace_ref="repo:canonical",
        limits=ResourceLimits(max_runtime_seconds=3600),
        queued_at=1,
    )
    return Ack(
        lease_id=f"lease-{runtime}",
        runner_id=f"runner-{runtime}",
        assignment=assignment,
        host_id="host/coord128",
        issued_at=1,
        expires_at=3601,
        heartbeat_interval_seconds=30,
        last_heartbeat_at=1,
        state=LeaseState.ACTIVE,
    )


def contract_for(ack: Ack) -> dict:
    return {
        "schema": SCHEMA,
        "task_id": "COORD-128",
        "execution_id": f"execution-{ack.assignment.runtime}",
        "assignment_id": ack.assignment.assignment_id,
        "generation": 1,
        "desired_role": "implementation",
        "exact_head_sha": "",
        "exact_pr": {"number": 0, "url": ""},
        "workspace_assignment": {"repo_role": "canonical"},
        "claim_expectations": {
            "required": True,
            "work_session_required": True,
            "role": "implementation",
        },
        "typed_tools": {
            "agent_requires_human": "agent_requires_human",
            "mission_yield": "yield_mission",
        },
        "launch_pointer": {"trigger": "explicit_start"},
    }


def test_all_provider_launches_receive_the_execution_phase_boundary() -> None:
    for runtime, provider, executable in (
        ("codex", "openai", "codex"),
        ("claude-code", "anthropic", "claude"),
    ):
        ack = ack_for(runtime, provider)
        config = HostRuntimeConfig(
            runtime=runtime,
            provider=provider,
            executable=executable,
            arguments_before_note=("--prompt",),
        )
        spec = build_launch_spec(
            ack,
            config,
            workspace_path=str(ROOT),
            completion_contract=contract_for(ack),
        )
        prompt = spec.argv[2]
        assert "Planning and scope approval are complete" in prompt
        assert "Do not invoke or repeat brainstorming or writing-plans" in prompt
        assert "Do not ask whether to proceed" in prompt
        assert "systematic debugging, test-driven development, review, and verification" in prompt
        assert "use the typed agent_requires_human tool" in prompt
        assert "Assistant prose is not an authority request" in prompt


def test_unscoped_connect_note_keeps_interactive_planning_available() -> None:
    ack = ack_for("codex", "openai")
    config = HostRuntimeConfig(
        runtime="codex",
        provider="openai",
        executable="codex",
        arguments_before_note=("--prompt",),
    )
    spec = build_launch_spec(ack, config, workspace_path=str(ROOT))
    prompt = spec.argv[2]
    assert "Planning and scope approval are complete" not in prompt
    assert "Do not invoke or repeat brainstorming or writing-plans" not in prompt


if __name__ == "__main__":
    test_all_provider_launches_receive_the_execution_phase_boundary()
    test_unscoped_connect_note_keeps_interactive_planning_available()
    print("COORD-128 Autopilot execution-phase boundary: PASS")
