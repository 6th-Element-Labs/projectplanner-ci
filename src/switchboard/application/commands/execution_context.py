"""Resolve the immutable, secret-free authority for one task execution.

The context is persisted with the wake.  Hosts receive references and digests,
never provider or SCM credentials.  ``authority_digest`` deliberately excludes
the execution generation so it can be re-resolved at claim time to fence policy,
topology, connection, or canonical-base changes.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Mapping


SCHEMA = "switchboard.execution_context.v1"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_RUNTIME_ALIASES = {
    "codex": "codex",
    "openai": "codex",
    "claude": "claude_code",
    "claude-code": "claude_code",
    "claude_code": "claude_code",
    "anthropic": "claude_code",
    "cursor": "cursor",
}
_PROVIDER_ALIASES = {
    "openai": "openai-codex",
    "codex": "openai-codex",
    "chatgpt": "openai-codex",
    "openai-codex": "openai-codex",
    "anthropic": "anthropic-claude",
    "claude": "anthropic-claude",
    "claude-code": "anthropic-claude",
    "anthropic-claude": "anthropic-claude",
    "cursor": "cursor",
}


class ExecutionContextError(ValueError):
    """A stable, secret-free refusal raised before a wake can launch."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "reason": self.message,
            "failure_class": "failed_gate",
            **self.details,
        }


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _provider_metadata(reference: str, project: str) -> dict[str, Any]:
    from switchboard.storage.repositories.provider_credentials import (
        CredentialVaultError,
        ProviderCredentialRepository,
    )
    try:
        value = ProviderCredentialRepository().get_metadata(
            reference, project=project, admin=True)
    except CredentialVaultError as exc:
        raise ExecutionContextError(
            "provider_connection_not_ready", exc.message,
            connection_reference=reference) from exc
    return dict(value)


def _scm_metadata(reference: str) -> dict[str, Any]:
    from switchboard.storage.repositories.scm_connections import (
        SCMConnectionError,
        SCMConnectionRepository,
    )
    try:
        return dict(SCMConnectionRepository().get(reference))
    except SCMConnectionError as exc:
        raise ExecutionContextError(
            "scm_connection_not_ready", exc.message,
            connection_reference=reference) from exc


def _canonical_base_sha(project: str) -> str:
    import store
    return str(store.get_meta("canonical_main_sha", "", project=project) or "")


def _safe_provider(value: Mapping[str, Any], reference: str,
                   expected_provider: str, project: str) -> dict[str, Any]:
    provider = _PROVIDER_ALIASES.get(
        str(value.get("provider") or "").strip().lower(), "")
    expected_provider = _PROVIDER_ALIASES.get(
        str(expected_provider or "").strip().lower(), "")
    if not provider or not expected_provider:
        raise ExecutionContextError(
            "provider_connection_not_ready",
            "provider connection names an unsupported provider",
            connection_reference=reference)
    allowlist = [str(item).lower() for item in value.get("project_allowlist") or []]
    state = str(value.get("lifecycle_state") or "").strip().lower()
    if provider != expected_provider or project.lower() not in allowlist or state != "active":
        raise ExecutionContextError(
            "provider_connection_not_ready",
            "provider connection does not match the active project policy",
            connection_reference=reference)
    return {
        "provider": provider,
        "connection_reference": reference,
        "connection_kind": str(value.get("connection_kind") or ""),
        "credential_version": int(value.get("credential_version") or 0),
        "lifecycle_state": state,
        "revocation_state": str(value.get("revocation_state") or ""),
    }


def _safe_scm(value: Mapping[str, Any], reference: str, provider: str,
              project: str, repository: str) -> dict[str, Any]:
    projects = [str(item).lower() for item in value.get("project_allowlist") or []]
    repositories = [
        str(item).lower() for item in value.get("repository_allowlist") or []]
    scopes = [str(item).lower() for item in value.get("operation_scopes") or []]
    actual_provider = str(value.get("provider") or "").strip().lower()
    configured_provider = str(provider or "").strip().lower()
    provider_matches = (
        actual_provider == configured_provider
        or {actual_provider, configured_provider} == {"github", "github_app"}
    )
    if (not provider_matches or project.lower() not in projects
            or repository.lower() not in repositories
            or "clone" not in scopes
            or str(value.get("lifecycle_state") or "").lower() != "active"):
        raise ExecutionContextError(
            "scm_connection_not_ready",
            "SCM connection does not authorize this project and canonical repository",
            connection_reference=reference)
    return {
        "provider": actual_provider,
        "connection_reference": reference,
        "installation_version": int(value.get("installation_version") or 0),
        "lifecycle_state": "active",
        "operation_scopes": sorted(scopes),
    }


