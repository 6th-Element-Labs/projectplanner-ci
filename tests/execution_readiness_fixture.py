"""Hermetic execution-readiness setup for tests that exercise Start."""
from __future__ import annotations

import store
from switchboard.storage.repositories.provider_credentials import (
    default_provider_credential_repository,
)
from switchboard.storage.repositories.scm_connections import (
    default_scm_connection_repository,
)


def configure_ready_project(project: str, *, actor: str = "test") -> None:
    """Provision non-secret provider/SCM references and bounded burst capacity."""
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
        topology = store.get_project_repo_topology(project)
    assert canonical and "/" in canonical, topology
    organization = canonical.split("/", 1)[0]
    user_id = f"{project}-readiness-fixture"
    store.set_meta("canonical_main_sha", "a" * 40, project=project)

    store.ensure_org(store.DEFAULT_ORG_ID, "Execution readiness fixtures", created_by=actor)
    store.set_project_access(
        project,
        store.DEFAULT_ORG_ID,
        purpose="Hermetic execution readiness fixture",
        created_by=actor,
    )
    store.ensure_user(
        user_id,
        f"{user_id}@example.test",
        "Execution readiness fixture",
        created_by=actor,
    )
    store.add_org_member(
        store.DEFAULT_ORG_ID, user_id, role="member", created_by=actor
    )
    provider = default_provider_credential_repository.enroll(
        project=project,
        user_id=user_id,
        provider="codex",
        provider_account_id=f"{project}-fixture-account",
        auth_type="personal_subscription",
        project_allowlist=[project],
        actor=actor,
        refresh_state="ready",
        materialization_mode="host_native",
    )
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
        actor=actor,
    )
    result = store.set_project_execution_policy(
        project=project,
        updates={
            "runtimes": {
                "allowed": ["claude_code", "codex"],
                "default": "codex",
            },
            "workspace": {"repo_role": "canonical", "isolation": "worktree"},
            "placement": {
                "host_classes": ["ephemeral"],
                "trust_zones": ["cloud_ephemeral"],
                "burst": {"enabled": True, "max_concurrent_ephemeral": 2},
            },
            "providers": {
                "selectors": [
                    {
                        "provider": "codex",
                        "connection_reference": provider["credential_reference"],
                    }
                ]
            },
            "scm": {
                "provider": "github",
                "connection_reference": scm["connection_id"],
            },
            "autopilot": {"enabled": False, "profile_id": ""},
            "lifecycle": {"status": "active"},
        },
        actor=actor,
    )
    assert not result.get("error"), result
