#!/usr/bin/env python3
"""CO-20/BUG-345: policy-free launch keeps placement utilities isolated."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401
from switchboard.application.commands import connect_dispatch, execution_context
from switchboard.storage.repositories import project_execution_policy
from switchboard.storage.repositories import projects as projects_repo
from switchboard.domain.coordination.placement import (
    HOST_PLACEMENT_SCHEMA,
    evaluate_host,
)


def context() -> dict:
    return {
        "schema": execution_context.SCHEMA,
        "project_id": "switchboard",
        "task_id": "CO-20",
        "repo_role": "canonical",
        "repository": "6th-Element-Labs/projectplanner",
        "default_branch": "master",
        "base_sha": "a" * 40,
        "workspace": {"isolation": "worktree", "repo_role": "canonical"},
        "runtime": {"requested": "codex", "registry_name": "codex"},
        "provider": {
            "provider": "openai-codex",
            "connection_reference": "provider-ref",
            "account_affinity_id": "affinity-a",
        },
        "scm": {
            "provider": "github_app",
            "connection_reference": "scm-ref",
        },
        "placement": {
            "host_classes": ["personal", "ephemeral"],
            "trust_zones": ["personal", "cloud_ephemeral"],
            "burst": {"enabled": True, "max_concurrent_ephemeral": 2},
        },
        "authority_digest": "sha256:authority",
        "generation": 0,
        "digest": "sha256:context",
    }


def host() -> dict:
    return {
        "host_id": "host/persistent",
        "status": "online",
        "runtimes": [{
            "runtime": "codex",
            "lanes": ["CO"],
            "capabilities": ["execution_lease_v2", "runner_lease_enforcement"],
            "policy": {"allow_work": True},
        }],
        "limits": {"max_sessions": 1},
        "capacity": {
            "active_sessions": 0,
            "placement": {
                "schema": HOST_PLACEMENT_SCHEMA,
                "host_class": "persistent",
                "cost_class": "already_paid",
                "wakeable": True,
                "drain_state": "accepting",
                "projects": ["switchboard"],
                "trust_zone": "personal",
                "providers": ["openai-codex"],
                "account_affinity_ids": ["affinity-a"],
                "repositories": ["6th-Element-Labs/projectplanner"],
                "supports_scm_materialization": True,
                "scm_providers": ["github_app"],
                "isolation_modes": ["task_worktree"],
                "workspace_backends": ["worktree"],
                "runtime_binaries": ["codex", "git"],
                "resources": {},
            },
        },
    }


# A saved policy may still be inspected in Settings, but Connect launch ignores it.
project_execution_policy.get_project_execution_policy = (
    lambda _project: {"configured": True, "activated": True})


def test_enqueue_ignores_execution_context_and_uses_canonical_binding():
    captured: list[dict] = []
    saved_resolve = execution_context.resolve
    saved_request = connect_dispatch.coordination_repo.request_wake
    saved_capacity = connect_dispatch.capacity_readback
    saved_topology = projects_repo.get_project_repo_topology
    try:
        execution_context.resolve = lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("launch must not resolve execution policy"))
        projects_repo.get_project_repo_topology = lambda _project: {
            "roles": {"canonical": {
                "configured": True,
                "repo": "6th-Element-Labs/projectplanner",
                "default_branch": "master",
            }}
        }
        connect_dispatch.coordination_repo.request_wake = (
            lambda **kwargs: captured.append(kwargs)
            or {"wake_id": "wake-co20", "status": "pending"})
        connect_dispatch.capacity_readback = lambda *_args, **_kwargs: {}
        result = connect_dispatch.enqueue_task(
            {
                "task_id": "CO-20",
                "_wsId": "CO",
                "description": "policy_profile:code_strict",
                "updated_at": 1,
            },
            project="switchboard",
            actor="co20-test",
            runtime="codex",
        )
    finally:
        execution_context.resolve = saved_resolve
        connect_dispatch.coordination_repo.request_wake = saved_request
        connect_dispatch.capacity_readback = saved_capacity
        projects_repo.get_project_repo_topology = saved_topology
    assert result["dispatched"] is True
    policy = captured[0]["policy"]
    assert policy["repository_binding"] == {
        "schema": "switchboard.repository_binding.v1",
        "project": "switchboard",
        "repo_role": "canonical",
        "repository": "6th-Element-Labs/projectplanner",
        "default_branch": "master",
    }
    assert "scheduler" not in policy
    assert "placement" not in policy
    assert "execution_context" not in policy


def test_missing_canonical_repository_never_creates_wake():
    calls: list[dict] = []
    saved_request = connect_dispatch.coordination_repo.request_wake
    saved_topology = projects_repo.get_project_repo_topology
    try:
        projects_repo.get_project_repo_topology = lambda _project: {
            "roles": {"canonical": {"configured": False}}
        }
        connect_dispatch.coordination_repo.request_wake = (
            lambda **kwargs: calls.append(kwargs) or {})
        result = connect_dispatch.enqueue_task(
            {"task_id": "CO-20", "_wsId": "CO"},
            project="switchboard",
            actor="co20-test",
        )
    finally:
        connect_dispatch.coordination_repo.request_wake = saved_request
        projects_repo.get_project_repo_topology = saved_topology
    assert result["dispatched"] is False
    assert result["error"] == "canonical_repository_unconfigured"
    assert calls == []


def test_host_constraints_fail_closed_independently():
    policy = connect_dispatch._hybrid_policy(
        context(),
        {"description": "policy_profile:code_strict"},
        "codex",
    )
    selector = {
        "runtime": "codex",
        "lane": "CO",
        "capabilities": ["execution_lease_v2", "runner_lease_enforcement"],
    }
    assert evaluate_host(
        host(), selector, policy, project="switchboard")["eligible"] is True
    mutations = {
        "wrong project": ("projects", ["atlas"], "project_not_allowed"),
        "wrong provider": ("providers", ["anthropic-claude"], "provider_not_allowed"),
        "wrong affinity": (
            "account_affinity_ids", ["affinity-b"],
            "provider_account_affinity_mismatch"),
        "wrong trust": ("trust_zone", "org_shared", "trust_zone_not_allowed"),
        "wrong workspace": (
            "workspace_backends", ["clone"], "workspace_backend_not_supported"),
        "draining": ("drain_state", "draining", "host_draining"),
    }
    for label, (field, value, reason) in mutations.items():
        candidate = host()
        candidate["capacity"]["placement"][field] = value
        result = evaluate_host(candidate, selector, policy, project="switchboard")
        assert result["eligible"] is False, label
        assert reason in result["reason_codes"], label


def test_remediation_claims_use_the_fenced_completion_handoff():
    source = (
        ROOT / "src" / "switchboard" / "storage" / "repositories" / "claims.py"
    ).read_text(encoding="utf-8")
    assert 'role in {"implementation", "remediation"}' in source


if __name__ == "__main__":
    test_enqueue_ignores_execution_context_and_uses_canonical_binding()
    test_missing_canonical_repository_never_creates_wake()
    test_host_constraints_fail_closed_independently()
    test_remediation_claims_use_the_fenced_completion_handoff()
    print("CO-20 policy-free launch and placement utility: 4 passed")