def resolve(
    *, project: str, task_id: str, runtime: str, generation: int = 0,
    topology_provider: Callable[[str], Mapping[str, Any]] | None = None,
    policy_provider: Callable[[str], Mapping[str, Any]] | None = None,
    provider_metadata: Callable[[str, str], Mapping[str, Any]] | None = None,
    scm_metadata: Callable[[str], Mapping[str, Any]] | None = None,
    base_sha_provider: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Resolve and validate one exact execution authority snapshot."""
    import store
    topology = dict((topology_provider or store.get_project_repo_topology)(project) or {})
    policy = dict((policy_provider or store.get_project_execution_policy)(project) or {})
    readiness = dict(policy.get("readiness") or {})
    if not topology.get("valid"):
        raise ExecutionContextError(
            "execution_topology_not_ready",
            "project canonical repository topology is not ready")
    if readiness.get("passed") is not True or policy.get("valid") is not True:
        raise ExecutionContextError(
            str(readiness.get("reason_code") or "project_execution_policy_not_ready"),
            str(readiness.get("message") or
                "project execution policy is not ready"))

    runtime_name = _RUNTIME_ALIASES.get(str(runtime or "").strip().lower(), "")
    allowed = {
        _RUNTIME_ALIASES.get(str(item).strip().lower(), str(item).strip().lower())
        for item in (policy.get("runtimes") or {}).get("allowed") or []
    }
    if not runtime_name or runtime_name not in allowed:
        raise ExecutionContextError(
            "runtime_not_authorized",
            "runtime is not authorized by project execution policy",
            runtime=str(runtime or ""), allowed_runtimes=sorted(allowed))

    workspace = dict(policy.get("workspace") or {})
    repo_role = str(workspace.get("repo_role") or "").strip()
    role = dict(((topology.get("roles") or {}).get(repo_role)) or {})
    repository = str(role.get("repo") or "").strip()
    default_branch = str(role.get("default_branch") or "").strip()
    if not repository or not default_branch:
        raise ExecutionContextError(
            "execution_topology_not_ready",
            "execution repo role must name an exact repository and default branch",
            repo_role=repo_role)
    base_sha = str((base_sha_provider or _canonical_base_sha)(project) or "").lower()
    if not _SHA.fullmatch(base_sha):
        raise ExecutionContextError(
            "canonical_base_sha_missing",
            "an exact 40-character canonical default-branch SHA is required")

    selectors = sorted(
        (dict(item) for item in (policy.get("providers") or {}).get("selectors") or []),
        key=lambda item: int(item.get("priority") or 0))
    selected = next((
        item for item in selectors
        if str(item.get("provider") or "").strip().lower()
        in {str(runtime or "").strip().lower(),
            "openai" if runtime_name == "codex" else
            "anthropic" if runtime_name == "claude_code" else runtime_name}
    ), selectors[0] if selectors else None)
    if not selected:
        raise ExecutionContextError(
            "provider_connection_not_ready",
            "project execution policy has no provider selector")
    provider_name = str(selected.get("provider") or "").strip().lower()
    provider_ref = str(selected.get("connection_reference") or "").strip()
    provider_value = dict((provider_metadata or _provider_metadata)(
        provider_ref, project) or {})
    safe_provider = _safe_provider(
        provider_value, provider_ref, provider_name, project)
    safe_provider["account_affinity_id"] = str(
        selected.get("account_affinity_id") or "")

    scm_policy = dict(policy.get("scm") or {})
    scm_ref = str(scm_policy.get("connection_reference") or "").strip()
    scm_value = dict((scm_metadata or _scm_metadata)(scm_ref) or {})
    safe_scm = _safe_scm(
        scm_value, scm_ref, str(scm_policy.get("provider") or ""),
        project, repository)

    authority = {
        "project_id": project,
        "task_id": str(task_id or "").strip().upper(),
        "repo_role": repo_role,
        "repository": repository,
        "default_branch": default_branch,
        "base_sha": base_sha,
        "workspace": {
            "isolation": str(workspace.get("isolation") or ""),
            "repo_role": repo_role,
        },
        "runtime": {
            "requested": str(runtime or ""),
            "registry_name": runtime_name,
        },
        "provider": safe_provider,
        "scm": safe_scm,
        "placement": dict(policy.get("placement") or {}),
        "topology_digest": _digest(topology),
        "policy_digest": _digest(policy),
    }
    authority_digest = _digest(authority)
    context = {
        "schema": SCHEMA,
        **authority,
        "generation": int(generation or 0),
        "authority_digest": authority_digest,
    }
    context["digest"] = _digest(context)
    return context


def with_generation(context: Mapping[str, Any], generation: int) -> dict[str, Any]:
    """Bind a resolved authority snapshot to the server-allocated generation."""
    result = dict(context)
    result["generation"] = int(generation)
    result.pop("digest", None)
    result["digest"] = _digest(result)
    return result


def require_current(context: Mapping[str, Any]) -> None:
    """Fence a wake whose authority changed after it was queued."""
    current = resolve(
        project=str(context.get("project_id") or ""),
        task_id=str(context.get("task_id") or ""),
        runtime=str((context.get("runtime") or {}).get("requested") or ""),
        generation=int(context.get("generation") or 0),
    )
    if current.get("authority_digest") != context.get("authority_digest"):
        raise ExecutionContextError(
            "stale_execution_context",
            "execution policy, topology, connection metadata, or canonical base changed",
            expected_digest=context.get("authority_digest"),
            current_digest=current.get("authority_digest"))
