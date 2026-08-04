#!/usr/bin/env python3
"""ADAPTER-49: typed executed-test MCP writes bind shared principals."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path


TMP = Path(tempfile.mkdtemp(prefix="adapter49-executed-test-binding-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)

from path_setup import ROOT as _ROOT  # noqa: E402,F401

import store  # noqa: E402
from switchboard.mcp.tools import claims as claim_tools  # noqa: E402


PROJECT = "switchboard"
AGENT = "codex/ADAPTER-49-executed-test-binding"
HEAD = "a" * 40
OUTPUT_SHA256 = "b" * 64


def decode(value: str) -> dict:
    result = json.loads(value)
    assert isinstance(result, dict), result
    return result


try:
    store.init_project_registry()
    store.init_db(PROJECT)
    task = store.create_task(
        {"workstream_id": "ADAPTER", "title": "Bind typed test evidence"},
        actor="adapter49-test",
        project=PROJECT,
    )
    task_id = task["task_id"]
    store.register_agent(
        AGENT,
        "codex",
        lane="ADAPTER",
        ttl_s=120,
        actor="adapter49-test",
        project=PROJECT,
    )
    claimed = store.claim_task(
        task_id,
        AGENT,
        work_session={
            "task_id": task_id,
            "agent_id": AGENT,
            "runtime": "codex",
            "repo_role": "canonical",
            "branch": f"codex/{task_id}-executed-test-binding",
            "upstream": "origin/master",
            "base_sha": "c" * 40,
            "head_sha": HEAD,
            "worktree_path": f"/tmp/{task_id.lower()}-executed-test-binding",
            "storage_mode": "worktree",
            "status": "active",
            "dirty_status": "clean",
            "conflict_marker_count": 0,
            "policy_profile": "code_strict",
        },
        require_work_session=True,
        session_policy_profile="code_strict",
        actor="adapter49-test",
        project=PROJECT,
    )
    assert claimed.get("claimed") is True, claimed
    work_session_id = claimed["work_session_id"]
    payload = json.dumps({
        "task_id": task_id,
        "work_session_id": work_session_id,
        "commands": ["python tests/test_adapter49_executed_test_actor_binding.py"],
        "passed": True,
        "exit_code": 0,
        "output_sha256": OUTPUT_SHA256,
    })

    principal = {
        "id": "env-mcp-token",
        "actor": "env-mcp-token",
        "kind": "environment",
    }

    def resolve_write_actor(principal_data, **kwargs):
        return store.resolve_write_actor(
            principal_data["actor"],
            principal_id=principal_data["id"],
            principal_kind=principal_data["kind"],
            **kwargs,
        )

    original_services = claim_tools._SERVICES
    claim_tools._SERVICES = claim_tools.ClaimToolServices(
        dumps=json.dumps,
        require_write=lambda *_args, **_kwargs: principal,
        resolve_write_actor=resolve_write_actor,
        write_binding_comment=lambda *_args, **_kwargs: None,
        ensure_pr_ready=lambda *_args, **_kwargs: {},
    )
    try:
        omitted = decode(claim_tools.record_executed_test_run(
            payload, None, project=PROJECT,
        ))
        assert omitted["error"] == "shared_token_requires_bound_actor", omitted

        unknown = decode(claim_tools.record_executed_test_run(
            payload, None, project=PROJECT, agent_id="codex/not-registered",
        ))
        assert unknown["error"] == "agent_not_registered", unknown

        recorded = decode(claim_tools.record_executed_test_run(
            payload, None, project=PROJECT, agent_id=AGENT,
        ))
        assert recorded.get("recorded") is True, recorded
        assert recorded["executed_test_gate"]["ok"] is True, recorded
    finally:
        claim_tools._SERVICES = original_services

    session = store.get_work_session(work_session_id, project=PROJECT) or {}
    hygiene_run = (session.get("hygiene") or {}).get("executed_test_run") or {}
    task_state = store.get_task(task_id, project=PROJECT) or {}
    claim_run = (
        ((task_state.get("git_state") or {}).get("evidence") or {})
        .get("executed_test_run") or {}
    )
    assert hygiene_run == claim_run
    assert hygiene_run["output_sha256"] == OUTPUT_SHA256
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print("PASS: omitted and inactive shared-token identities fail closed")
print("PASS: a live agent_id binds the typed receipt onto both evidence surfaces")
