"""Thin MCP adapters for the staged mission context and yield protocol."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import auth
from mcp.server.fastmcp import Context

from switchboard.application.commands import mission_journal as commands
from switchboard.application.queries import mission_context as queries


@dataclass(frozen=True)
class MissionToolServices:
    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]
    resolve_write_actor: Callable[..., dict[str, Any]]


_SERVICES: MissionToolServices | None = None


def _services() -> MissionToolServices:
    if _SERVICES is None:
        raise RuntimeError("mission MCP tools must be registered before use")
    return _SERVICES


def get_mission_context(task_id: str, project: str = "maxwell") -> str:
    """Return bounded authority facts only when this task has a mission row."""
    return _services().dumps(queries.get(task_id, project=project))


def list_mission_history(
    task_id: str,
    after_sequence: int = 0,
    limit: int = 50,
    project: str = "maxwell",
) -> str:
    """Return a bounded cursor page of append-only mission evidence."""
    return _services().dumps(queries.list_history(
        task_id,
        project=project,
        after_sequence=after_sequence,
        limit=limit,
    ))


def yield_mission(
    task_id: str,
    execution_id: str,
    generation: int,
    observed_through: int,
    outcome: str,
    requested_role: str,
    ctx: Context,
    head_sha: str = "",
    project: str = "maxwell",
) -> str:
    """Yield one authenticated exact execution to Capacity's lease reaper."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    agent_hint = str(
        principal.get("bound_agent_id")
        or (
            auth.actor(principal)
            if str(principal.get("kind") or "").lower()
            in {"agent", "direct_session"}
            else ""
        )
        or ""
    )
    binding = services.resolve_write_actor(
        principal,
        project=project,
        task_id=task_id,
        agent_id=agent_hint,
    )
    if not binding.get("ok"):
        return services.dumps(binding)
    try:
        result = commands.yield_mission(
            task_id,
            project=project,
            execution_id=execution_id,
            generation=generation,
            observed_through=observed_through,
            outcome=outcome,
            requested_role=requested_role,
            actor=str(binding.get("agent_id") or binding.get("actor") or ""),
            head_sha=head_sha,
        )
    except commands.MissionJournalError as exc:
        return services.dumps({
            "accepted": False,
            "error": exc.code,
            "message": str(exc),
            "task_id": task_id,
        })
    return services.dumps({"accepted": True, **result})


MISSION_TOOL_NAMES = (
    "get_mission_context",
    "list_mission_history",
    "yield_mission",
)


def register_mission_tools(
    mcp: Any,
    services: MissionToolServices,
) -> dict[str, Callable[..., str]]:
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in MISSION_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered


__all__ = [*MISSION_TOOL_NAMES, "MissionToolServices", "register_mission_tools"]
