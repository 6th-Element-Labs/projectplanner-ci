"""Access-controlled project lifecycle MCP read tools."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import store
from switchboard.application.commands import (
    project_consolidation,
    project_lifecycle,
    project_metadata,
)
from switchboard.application.queries import project_admin, project_impact


@dataclass(frozen=True)
class ProjectToolServices:
    dumps: Callable[[Any], str]
    require_read: Callable[..., dict[str, Any]]
    require_write: Callable[..., dict[str, Any]]
    principal_actor: Callable[[dict[str, Any]], str]


_SERVICES: ProjectToolServices | None = None


def _services() -> ProjectToolServices:
    if _SERVICES is None:
        raise RuntimeError("project MCP tools must be registered before use")
    return _SERVICES


def get_project_impact_report(ctx: Context, project: str = "maxwell",
                              limit: int = 50) -> str:
    """Read-only project dependency/sprawl impact audit.

    Returns the versioned ``switchboard.project_impact_report.v1`` contract with
    bounded deterministic samples, archive blockers, and a keep/consolidate/archive
    recommendation. Requires read access to the selected project.
    """
    services = _services()
    services.require_read(ctx, project, ("read",))
    result = project_impact.execute_for(
        project,
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
        limit=limit,
    )
    return services.dumps(result)


def get_project(ctx: Context, project: str = "maxwell") -> str:
    """Return the shared project administration record, access summary, and receipts."""
    services = _services()
    principal = services.require_read(ctx, project, ("read",))
    result = project_admin.execute_for(
        project,
        access_repository=store.access_repository,
        repo_topology_provider=store.get_project_repo_topology,
        access_model_provider=store.project_access_model,
        principal_id=str(principal.get("id") or ""),
        principal_scopes=list(
            principal.get("effective_scopes") or principal.get("scopes") or []),
    )
    return services.dumps(result)


def update_project(ctx: Context, project: str, metadata_json: str) -> str:
    """Update safe ordinary metadata; lifecycle and ownership fields are rejected."""
    services = _services()
    if not str(project or "").strip():
        return services.dumps({"error": "project is required"})
    try:
        metadata = json.loads(metadata_json or "")
    except (TypeError, json.JSONDecodeError):
        return services.dumps({"error": "metadata_json must be valid JSON"})
    if not isinstance(metadata, dict):
        return services.dumps({"error": "metadata_json must decode to an object"})
    required = (("write:system",)
                if {"boundary", "visibility"}.intersection(metadata)
                else ("write:projects",))
    principal = services.require_write(ctx, project, required)
    result = project_metadata.execute(
        {**metadata, "project_id": project},
        actor=services.principal_actor(principal),
        access_repository=store.access_repository,
    )
    return services.dumps(result)


def archive_project(ctx: Context, project: str, reason: str,
                    impact_report_receipt_json: str) -> str:
    """Archive a project against an exact current impact-report receipt."""
    services = _services()
    if not str(project or "").strip():
        return services.dumps({"error": "project is required"})
    principal = services.require_write(ctx, project, ("write:system",))
    try:
        receipt = json.loads(impact_report_receipt_json or "")
    except (TypeError, json.JSONDecodeError):
        return services.dumps({"error": "impact_report_receipt_json must be valid JSON"})
    result = project_lifecycle.archive_project(
        {"project_id": project, "reason": reason,
         "impact_report_receipt": receipt,
         "actor": services.principal_actor(principal)},
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )
    return services.dumps(result)


def restore_project(ctx: Context, project: str, reason: str) -> str:
    """Restore archived project writes after access and topology validation."""
    services = _services()
    if not str(project or "").strip():
        return services.dumps({"error": "project is required"})
    principal = services.require_write(ctx, project, ("write:system",))
    result = project_lifecycle.restore_project(
        {"project_id": project, "reason": reason,
         "actor": services.principal_actor(principal)},
        access_repository=store.access_repository,
        repo_topology_provider=store.get_project_repo_topology,
    )
    return services.dumps(result)


def plan_project_consolidation(ctx: Context, project: str,
                               replacement_project: str, reason: str,
                               approval_json: str,
                               replacement_board: str = "",
                               replacement_mission: str = "",
                               replacement_deliverable: str = "",
                               safe_routing_keys_json: str = "[]") -> str:
    """Dry-run an operator-approved project consolidation; no state is mutated."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:system",))
    try:
        approval = json.loads(approval_json or "")
        routing_keys = json.loads(safe_routing_keys_json or "[]")
    except (TypeError, json.JSONDecodeError):
        return services.dumps({"error": "approval and routing keys must be valid JSON"})
    result = project_consolidation.plan_project_consolidation(
        {
            "source_project_id": project,
            "replacement_project_id": replacement_project,
            "replacement_board_id": replacement_board or None,
            "replacement_mission_id": replacement_mission or None,
            "replacement_deliverable_id": replacement_deliverable or None,
            "safe_routing_keys": routing_keys,
            "reason": reason,
            "actor": services.principal_actor(principal),
            "approval": approval,
        },
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )
    return services.dumps(result)


def apply_project_consolidation(ctx: Context, project: str,
                                plan_json: str, confirmation: str) -> str:
    """Apply an exact current consolidation plan and immediately verify it."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:system",))
    try:
        plan = json.loads(plan_json or "")
    except (TypeError, json.JSONDecodeError):
        return services.dumps({"error": "plan_json must be valid JSON"})
    if not isinstance(plan, dict) or plan.get("source_project_id") != project:
        return services.dumps({"error": "plan source does not match project"})
    result = project_consolidation.apply_project_consolidation(
        {"plan": plan, "confirmation": confirmation,
         "actor": services.principal_actor(principal)},
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )
    return services.dumps(result)


def verify_project_consolidation(ctx: Context, project: str,
                                 consolidation_id: str) -> str:
    """Verify archived source history, pointers, routes, and cross-project graph reads."""
    services = _services()
    services.require_read(ctx, project, ("read",))
    result = project_consolidation.verify_project_consolidation(
        project, consolidation_id,
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )
    return services.dumps(result)


def rollback_project_consolidation(ctx: Context, project: str,
                                   consolidation_id: str, reason: str) -> str:
    """Rollback a consolidation before purge and restore exact routing state."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:system",))
    result = project_consolidation.rollback_project_consolidation(
        {"source_project_id": project, "consolidation_id": consolidation_id,
         "reason": reason, "actor": services.principal_actor(principal)},
        access_repository=store.access_repository,
        project_configs=store._project_map(),
    )
    return services.dumps(result)


PROJECT_TOOL_NAMES = (
    "get_project", "update_project", "get_project_impact_report",
    "archive_project", "restore_project",
    "plan_project_consolidation", "apply_project_consolidation",
    "verify_project_consolidation", "rollback_project_consolidation",
)


def register_project_tools(mcp: Any,
                           services: ProjectToolServices) -> dict[str, Callable[..., str]]:
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in PROJECT_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
