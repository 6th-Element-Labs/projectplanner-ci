"""Shared project-administration read model for REST, MCP, and operator UI."""
from __future__ import annotations

from collections import Counter
from typing import Any, Callable

from switchboard.storage.repositories.protocols.access import AccessRepository


PROJECT_ADMINISTRATION_SCHEMA = "switchboard.project_administration.v1"


def execute_for(
        project_id: str, *,
        access_repository: AccessRepository,
        repo_topology_provider: Callable[[str], dict[str, Any]],
        access_model_provider: Callable[[str, str], dict[str, Any]],
        principal_id: str = "",
        principal_scopes: list[str] | None = None,
        ) -> dict[str, Any]:
    """Return one access-controlled administration projection without board writes."""
    project = access_repository.get_project_record(project_id)
    if project.get("error"):
        return {"schema": PROJECT_ADMINISTRATION_SCHEMA, **project}

    try:
        topology = dict(repo_topology_provider(project_id) or {})
    except Exception as exc:  # noqa: BLE001 - reads fail visibly, never optimistically
        topology = {"error": "repo_topology_unavailable", "message": str(exc)}
    try:
        access_model = dict(access_model_provider(project_id, principal_id) or {})
    except Exception as exc:  # noqa: BLE001
        access_model = {"error": "project_access_unavailable", "message": str(exc)}

    grants = access_model.get("grants") or []
    role_counts = Counter(str(item.get("role") or "unknown") for item in grants)
    principal_roles = [
        str(item.get("role") or "") for item in access_model.get("principal_roles") or []
        if item.get("role")
    ]
    return {
        "schema": PROJECT_ADMINISTRATION_SCHEMA,
        "project": project,
        "repo_topology": topology,
        "access_summary": {
            "access": access_model.get("access") or {},
            "grant_count": len(grants),
            "role_counts": dict(sorted(role_counts.items())),
            "principal_roles": sorted(set(principal_roles)),
            "effective_scopes": sorted(set(principal_scopes or [])),
            "error": access_model.get("error"),
        },
        "lifecycle_events": access_repository.list_project_lifecycle_events(project_id),
    }
