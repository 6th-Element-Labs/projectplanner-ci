"""Generic IXP resource-lease MCP tools.

Transport adapter extracted in ARCH-MS-52. Authentication and MCP serialization
remain edge concerns; persistence stays behind store / application commands.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth
import store


@dataclass(frozen=True)
class ResourceToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: ResourceToolServices | None = None


def _services() -> ResourceToolServices:
    if _SERVICES is None:
        raise RuntimeError("resource MCP tools must be registered before use")
    return _SERVICES


def claim_resource(agent_id: str, resource_type: str, names: str, ctx: Context,
                   task_id: str = "", ttl_seconds: int = 1800,
                   idem_key: str = "", project: str = "maxwell") -> str:
    """Generic IXP resource claim. resource_type can be file, port, build_dir, worktree,
    binary, branch, task, etc. names is comma/newline-separated."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    name_list = [n.strip() for n in names.replace("\n", ",").split(",") if n.strip()]
    return services.dumps(store.claim_resources(
        agent_id=agent_id, resource_type=resource_type, names=name_list,
        task_id=task_id or None, ttl_seconds=ttl_seconds, principal_id=principal["id"],
        actor=auth.actor(principal), idem_key=idem_key, project=project))



def check_resource(resource_type: str, names: str, project: str = "maxwell") -> str:
    """Check whether generic IXP resources are held by active leases."""
    services = _services()
    name_list = [n.strip() for n in names.replace("\n", ",").split(",") if n.strip()]
    return services.dumps(store.check_resources(resource_type, name_list, project=project))



def release_resource(lease_id: str, ctx: Context, project: str = "maxwell") -> str:
    """Release a generic IXP resource lease."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(store.release_resource_lease(
        lease_id, actor=auth.actor(principal), project=project))



def list_active_resource_leases(project: str = "maxwell") -> str:
    """All active generic IXP resource leases."""
    services = _services()
    return services.dumps(store.list_active_resource_leases(project=project))




RESOURCE_TOOL_NAMES = ('claim_resource', 'check_resource', 'release_resource', 'list_active_resource_leases')


def register_resource_tools(mcp: Any, services: ResourceToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in RESOURCE_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
