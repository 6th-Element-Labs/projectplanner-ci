#!/usr/bin/env python3
"""COORD-47: immutable, secret-free execution authority."""
from __future__ import annotations

import json

from path_setup import ROOT  # noqa: F401
from switchboard.application.commands import execution_context


def topology(repo: str, branch: str):
    return {
        "schema": "switchboard.project_repo_topology.v1",
        "valid": True,
        "roles": {
            "canonical": {
                "configured": True,
                "repo": repo,
                "default_branch": branch,
            },
        },
    }


def policy(provider_ref: str, scm_ref: str):
    return {
        "schema": "switchboard.project_execution_policy.v1",
        "valid": True,
        "readiness": {"passed": True},
        "runtimes": {"allowed": ["codex"], "default": "codex"},
        "workspace": {"repo_role": "canonical", "isolation": "worktree"},
        "placement": {
            "host_classes": ["personal", "ephemeral"],
            "trust_zones": ["personal", "cloud_ephemeral"],
        },
        "providers": {"selectors": [{
            "provider": "openai",
            "connection_reference": provider_ref,
            "account_affinity_id": "affinity-a",
            "priority": 0,
        }]},
        "scm": {"provider": "github", "connection_reference": scm_ref},
        "lifecycle": {"revision": 7, "status": "active"},
    }


def provider(reference: str, project: str):
    return {
        "provider": "openai-codex",
        "credential_reference": reference,
        "connection_kind": "api",
        "credential_version": 3,
        "lifecycle_state": "active",
        "revocation_state": "clear",
        "project_allowlist": [project],
        # This must never survive the resolver's allowlist.
        "encrypted_credential": "ciphertext-not-for-the-wake",
    }


def scm(reference: str, *, repo: str, project: str):
    return {
        "provider": "github_app",
        "connection_id": reference,
        "installation_ref": "github-app-installation:opaque-secret-adjacent",
        "installation_version": 4,
        "lifecycle_state": "active",
        "project_allowlist": [project],
        "repository_allowlist": [repo],
        "operation_scopes": ["clone", "fetch", "push", "create_pr"],
    }


def resolve(project: str, repo: str, branch: str, sha: str):
    provider_ref = f"provider-{project}"
    scm_ref = f"scm-{project}"
    return execution_context.resolve(
        project=project,
        task_id="COORD-47",
        runtime="codex",
        topology_provider=lambda _project: topology(repo, branch),
        policy_provider=lambda _project: policy(provider_ref, scm_ref),
        provider_metadata=provider,
        scm_metadata=lambda reference: scm(
            reference, repo=repo, project=project),
        base_sha_provider=lambda _project: sha,
    )


def test_context_is_idempotent_exact_and_secret_free():
    sha = "a" * 40
    first = resolve("switchboard", "6th-Element-Labs/projectplanner", "master", sha)
    second = resolve("switchboard", "6th-Element-Labs/projectplanner", "master", sha)
    assert first == second
    assert first["schema"] == execution_context.SCHEMA
    assert first["repository"] == "6th-Element-Labs/projectplanner"
    assert first["default_branch"] == "master"
    assert first["base_sha"] == sha
    assert first["runtime"]["registry_name"] == "codex"
    serialized = json.dumps(first, sort_keys=True)
    assert "ciphertext-not-for-the-wake" not in serialized
    assert "opaque-secret-adjacent" not in serialized
    assert first["provider"]["connection_reference"] == "provider-switchboard"
    assert first["scm"]["connection_reference"] == "scm-switchboard"


def test_generation_changes_context_digest_not_authority_digest():
    context = resolve(
        "switchboard", "6th-Element-Labs/projectplanner", "master", "b" * 40)
    generation_one = execution_context.with_generation(context, 1)
    generation_two = execution_context.with_generation(context, 2)
    assert generation_one["generation"] == 1
    assert generation_two["generation"] == 2
    assert generation_one["digest"] != generation_two["digest"]
    assert generation_one["authority_digest"] == generation_two["authority_digest"]


def test_atlas_and_switchboard_authority_cannot_leak():
    atlas = resolve("atlas", "6th-Element-Labs/ActionEngine", "main", "c" * 40)
    switchboard = resolve(
        "switchboard", "6th-Element-Labs/projectplanner", "master", "d" * 40)
    assert (atlas["repository"], atlas["default_branch"], atlas["base_sha"]) == (
        "6th-Element-Labs/ActionEngine", "main", "c" * 40)
    assert (
        switchboard["repository"],
        switchboard["default_branch"],
        switchboard["base_sha"],
    ) == ("6th-Element-Labs/projectplanner", "master", "d" * 40)
    assert atlas["authority_digest"] != switchboard["authority_digest"]


def test_missing_policy_and_base_sha_fail_closed():
    try:
        execution_context.resolve(
            project="switchboard", task_id="COORD-47", runtime="codex",
            topology_provider=lambda _project: topology("org/repo", "main"),
            policy_provider=lambda _project: {
                "valid": False,
                "readiness": {
                    "passed": False,
                    "reason_code": "project_execution_policy_missing",
                    "message": "missing",
                },
            },
        )
    except execution_context.ExecutionContextError as exc:
        assert exc.code == "project_execution_policy_missing"
    else:
        raise AssertionError("missing execution policy was accepted")

    try:
        resolve("switchboard", "org/repo", "main", "")
    except execution_context.ExecutionContextError as exc:
        assert exc.code == "canonical_base_sha_missing"
    else:
        raise AssertionError("missing canonical base SHA was accepted")


def test_stale_authority_is_fenced(monkeypatch):
    context = resolve("switchboard", "org/repo", "main", "e" * 40)
    changed = {**context, "authority_digest": "sha256:changed"}
    monkeypatch.setattr(execution_context, "resolve", lambda **_kwargs: changed)
    try:
        execution_context.require_current(context)
    except execution_context.ExecutionContextError as exc:
        assert exc.code == "stale_execution_context"
    else:
        raise AssertionError("stale execution context was accepted")
