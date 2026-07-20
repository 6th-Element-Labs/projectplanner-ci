"""Access/identity administration MCP tools: scoped tokens, audit export, and
lifecycle cleanup (ARCH-MS-70).

Transport adapter extracted from ``mcp_server_impl``. Authentication and MCP
serialization remain edge concerns; persistence stays behind ``store``.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth
import store


@dataclass(frozen=True)
class AccessToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: AccessToolServices | None = None


def _services() -> AccessToolServices:
    if _SERVICES is None:
        raise RuntimeError("access MCP tools must be registered before use")
    return _SERVICES


def list_scoped_tokens(ctx: Context, project: str = "maxwell",
                       include_revoked: bool = False, kind: str = "") -> str:
    """List bearer principals for one project without exposing token hashes or raw tokens.

    Requires write:system on the target project. Use this to audit which humans, agents,
    hosts, or system actors can call Switchboard over REST/MCP.
    """
    services = _services()
    services.require_write(ctx, project, ("write:system",))
    return services.dumps({
        "project": project,
        "tokens": store.list_principals(project=project, include_revoked=include_revoked, kind=kind),
        "scope_definitions": store.principal_scope_definitions(),
        "valid_kinds": sorted(store.VALID_PRINCIPAL_KINDS),
    })


def get_audit_export(ctx: Context, project: str = "maxwell") -> str:
    """Return the redacted enterprise audit evidence bundle for one project.

    Requires write:system. The bundle includes task/activity history, claims, messages,
    runner/session/control evidence, Git/offline provenance, Tally economics, and access
    principal/role history without exposing token hashes or raw secrets.
    """
    from switchboard.application.queries.audit_export import execute
    services = _services()
    services.require_write(ctx, project, ("write:system",))
    return services.dumps(execute(project=project))


def list_cleanup_candidates(ctx: Context, project: str = "maxwell",
                            kinds: str = "", proof_task_age_days: float = 14) -> str:
    """List stale lifecycle cleanup candidates without changing board state.

    Requires write:system. Candidates cover stale agent presence, expired runner sessions,
    orphan/expired claims and leases, old active wakes, terminal wake history,
    fired/orphan monitors, and old terminal proof/sentinel tasks. Terminal wake cleanup
    archives in bounded batches without deleting provenance.
    """
    services = _services()
    services.require_write(ctx, project, ("write:system",))
    return services.dumps(store.cleanup_candidates(
        project=project,
        proof_task_age_days=proof_task_age_days,
        include_kinds=store.coerce_csv_list(kinds),
    ))


def apply_cleanup(ctx: Context, project: str = "maxwell", candidate_ids: str = "",
                  dry_run: bool = True, reason: str = "operator lifecycle cleanup",
                  kinds: str = "", proof_task_age_days: float = 14) -> str:
    """Dry-run or apply safe lifecycle cleanup with an audit trail.

    Pass comma/newline-separated candidate_ids to limit scope. With dry_run=false, each mutation
    writes cleanup activity and uses archive/resolve paths rather than raw deletion.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:system",))
    return services.dumps(store.apply_cleanup(
        project=project,
        candidate_ids=store.coerce_csv_list(candidate_ids),
        dry_run=dry_run,
        actor=auth.actor(principal),
        reason=reason,
        proof_task_age_days=proof_task_age_days,
        include_kinds=store.coerce_csv_list(kinds),
    ))


def _scoped_token_auth_project(project: str) -> str:
    binding = (project or "maxwell").strip()
    if store.is_global_project_binding(binding):
        return "switchboard"
    return binding


