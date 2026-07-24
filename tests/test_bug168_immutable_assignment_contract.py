#!/usr/bin/env python3
"""BUG-168: remediation launches carry one immutable exact-head contract."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

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

    forged = {**contract, "desired_role": "review_merge"}
    # The host compares the prompt contract to lifecycle before calling this
    # builder; its final boundary also rejects assignment identity forgery.
    try:
        build_launch_spec(
            ack, config, workspace_path=str(ROOT),
            completion_contract={**forged, "assignment_id": "assignment-forged"})
    except LaunchRefused as exc:
        assert exc.code == "execution_assignment_id_mismatch"
    else:
        raise AssertionError("forged assignment contract launched")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print("BUG-168 immutable remediation assignment contract: PASS")
