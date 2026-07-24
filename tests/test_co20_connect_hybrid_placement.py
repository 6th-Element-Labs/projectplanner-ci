#!/usr/bin/env python3
"""CO-20: task-bound Connect generations require project-derived placement."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401
from switchboard.application.commands import connect_dispatch, execution_context
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
                "session_policies": ["code_strict"],
                "isolation_modes": ["task_worktree"],
                "workspace_backends": ["worktree"],
                "runtime_binaries": ["codex", "git"],
                "resources": {},
            },
        },
    }


def test_enqueue_requires_execution_context_and_persists_hybrid_policy():
    captured: list[dict] = []
    saved_resolve = execution_context.resolve
    saved_request = connect_dispatch.coordination_repo.request_wake
    saved_capacity = connect_dispatch.capacity_readback
    try:
        execution_context.resolve = lambda **_kwargs: context()
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
    assert result["dispatched"] is True
    policy = captured[0]["policy"]
    assert policy["scheduler"]["mode"] == "hybrid"
    assert policy["execution_context"]["authority_digest"] == "sha256:authority"
    assert policy["placement"] == {
        "canonical_repo": "6th-Element-Labs/projectplanner",
        "repo_role": "canonical",
        "host_classes": ["ephemeral", "persistent"],
        "trust_zones": ["cloud_ephemeral", "personal"],
        "session_policy": "code_strict",
        "isolation": "task_worktree",
        "workspace_backend": "worktree",
        "runtime_binaries": ["codex", "git"],
        "provider": "openai-codex",
        "account_affinity_id": "affinity-a",
        "scm_provider": "github_app",
    }


def test_missing_execution_policy_never_creates_legacy_wake():
    calls: list[dict] = []
    saved_resolve = execution_context.resolve
    saved_request = connect_dispatch.coordination_repo.request_wake
    try:
        def refuse(**_kwargs):
            raise execution_context.ExecutionContextError(
                "project_execution_policy_missing", "missing")
        execution_context.resolve = refuse
        connect_dispatch.coordination_repo.request_wake = (
            lambda **kwargs: calls.append(kwargs) or {})
        result = connect_dispatch.enqueue_task(
            {"task_id": "CO-20", "_wsId": "CO"},
            project="switchboard",
            actor="co20-test",
        )
    finally:
        execution_context.resolve = saved_resolve
        connect_dispatch.coordination_repo.request_wake = saved_request
    assert result["dispatched"] is False
    assert result["error"] == "project_execution_policy_missing"
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
    test_enqueue_requires_execution_context_and_persists_hybrid_policy()
    test_missing_execution_policy_never_creates_legacy_wake()
    test_host_constraints_fail_closed_independently()
    test_remediation_claims_use_the_fenced_completion_handoff()
    print("CO-20 mandatory hybrid placement: 4 passed")