def create_scoped_token(ctx: Context, project: str = "maxwell", kind: str = "agent",
                        display_name: str = "", scopes: str = "", role: str = "",
                        principal_id: str = "", allowed_projects: str = "",
                        purpose: str = "", expires_in_seconds: int = 0) -> str:
    """Create one project-scoped bearer token for REST/MCP callers.

    Requires write:system on the target project. Pass project='*' only for an operator token and
    provide an explicit ``allowed_projects`` set, ``purpose``, and positive
    ``expires_in_seconds``. Future boards remain denied. `role` is a preset such as viewer,
    contributor, operator, or admin;
    `scopes` can also be a comma/newline list. The raw token is returned once and is never stored,
    so capture it immediately.
    """
    services = _services()
    binding = (project or "maxwell").strip()
    auth_project = _scoped_token_auth_project(binding)
    if not store.is_global_project_binding(binding) and not store.has_project(binding):
        return services.dumps({"error": f"unknown project: {binding}"})
    principal = services.require_write(ctx, auth_project, ("write:system",))
    resolved = store.resolve_principal_scopes(scopes, role=role)
    if resolved.get("error"):
        return services.dumps(resolved)
    normalized_kind = store.validate_principal_kind(kind or "agent")
    if not normalized_kind:
        return services.dumps({"error": "kind must be one of: " + ", ".join(sorted(store.VALID_PRINCIPAL_KINDS))})
    requested_projects = store.coerce_csv_list(allowed_projects)
    if store.is_global_project_binding(binding):
        purpose = (purpose or "").strip()
        if not requested_projects:
            return services.dumps({"error": "allowed_projects required for operator token"})
        unknown = sorted(set(requested_projects) - set(store.project_ids()))
        if unknown:
            return services.dumps({"error": "unknown allowed project(s): " + ", ".join(unknown)})
        if not purpose:
            return services.dumps({"error": "purpose required for operator token"})
        try:
            expires_in_seconds = int(expires_in_seconds)
        except (TypeError, ValueError):
            expires_in_seconds = 0
        if expires_in_seconds <= 0:
            return services.dumps({"error": "positive expires_in_seconds required for operator token"})
    raw_token = auth.new_secret_token()
    created = store.create_principal(
        kind=normalized_kind,
        display_name=display_name or normalized_kind,
        token=raw_token,
        scopes=resolved["scopes"],
        principal_id=principal_id or None,
        project=binding,
    )
    if created.get("error"):
        return services.dumps(created)
    operator_grants = []
    if store.is_global_project_binding(binding):
        grant_role = resolved.get("role") or "custom"
        expires_at = time.time() + expires_in_seconds
        for project_id in sorted(set(requested_projects)):
            grant = store.grant_project_role(
                project_id, "principal", created["id"], grant_role,
                created_by=auth.actor(principal), scopes=resolved["scopes"],
                purpose=purpose, expires_at=expires_at)
            if grant.get("error"):
                store.revoke_principal_token(
                    created["id"], project=auth_project, actor=auth.actor(principal))
                return services.dumps({
                    "error": "operator_grant_failed",
                    "project": project_id,
                    "detail": grant,
                })
            operator_grants.append({
                "project_id": grant.get("project_id"),
                "role": grant.get("role"),
                "scopes": grant.get("scopes") or [],
                "created_at": grant.get("created_at"),
                "created_by": grant.get("created_by"),
                "purpose": grant.get("purpose"),
                "expires_at": grant.get("expires_at"),
            })
    public = store.public_principal_record(created, project=auth_project)
    store.append_activity(
        "access.token_created",
        auth.actor(principal),
        {"principal": public, "role": resolved.get("role"),
         "operator_grants": operator_grants, "token_returned_once": True},
        task_id=None,
        project=auth_project,
    )
    return services.dumps({"project": binding, "principal": public, "token": raw_token,
                   "operator_grants": operator_grants, "token_returned_once": True})


def revoke_scoped_token(principal_id: str, ctx: Context, project: str = "maxwell") -> str:
    """Revoke one project-scoped bearer principal and any live sessions for that principal."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:system",))
    result = store.revoke_principal_token(principal_id, project=project, actor=auth.actor(principal))
    return services.dumps(result)


ACCESS_TOOL_NAMES = (
    "list_scoped_tokens", "get_audit_export", "list_cleanup_candidates",
    "apply_cleanup", "create_scoped_token", "revoke_scoped_token",
)


def register_access_tools(mcp: Any, services: AccessToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the access-administration tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in ACCESS_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
