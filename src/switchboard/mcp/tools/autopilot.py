"""Autopilot MCP tools (UI-58).

The MCP twin of ``/api/deliverables/{id}/autopilot`` and the task variant. Both
adapters call ``switchboard.application.commands.autopilot.execute_mapping_result``
and return its body verbatim, so an agent and an operator see identical bodies
and identical typed errors. No tool here accepts a runner id, host id, wake, or
scope id — the service resolves the live scope from (deliverable, scope_type,
task) alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth
from switchboard.application.commands import autopilot as autopilot_command


@dataclass(frozen=True)
class AutopilotToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: AutopilotToolServices | None = None


def _services() -> AutopilotToolServices:
    if _SERVICES is None:
        raise RuntimeError("autopilot MCP tools must be registered before use")
    return _SERVICES


def get_autopilot(deliverable_id: str, project: str = "maxwell",
                  profile_id: str = "autopilot-default") -> str:
    """List every live (active/paused) Autopilot scope for one deliverable.

    Read-only. Returns {schema:"switchboard.autopilot.v1", deliverable_id,
    scopes:[...]} — the same body the mission cockpit's GET returns. Prefer this
    over reading autopilot_scopes state yourself."""
    return _services().dumps(autopilot_command.execute_mapping_result(
        "get_autopilot", deliverable_id, project=project, profile_id=profile_id))


def control_autopilot(ctx: Context, deliverable_id: str = "", project: str = "maxwell",
                      action: str = "start", scope_type: str = "deliverable",
                      task_project: str = "", task_id: str = "",
                      runtime: str = "codex",
                      profile_id: str = "autopilot-default",
                      agent_id: str = "") -> str:
    """Start, pause, resume, or stop one durable Autopilot scope.

    action ∈ start|pause|resume|stop. A deliverable scope is named by
    deliverable_id; a task scope is named by task_id alone (task_project defaults
    to project) and needs no deliverable, so a task started on its own can also be
    paused, resumed and stopped. start creates or
    idempotently reads back a scope; the other three move an existing live one
    and refuse with no_active_scope when none exists. runtime must be one the
    host supports; an unsupported value returns the real supported_runtimes list.
    The server owns scope identity; you never pass a scope id."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
    return services.dumps(autopilot_command.execute_mapping_result(
        "control_autopilot", deliverable_id, project=project, action=action,
        scope_type=scope_type, task_project=task_project, task_id=task_id,
        runtime=runtime, profile_id=profile_id, actor=auth.actor(principal),
        agent_id=agent_id))


AUTOPILOT_TOOL_NAMES = ("get_autopilot", "control_autopilot")


def register_autopilot_tools(
        mcp: Any, services: AutopilotToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the autopilot tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in AUTOPILOT_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
