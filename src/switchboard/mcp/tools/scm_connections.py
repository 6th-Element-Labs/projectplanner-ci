"""MCP administration and fail-closed preflight for SCM connections."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

from switchboard.storage.repositories.scm_connections import (
    SCMConnectionError,
    default_scm_connection_repository as repository,
)


@dataclass(frozen=True)
class SCMConnectionToolServices:
    dumps: Callable[[Any], str]
    require_read: Callable[..., dict[str, Any]]
    require_write: Callable[..., dict[str, Any]]
    principal_actor: Callable[[dict[str, Any]], str]


_SERVICES: SCMConnectionToolServices | None = None


def _services() -> SCMConnectionToolServices:
    if _SERVICES is None:
        raise RuntimeError("SCM connection MCP tools are not registered")
    return _SERVICES


def _object(value: str, field: str) -> dict[str, Any]:
    try:
        result = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} must be valid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{field} must decode to an object")
    return result


def _admin(principal: dict[str, Any]) -> None:
    scopes = set(principal.get("effective_scopes") or principal.get("scopes") or [])
    if "admin" not in scopes:
        raise SCMConnectionError(
            "scm_connection_admin_required",
            "SCM connection administration requires admin scope",
            status_code=403,
        )


def create_scm_connection(connection_json: str, ctx: Context,
                          project: str = "maxwell") -> str:
    services = _services()
    principal = services.require_write(ctx, project, ("write:credentials",))
    _admin(principal)
    payload = _object(connection_json, "connection_json")
    payload["project_allowlist"] = payload.get("project_allowlist") or [project]
    payload["project"] = project
    try:
        result = repository.create(payload, actor=services.principal_actor(principal))
    except SCMConnectionError as exc:
        result = exc.as_dict()
    return services.dumps(result)


def list_scm_connections(ctx: Context, project: str = "maxwell") -> str:
    services = _services()
    principal = services.require_read(ctx, project, ("read:credentials",))
    _admin(principal)
    return services.dumps({"connections": repository.list(project=project)})


def get_scm_connection(connection_id: str, ctx: Context,
                       project: str = "maxwell") -> str:
    services = _services()
    principal = services.require_read(ctx, project, ("read:credentials",))
    _admin(principal)
    try:
        result = repository.get(connection_id, include_events=True)
        if project.lower() not in result["project_allowlist"]:
            raise SCMConnectionError("repository_not_authorized",
                                     "SCM connection is not authorized for this project",
                                     status_code=403)
    except SCMConnectionError as exc:
        result = exc.as_dict()
    return services.dumps(result)


def update_scm_connection(connection_id: str, update_json: str, ctx: Context,
                          project: str = "maxwell") -> str:
    services = _services()
    principal = services.require_write(ctx, project, ("write:credentials",))
    _admin(principal)
    try:
        result = repository.update(
            connection_id, _object(update_json, "update_json"),
            actor=services.principal_actor(principal), project=project)
    except SCMConnectionError as exc:
        result = exc.as_dict()
    return services.dumps(result)


def rotate_scm_connection(connection_id: str, installation_ref: str, ctx: Context,
                          project: str = "maxwell") -> str:
    services = _services()
    principal = services.require_write(ctx, project, ("write:credentials",))
    _admin(principal)
    try:
        result = repository.rotate(
            connection_id, installation_ref, actor=services.principal_actor(principal),
            project=project)
    except SCMConnectionError as exc:
        result = exc.as_dict()
    return services.dumps(result)


def revoke_scm_connection(connection_id: str, reason: str, ctx: Context,
                          project: str = "maxwell") -> str:
    services = _services()
    principal = services.require_write(ctx, project, ("write:credentials",))
    _admin(principal)
    try:
        result = repository.revoke(
            connection_id, reason, actor=services.principal_actor(principal),
            project=project)
    except SCMConnectionError as exc:
        result = exc.as_dict()
    return services.dumps(result)


def delete_scm_connection(connection_id: str, reason: str, ctx: Context,
                          project: str = "maxwell") -> str:
    services = _services()
    principal = services.require_write(ctx, project, ("write:credentials",))
    _admin(principal)
    try:
        result = repository.delete(
            connection_id, reason, actor=services.principal_actor(principal),
            project=project)
    except SCMConnectionError as exc:
        result = exc.as_dict()
    return services.dumps(result)


def preflight_scm_repository(connection_id: str, repository_name: str,
                             operation: str, ctx: Context,
                             project: str = "maxwell") -> str:
    """Authorize the exact canonical repository before clone/fetch/push."""
    services = _services()
    principal = services.require_write(ctx, project, ("use:credentials",))
    try:
        result = repository.preflight(
            connection_id, project=project, repository=repository_name,
            operation=operation, actor=services.principal_actor(principal))
    except SCMConnectionError as exc:
        result = exc.as_dict()
    return services.dumps(result)


SCM_CONNECTION_TOOL_NAMES = (
    "create_scm_connection", "list_scm_connections", "get_scm_connection",
    "update_scm_connection", "rotate_scm_connection", "revoke_scm_connection",
    "delete_scm_connection", "preflight_scm_repository",
)


def register_scm_connection_tools(
        mcp: Any, services: SCMConnectionToolServices) -> dict[str, Callable[..., str]]:
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in SCM_CONNECTION_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
