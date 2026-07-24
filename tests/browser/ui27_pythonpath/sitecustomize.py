"""Hermetic execution-authority seam for the UI-27 app subprocess."""

from switchboard.application.commands import connect_dispatch


def _resolve(*, project: str, task_id: str, runtime: str, **_kwargs):
    provider = {
        "codex": "openai-codex",
        "claude-code": "anthropic-claude",
        "cursor": "cursor",
    }[runtime]
    return {
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
            "connection_reference": "provider-ui27",
            "account_affinity_id": "ui27-affinity",
        },
        "scm": {
            "provider": "github_app",
            "connection_reference": "scm-ui27",
        },
        "placement": {
            "host_classes": ["personal", "ephemeral"],
            "trust_zones": ["personal", "cloud_ephemeral"],
            "burst": {"enabled": True, "max_concurrent_ephemeral": 1},
        },
        "authority_digest": "sha256:ui27-authority",
        "generation": 0,
        "digest": "sha256:ui27-context",
    }


connect_dispatch.execution_context.resolve = _resolve
