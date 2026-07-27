#!/usr/bin/env python3
"""ADR-0008 W1 regression: the host must launch a Connect wake that carries prior_attempts.

COORD-52 puts bounded execution memory (``prior_attempts``) on the execution
assignment at dispatch, carried verbatim. The contract's own rule (see
``switchboard.connect.execution_assignment``) is that every verification-path
caller must ECHO the stored value rather than re-derive it, because the corpus
moves between dispatch and claim. The server claim path echoes it
(``claims.py``); the Agent Host's launch gate did not, so its re-derived
``expected`` lacked the key and exact-equality refused every retry/remediation
launch with ``execution_assignment_contract_mismatch`` — the 2026-07-25 and
2026-07-27 fleet-down signature. First attempts (no history) still launched,
which is why the break looked intermittent.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="adr008-prior-attempts-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from execution_policy_fixture import (  # noqa: E402
    install_ready_execution_policy, ready_execution_context,
)
from switchboard.application.commands import connect_dispatch  # noqa: E402
from switchboard.application.commands.execution_context import (  # noqa: E402
    with_generation,
)
from switchboard.connect.execution_assignment import (  # noqa: E402
    build_execution_assignment,
)
from switchboard.domain.decisions.prior_attempts import (  # noqa: E402
    build_prior_attempts,
)
import adapters.agent_host as agent_host  # noqa: E402


P = "switchboard"
connect_dispatch.execution_context.resolve = lambda **kwargs: ready_execution_context(
    kwargs["task_id"], runtime=kwargs["runtime"])
HEAD = "e62a9c9a414ba904ffe27052b9d859bc4887212d"
REASON = "required_exact_head_ci_failed"


try:
    store.init_db(P)
    install_ready_execution_policy(P)
    task = store.create_task({
        "workstream_id": "BUG",
        "title": "ADR-0008 prior-attempts launch regression",
    }, actor="adr008-test", project=P)
    task["git_state"] = {
        "head_sha": HEAD,
        "pr_number": 946,
        "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/946",
    }
    result = connect_dispatch.enqueue_task(
        task, project=P, actor="adr008-test", runtime="codex",
        generation_ref=f"remediation:{HEAD}",
        role="remediation", source_sha=HEAD,
        reason_code=REASON)
    wake = next(
        row for row in store.list_wake_intents(project=P)
        if row.get("wake_id") == result.get("wake_id"))
    policy = wake["policy"]

    # Re-mint the persisted contract exactly the way dispatch does for a task
    # WITH history: same server mint function, dispatch-time prior_attempts.
    prior = build_prior_attempts(
        [{
            "reason_code": REASON, "head_sha": HEAD, "route": "remediation",
            "outcome": "open", "execution_id": "", "generations_spent": 0,
            "head_advanced": False, "features": {},
        }],
        reason_code=REASON, head_sha=HEAD)
    assert prior, "fixture must produce a non-empty prior_attempts summary"
    assignment = dict(policy["assignment"])
    assignment.pop("schema", None)
    contract = build_execution_assignment(
        task_id=task["task_id"], assignment=assignment,
        lifecycle=policy["lifecycle"], prior_attempts=prior)
    assert contract.get("prior_attempts") == prior
    policy["execution_assignment"] = contract

    policy["execution_context"] = with_generation({
        "schema": "switchboard.execution_context.v1",
        "project_id": P,
        "task_id": task["task_id"],
        "repository": "6th-Element-Labs/projectplanner",
        "base_sha": HEAD,
        "workspace": {"isolation": "worktree"},
        "runtime": {"registry_name": "codex"},
        "provider": {
            "provider": "openai-codex",
            "connection_reference": "provider-test",
            "credential_version": 1,
            "lifecycle_state": "active",
            # The live emitter's word. The binding validator refusing it took
            # every Connect launch down with provider_connection_revoked.
            "revocation_state": "not_revoked",
        },
        "authority_digest": "sha256:adr008",
    }, policy["lifecycle"]["generation"])

    inventory = {
        "host_id": "host/adr008",
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

    # The regression: this raised "connect execution assignment disagrees with
    # persisted lease: execution_assignment_contract_mismatch".
    command, mode = agent_host.launch_command(
        wake, inventory, runner_session_id="run_adr008prior",
        workspace_path=str(ROOT))
    assert mode == "connect", mode

    # Echoing prior_attempts must not weaken the gate for every other field:
    # tampering anything else in the persisted contract still refuses launch.
    for forged in (
        {**contract, "desired_role": "review_merge"},
        {**contract, "exact_head_sha": "0" * 40},
        {**contract, "reason_code": "review_required"},
    ):
        forged_wake = {
            **wake,
            "policy": {**policy, "execution_assignment": forged},
        }
        try:
            agent_host.launch_command(
                forged_wake, inventory,
                runner_session_id="run_adr008prior",
                workspace_path=str(ROOT))
        except ValueError as exc:
            assert "execution assignment disagrees" in str(exc), exc
        else:
            raise AssertionError("tampered execution contract launched")

    # A first-ever dispatch (no prior_attempts key at all) must keep working.
    bare = {k: v for k, v in contract.items() if k != "prior_attempts"}
    bare_wake = {
        **wake,
        "policy": {**policy, "execution_assignment": bare},
    }
    command, mode = agent_host.launch_command(
        bare_wake, inventory, runner_session_id="run_adr008bare",
        workspace_path=str(ROOT))
    assert mode == "connect", mode
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print("ADR-0008 Connect launch echoes prior_attempts: PASS")
