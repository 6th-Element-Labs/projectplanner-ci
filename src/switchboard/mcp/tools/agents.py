"""Agent-registry MCP tools.

Transport adapter for register_agent / register_host. Authentication and MCP
serialization remain edge concerns; the shared application commands used by
REST own transport-neutral validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth
from switchboard.application.commands import register_agent as register_agent_command
from switchboard.application.commands import register_host as register_host_command


@dataclass(frozen=True)
class AgentToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: AgentToolServices | None = None


def _services() -> AgentToolServices:
    if _SERVICES is None:
        raise RuntimeError("agent MCP tools must be registered before use")
    return _SERVICES


def register_agent(agent_id: str, runtime: str, ctx: Context, model: str = "",
                   lane: str = "", task_id: str = "", ttl_s: int = 120,
                   control_json: str = "{}", protocol_json: str = "{}",
                   project: str = "maxwell") -> str:
    """Register a live agent session. Call at session start before claiming work.
    control_json advertises truthful control fidelity, e.g. {"mode":"advisory_poll"}.
    protocol_json advertises the adapter protocol envelope returned by get_working_agreement."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(register_agent_command.execute_mapping_result(
        {
            "agent_id": agent_id,
            "runtime": runtime,
            "model": model,
            "lane": lane,
            "task_id": task_id,
            "ttl_s": ttl_s,
            "control_json": control_json,
            "protocol_json": protocol_json,
            "project": project,
        },
        actor=auth.actor(principal),
        principal_id=principal["id"],
    ))


def register_host(host_id: str, runtimes_json: str, ctx: Context,
                  hostname: str = "", repo_root: str = "",
                  agent_host_version: str = "0.1.0",
                  limits_json: str = "{}", heartbeat_ttl_s: int = 60,
                  project: str = "maxwell") -> str:
    """Register an always-on Agent Host that can wake/start runtimes.

    runtimes_json is a JSON list, e.g. [{"runtime":"claude-code","lanes":["ADAPTER"],
    "capabilities":["python","docs"]}]. limits_json can include {"max_sessions":2}.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(register_host_command.execute_mapping_result(
        {
            "host_id": host_id,
            "hostname": hostname,
            "repo_root": repo_root,
            "agent_host_version": agent_host_version,
            "runtimes_json": runtimes_json,
            "limits_json": limits_json,
            "heartbeat_ttl_s": heartbeat_ttl_s,
            "project": project,
        },
        actor=auth.actor(principal),
        principal_id=principal["id"],
    ))


AGENT_TOOL_NAMES = ("register_agent", "register_host")


def register_agent_tools(mcp: Any, services: AgentToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the agent registry tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in AGENT_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
