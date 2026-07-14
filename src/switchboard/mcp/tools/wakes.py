"""Wake-focused MCP tools.

Transport adapter for request_wake / claim_wake / complete_wake. Authentication
and MCP serialization remain edge concerns; the shared application commands used
by REST own transport-neutral validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth
from switchboard.application.commands import claim_wake as claim_wake_command
from switchboard.application.commands import complete_wake as complete_wake_command
from switchboard.application.commands import request_wake as request_wake_command


@dataclass(frozen=True)
class WakeToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: WakeToolServices | None = None


def _services() -> WakeToolServices:
    if _SERVICES is None:
        raise RuntimeError("wake MCP tools must be registered before use")
    return _SERVICES


def request_wake(selector_json: str, reason: str, ctx: Context,
                 source: str = "", policy_json: str = "{}", task_id: str = "",
                 idem_key: str = "", project: str = "maxwell") -> str:
    """Create a durable wake intent for an absent runtime/session.

    selector_json includes runtime/agent_id/lane/capabilities. Example:
    {"runtime":"claude-code","agent_id":"claude-code","lane":"ADAPTER"}.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(request_wake_command.execute_mapping_result(
        {
            "selector_json": selector_json,
            "reason": reason,
            "source": source or auth.actor(principal),
            "policy_json": policy_json,
            "task_id": task_id,
            "idem_key": idem_key,
            "project": project,
        },
        actor=auth.actor(principal),
        principal_id=principal["id"],
    ))


def claim_wake(host_id: str, wake_id: str, ctx: Context,
               project: str = "maxwell") -> str:
    """Atomically assign one pending wake intent to an eligible Agent Host."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(claim_wake_command.execute_mapping_result(
        {
            "host_id": host_id,
            "wake_id": wake_id,
            "project": project,
        },
        actor=auth.actor(principal),
    ))


def complete_wake(wake_id: str, ctx: Context, runner_session_id: str = "",
                  agent_id: str = "", result_json: str = "{}",
                  project: str = "maxwell") -> str:
    """Record wake success/failure after the host daemon starts or fails to start a runtime."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(complete_wake_command.execute_mapping_result(
        {
            "wake_id": wake_id,
            "runner_session_id": runner_session_id,
            "agent_id": agent_id,
            "result_json": result_json,
            "project": project,
        },
        actor=auth.actor(principal),
    ))


WAKE_TOOL_NAMES = ("request_wake", "claim_wake", "complete_wake")


def register_wake_tools(mcp: Any, services: WakeToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the wake tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in WAKE_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
