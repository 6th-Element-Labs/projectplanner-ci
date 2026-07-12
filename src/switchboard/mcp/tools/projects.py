"""Access-controlled project lifecycle MCP read tools."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import store
from switchboard.application.queries import project_impact


@dataclass(frozen=True)
class ProjectToolServices:
    dumps: Callable[[Any], str]
    require_read: Callable[..., dict[str, Any]]


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


PROJECT_TOOL_NAMES = ("get_project_impact_report",)


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
