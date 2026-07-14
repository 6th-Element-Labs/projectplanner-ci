"""Agent-registry MCP tools.

Transport adapter for register_agent / register_host. Authentication and MCP
serialization remain edge concerns; the shared application commands used by
REST own transport-neutral validation.
"""
from __future__ import annotations

import json

from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth
import store
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


def heartbeat(agent_id: str, ctx: Context, project: str = "maxwell") -> str:
    """Renew presence for a registered agent session."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(store.heartbeat(agent_id, actor=auth.actor(principal), project=project))



def list_active_agents(project: str = "maxwell", lane: str = "") -> str:
    """List active registered agents and their advertised control fidelity."""
    services = _services()
    return services.dumps(store.list_active_agents(lane=lane, project=project))



def heartbeat_host(host_id: str, ctx: Context, active_sessions: int = -1,
                   capacity_json: str = "{}", status: str = "online",
                   last_error: str = "", project: str = "maxwell") -> str:
    """Renew liveness/capacity for an Agent Host."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        capacity = json.loads(capacity_json or "{}")
    except Exception:
        return services.dumps({"error": "capacity_json must be a JSON object string"})
    return services.dumps(store.heartbeat_host(
        host_id, active_sessions=(None if active_sessions < 0 else active_sessions),
        capacity=capacity, status=status, last_error=last_error,
        actor=auth.actor(principal), project=project))



def list_agent_hosts(project: str = "maxwell", runtime: str = "", lane: str = "",
                     capability: str = "", include_stale: bool = False) -> str:
    """List registered Agent Hosts and their wake capacity."""
    services = _services()
    return services.dumps(store.list_agent_hosts(runtime=runtime, lane=lane,
                                        capability=capability,
                                        include_stale=include_stale,
                                        project=project))



def host_status(host_id: str, project: str = "maxwell") -> str:
    """Return one Agent Host's inventory, liveness, capacity, and wake counts."""
    services = _services()
    return services.dumps(store.host_status(host_id, project=project))



def set_agent_state(task_id: str, agent_id: str, state: str,
                    ctx: Context, project: str = "maxwell") -> str:
    """Write your working state for a task — a small JSON object (max ~500 chars)
    capturing what you're doing, where you are, and what you plan next. Stored inside
    the task and visible to all agents via get_agent_state. Good keys to include:
      "files_open": which files you have staged or modified
      "next_step": what you're about to do next
      "blocked_on": what you're waiting for (or null)
      "progress": e.g. "3/7 tests passing"
    state: JSON-string object. agent_id: your stable session id (e.g. 'claude/ENGINE-11').
    project: 'maxwell' (default), 'helm', or 'switchboard'."""
    services = _services()
    try:
        state_obj = json.loads(state)
    except Exception:
        return services.dumps({"error": "state must be a valid JSON object string"})
    services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(store.set_agent_state(task_id, agent_id, state_obj, project=project))



def get_agent_state(task_id: str, project: str = "maxwell") -> str:
    """Read the working-state blobs for all agents currently on a task.
    Returns {agent_id: {state fields}, ...}. Call this before starting work on a
    task to see if another agent is already active, what files it has open, and what
    it plans next — complements list_unacked_messages for live coordination.
    project: 'maxwell' (default), 'helm', or 'switchboard'."""
    services = _services()
    return services.dumps(store.get_agent_state(task_id, project=project))


AGENT_TOOL_NAMES = ("register_agent", "register_host", 'heartbeat', 'list_active_agents',
                    'heartbeat_host', 'list_agent_hosts', 'host_status',
                    'set_agent_state', 'get_agent_state')


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
