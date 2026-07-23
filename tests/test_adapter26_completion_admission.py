"""ADAPTER-26: completion decisions are immutable runner admissions."""
from __future__ import annotations

import json
from unittest.mock import patch

from path_setup import ROOT

from switchboard.application.commands import connect_dispatch
from switchboard.connect import (
    Ack, Assignment, HostRuntimeConfig, ResourceLimits, build_launch_spec,
)
from switchboard.storage.repositories.external_effects import make_external_effect_key


def ok(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  PASS  {message}")


task = {
    "task_id": "ADAPTER-26",
    "_wsId": "ADAPTER",
    "updated_at": 1_784_842_717.0,
}
captured: list[dict] = []


def request_wake(**kwargs):
    captured.append(kwargs)
    return {"wake_id": "wake-adapter26", "status": "pending"}


decision = {
    "role": "review_merge",
    "source_sha": "a" * 40,
    "reason_code": "review_required",
    "route": "review_merge",
    "decision_attempt": 3,
    "state_version": 7,
    "acceptance_findings": [{"code": "review_required", "blocking": True}],
}
with patch.object(connect_dispatch.coordination_repo, "request_wake", request_wake), \
        patch.object(connect_dispatch, "capacity_readback", lambda *_a, **_k: {}):
    first = connect_dispatch.enqueue_task(
        task, project="switchboard", actor="coordinator/a",
        caller_agent_id="coordinator/a", generation_ref="decision-7", **decision)
    second = connect_dispatch.enqueue_task(
        task, project="switchboard", actor="coordinator/b",
        caller_agent_id="coordinator/b", generation_ref="decision-7", **decision)
    connect_dispatch.enqueue_task(
        task, project="switchboard", actor="coordinator/b",
        caller_agent_id="coordinator/b", generation_ref="decision-8",
        **{**decision, "source_sha": "b" * 40, "state_version": 8})

ok(first["assignment_id"] == second["assignment_id"],
   "same completion decision reuses the assignment receipt identity")
one, replay, changed = captured
ok(one["policy"]["effect_identity"] == replay["policy"]["effect_identity"],
   "effect identity excludes ephemeral coordinator identity")
ok(one["policy"]["effect_identity"] != changed["policy"]["effect_identity"],
   "new exact head/state version creates a distinct effect identity")

key_one = make_external_effect_key(
    "wake", "agent_host", "completion:ADAPTER-26",
    one["policy"]["effect_identity"], project="switchboard")
key_replay = make_external_effect_key(
    "wake", "agent_host", "completion:ADAPTER-26",
    replay["policy"]["effect_identity"], project="switchboard")
key_changed = make_external_effect_key(
    "wake", "agent_host", "completion:ADAPTER-26",
    changed["policy"]["effect_identity"], project="switchboard")
ok(key_one["effect_key"] == key_replay["effect_key"],
   "same decision replay has one stable external effect key")
ok(key_one["effect_key"] != key_changed["effect_key"],
   "new decision produces a new external effect key")

lifecycle = {
    **one["policy"]["lifecycle"],
    "execution_id": "execution-26",
    "generation": 12,
    "fence_epoch": 4,
}
assignment = Assignment(
    assignment_id=first["assignment_id"],
    principal_ref="cursor/launcher-adapter-26",
    work_ref="task:switchboard:ADAPTER-26",
    runtime="codex",
    provider="openai",
    workspace_ref="repo:canonical",
    limits=ResourceLimits(max_runtime_seconds=120),
    queued_at=1_784_842_717.0,
)
ack = Ack(
    lease_id="wake-adapter26",
    runner_id="runner-adapter26",
    assignment=assignment,
    host_id="host/test",
    issued_at=100.0,
    expires_at=220.0,
    heartbeat_interval_seconds=30,
    last_heartbeat_at=100.0,
)
spec = build_launch_spec(
    ack,
    HostRuntimeConfig(
        runtime="codex", provider="openai", executable="codex",
        arguments_before_note=("--test",)),
    workspace_path="/tmp/projectplanner-adapter-26",
    completion_contract=lifecycle,
)
env = dict(spec.environment)
boot = json.loads(env["SWITCHBOARD_COMPLETION_CONTRACT_JSON"])
ok(
    (boot["task_id"], boot["role"], boot["head_sha"], boot["reason_code"],
     boot["generation"], boot["fence_epoch"])
    == ("ADAPTER-26", "review_merge", "a" * 40, "review_required", 12, 4),
    "fresh runner receives role, exact head, reason, generation, and fence at boot")
ok("post-start runner injection" in spec.argv[-1],
   "boot note explicitly forbids lifecycle dependence on runner injection")

print("ADAPTER-26 immutable completion admission: PASS")
