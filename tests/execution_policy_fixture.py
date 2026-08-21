"""Shared ready project-execution policy for Connect integration tests."""

from __future__ import annotations

import copy

import store

from switchboard.application.commands.execution_context import with_generation
from switchboard.storage.repositories.provider_credentials import (
    default_provider_credential_repository,
)
from switchboard.storage.repositories.scm_connections import (
    default_scm_connection_repository,
)


READY_EXECUTION_POLICY = {
    "runtimes": {
        "allowed": ["claude_code", "codex", "cursor"],
        "default": "codex",
    },
    "workspace": {"repo_role": "canonical", "isolation": "worktree"},
    "placement": {
        "host_classes": ["personal", "ephemeral"],
        "trust_zones": ["personal", "cloud_ephemeral"],
        "burst": {"enabled": True, "max_concurrent_ephemeral": 4},
    },
    "providers": {
        "selectors": [
            {
                "provider": "openai-codex",
                "connection_reference": "provider-test",
                "account_affinity_id": "test-affinity",
                "priority": 0,
            },
            {
                "provider": "anthropic-claude",
                "connection_reference": "provider-test",
                "account_affinity_id": "test-affinity",
                "priority": 1,
            },
            {
                "provider": "cursor",
                "connection_reference": "provider-test",
                "account_affinity_id": "test-affinity",
                "priority": 2,
            },
        ],
    },
    "scm": {"provider": "github_app", "connection_reference": "scm-test"},
    "autopilot": {"enabled": True, "profile_id": "test"},
    "lifecycle": {"status": "active"},
}


def ready_execution_context(
    task_id: str, *, project: str = "switchboard", runtime: str = "codex",
) -> dict:
    provider = {
        "codex": "openai-codex",
        "claude-code": "anthropic-claude",
        "cursor": "cursor",
        "opencode": "opencode-zen",
    }[runtime]
    context = {
        "schema": "switchboard.execution_context.v1",
        "project_id": project,
        "task_id": task_id,
        "repo_role": "canonical",
        "repository": "6th-Element-Labs/projectplanner",
        "default_branch": "master",
        "base_sha": "a" * 40,
        "workspace": {"isolation": "worktree", "repo_role": "canonical"},
        "runtime": {"requested": runtime, "registry_name": runtime},
        "provider": {
            "provider": provider,
            "connection_reference": "provider-test",
            "account_affinity_id": "test-affinity",
            "connection_kind": "personal_plan",
            "credential_version": 1,
            "lifecycle_state": "active",
            "revocation_state": "",
        },
        "scm": {
            "provider": "github_app",
            "connection_reference": "scm-test",
        },
        "placement": READY_EXECUTION_POLICY["placement"],
        "authority_digest": "sha256:test-authority",
        "generation": 0,
    }
    # Hosts refuse a context whose fields disagree with its own digest, so the
    # fixture must sign itself the same way the server does.
    return with_generation(context, 0)


def ready_host_placement(project: str = "switchboard") -> dict:
    return {
        "schema": "switchboard.agent_host_placement.v1",
        "host_class": "persistent",
        "cost_class": "already_paid",
        "wakeable": True,
        "drain_state": "accepting",
        "projects": [project],
        "trust_zone": "personal",
        "providers": ["openai-codex"],
        "account_affinity_ids": ["test-affinity"],
        "repositories": ["6th-Element-Labs/projectplanner"],
        "supports_scm_materialization": True,
        "scm_providers": ["github_app"],
        "isolation_modes": ["task_worktree"],
        "workspace_backends": ["worktree"],
        "runtime_binaries": ["codex", "git"],
        "resources": {},
    }


def install_ready_execution_policy(project: str) -> None:
    topology = store.get_project_repo_topology(project)
    canonical = str(
        (((topology.get("roles") or {}).get("canonical") or {}).get("repo")) or ""
    )
    if not canonical:
        canonical = f"example/{project}"
        store.set_project_repo_topology(
            project=project,
            canonical_repo=canonical,
            canonical_default_branch="main",
        )
    organization = canonical.split("/", 1)[0]
    user_id = f"{project}-execution-policy-fixture"

    store.ensure_org(
        store.DEFAULT_ORG_ID,
        "Execution policy fixtures",
        created_by="test-fixture",
    )
    store.set_project_access(
        project,
        store.DEFAULT_ORG_ID,
        purpose="Hermetic execution policy fixture",
        created_by="test-fixture",
    )
    store.ensure_user(
        user_id,
        f"{user_id}@example.test",
        "Execution policy fixture",
        created_by="test-fixture",
    )
    store.add_org_member(
        store.DEFAULT_ORG_ID,
        user_id,
        role="member",
        created_by="test-fixture",
    )

    provider_references = {}
    for provider in ("openai-codex", "anthropic-claude", "cursor"):
        connection = default_provider_credential_repository.enroll(
            project=project,
            user_id=user_id,
            provider=provider,
            provider_account_id=f"{project}-{provider}-fixture",
            auth_type="personal_subscription",
            project_allowlist=[project],
            actor="test-fixture",
            refresh_state="ready",
            materialization_mode="host_native",
        )
        connection = default_provider_credential_repository.verify_host_native(
            connection["credential_reference"],
            project=project,
            actor="test-fixture",
            principal_user_id=user_id,
        )
        provider_references[provider] = connection["credential_reference"]

    scm = default_scm_connection_repository.create(
        {
            "provider": "github_app",
            "installation_ref": f"github-app:{project}-fixture",
            "org_allowlist": [organization],
            "project_allowlist": [project],
            "repository_allowlist": [canonical],
            "operation_scopes": ["clone", "fetch", "push", "create_pr"],
            "project": project,
        },
        actor="test-fixture",
    )

    policy = copy.deepcopy(READY_EXECUTION_POLICY)
    for selector in policy["providers"]["selectors"]:
        selector["connection_reference"] = provider_references[selector["provider"]]
    policy["scm"]["connection_reference"] = scm["connection_id"]
    store.set_meta("canonical_main_sha", "a" * 40, project=project)
    result = store.set_project_execution_policy(
        project=project,
        updates=policy,
        actor="test-fixture",
    )
    assert not result.get("error"), result
