#!/usr/bin/env python3
"""BUG-168: remediation launches carry one immutable exact-head contract."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="bug168-assignment-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from switchboard.application.commands import connect_dispatch  # noqa: E402
from switchboard.connect import (  # noqa: E402
    Ack, HostRuntimeConfig, LaunchRefused, LeaseState, build_launch_spec,
)
import adapters.agent_host as agent_host  # noqa: E402


P = "switchboard"
HEAD = "1af20970ba52ed4cb862c3ae08d44b6b9ccdcde0"
FINDINGS = [{
    "id": "reviewremediation-de1aaf65367940b9",
    "repair_requirement": "Implement the three accepted remediation findings.",
}]


try:
    store.init_db(P)
    task = store.create_task({
        "workstream_id": "BUG",
        "title": "SIMPLIFY-16 regression",
    }, actor="bug168-test", project=P)
    task["git_state"] = {
        "head_sha": HEAD,
        "pr_number": 831,
        "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/831",
    }
    result = connect_dispatch.enqueue_task(
        task, project=P, actor="bug168-test", runtime="codex",
        generation_ref=f"remediation:{HEAD}",
        role="remediation", source_sha=HEAD,
        reason_code="changes_requested", acceptance_findings=FINDINGS)
    wake = next(
        row for row in store.list_wake_intents(project=P)
        if row.get("wake_id") == result.get("wake_id"))
    policy = wake["policy"]
    assignment = policy["assignment"]
    contract = policy["execution_assignment"]
    policy["execution_context"] = {
        "schema": "switchboard.execution_context.v1",
        "project_id": P,
        "task_id": task["task_id"],
        "repository": "6th-Element-Labs/projectplanner",
        "base_sha": HEAD,
        "generation": policy["lifecycle"]["generation"],
        "workspace": {"isolation": "worktree"},
        "runtime": {"registry_name": "codex"},
        "authority_digest": "sha256:bug168",
        "digest": "sha256:bug168-generation-1",
    }

    assert contract["task_id"] == task["task_id"]
    assert contract["assignment_id"] == assignment["assignment_id"]
    assert contract["execution_id"] == policy["lifecycle"]["execution_id"]
    assert contract["generation"] == policy["lifecycle"]["generation"] == 1
    assert contract["desired_role"] == "remediation"
    assert contract["exact_head_sha"] == HEAD
    assert contract["exact_pr"]["number"] == 831
    assert contract["acceptance_findings"] == FINDINGS
    assert contract["claim_expectations"] == {
        "required": True,
        "work_session_required": True,
        "role": "remediation",
    }

    from switchboard.connect.contract import Assignment, ResourceLimits
    data = dict(assignment)
    data.pop("schema")
    data["limits"] = ResourceLimits(**data["limits"])
    ack = Ack(
        lease_id=wake["wake_id"], runner_id="run_13a36dcc04555b14",
        assignment=Assignment(**data), host_id="host/bug168",
        issued_at=1, expires_at=100, heartbeat_interval_seconds=30,
        last_heartbeat_at=1, state=LeaseState.ACTIVE)
    config = HostRuntimeConfig(
        runtime="codex", provider="openai", executable="codex",
        arguments_before_note=("--prompt",))
    spec = build_launch_spec(
        ack, config, workspace_path=str(ROOT), completion_contract=contract)
    prompt = spec.argv[2]
    assert "desired_role" in prompt and "remediation" in prompt
    assert HEAD in prompt and "reviewremediation-de1aaf65367940b9" in prompt
    assert "Claim and start exactly desired_role" in prompt
    assert "do not infer a different role from board status" in prompt
    assert json.loads(
        spec.env_dict()["SWITCHBOARD_EXECUTION_ASSIGNMENT_JSON"]) == contract

    inventory = {
        "host_id": "host/bug168",
        "repo_root": str(ROOT),
        "policy": {"allow_work": True},
        "runtimes": [{
            "runtime": "codex",
            "provider": "openai",
            "lanes": ["BUG"],
            "capabilities": [
                "execution_lease_v2", "runner_lease_enforcement"],
            "policy": {
                "allow_work": True,
                "lane_mode": "all_project_lanes",
            },
        }],
    }
    command, mode = agent_host.launch_command(
        wake, inventory, runner_session_id="run_13a36dcc04555b14",
        workspace_path=str(ROOT))
    child = command[command.index("--") + 1:]
    host_prompt = next(
        part for part in child
        if isinstance(part, str) and "Immutable execution assignment:" in part)
    assert mode == "connect"
    assert '"desired_role":"remediation"' in host_prompt
    assert HEAD in host_prompt
    assert "Claim and start exactly desired_role" in host_prompt

    original_token = agent_host._issue_connect_session_mcp_token
    original_run = agent_host.subprocess.run
    original_materialize = agent_host.materialize_repository_workspace
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "runner_session_id": "run_13a36dcc04555b14",
                "started": True,
            }),
            stderr="",
        )

    try:
        agent_host._issue_connect_session_mcp_token = (
            lambda *_args, **_kwargs: "dst-bug168")
        agent_host.materialize_repository_workspace = (
            lambda *_args, **_kwargs: SimpleNamespace(
                path=ROOT,
                receipt_path=TMP / "workspace-receipt.json",
                receipt={"schema": "switchboard.repository_workspace_receipt.v1"},
            ))
        agent_host.subprocess.run = fake_run
        launched = agent_host.launch(
            wake, inventory, runner_session_id="run_13a36dcc04555b14")
    finally:
        agent_host._issue_connect_session_mcp_token = original_token
        agent_host.materialize_repository_workspace = original_materialize
        agent_host.subprocess.run = original_run
    assert launched["started"] is True
    assert json.loads(
        captured["env"]["SWITCHBOARD_EXECUTION_ASSIGNMENT_JSON"]) == contract
    assert json.loads(
        captured["env"]["SWITCHBOARD_COMPLETION_CONTRACT_JSON"]) == contract

    for forged in (
        {**contract, "desired_role": "review_merge"},
        {**contract, "exact_pr": {**contract["exact_pr"], "number": 999}},
        {**contract, "acceptance_findings": []},
    ):
        forged_wake = {
            **wake,
            "policy": {**policy, "execution_assignment": forged},
        }
        try:
            agent_host.launch_command(
                forged_wake, inventory,
                runner_session_id="run_13a36dcc04555b14",
                workspace_path=str(ROOT))
        except ValueError as exc:
            assert "execution assignment disagrees" in str(exc)
        else:
            raise AssertionError("tampered execution contract launched")

    refused = connect_dispatch.enqueue_task(
        {**task, "git_state": {}},
        project=P, actor="bug168-test", runtime="codex",
        generation_ref="remediation:missing-head",
        role="remediation", source_sha="",
    )
    assert refused == {
        "dispatched": False,
        "error": "exact_head_required",
        "role": "remediation",
        "task_id": task["task_id"],
    }

    empty_head = {**contract, "exact_head_sha": ""}
    try:
        build_launch_spec(
            ack, config, workspace_path=str(ROOT),
            completion_contract=empty_head)
    except LaunchRefused as exc:
        assert exc.code == "execution_assignment_exact_head_missing"
    else:
        raise AssertionError("exact-head role launched without a head")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print("BUG-168 immutable remediation assignment contract: PASS")
