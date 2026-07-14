"""Session-boot MCP tools — thin adapters over application.session_boot."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from switchboard.application import session_boot


@dataclass(frozen=True)
class BootToolServices:
    """Host services needed by the session-boot adapter."""

    dumps: Callable[[Any], str]


_SERVICES: BootToolServices | None = None


def _services() -> BootToolServices:
    if _SERVICES is None:
        raise RuntimeError("boot MCP tools must be registered before use")
    return _SERVICES


def get_project_contract(
    project: str = "maxwell",
    lane: str = "",
    task_id: str = "",
    deliverable_id: str = "",
    board_id: str = "",
    mission_id: str = "",
    milestone_id: str = "",
) -> str:
    """Return the project-level project/lane/task contract for any Switchboard project.

    This is the project-agnostic replacement for assuming repo-local files such as
    docs/EPICS.md describe the active board. It returns the selected project, lane tasks,
    assigned task deliverable/exit criteria/dependencies, active agents in the lane, and
    operating rules. When deliverable_id or board_id/mission_id is set, it also returns
    mission_context from get_mission_status. Use it at boot and whenever a repo contains
    docs for a different project.
    """
    return _services().dumps(session_boot.get_project_contract(
        project=project,
        lane=lane,
        task_id=task_id,
        deliverable_id=deliverable_id,
        board_id=board_id,
        mission_id=mission_id,
        milestone_id=milestone_id,
    ))


def prepare_agent_session(
    runtime: str = "",
    agent_id: str = "",
    project: str = "",
    task_id: str = "",
    lane: str = "",
    model: str = "",
    intent: str = "",
    deliverable_id: str = "",
    board_id: str = "",
    mission_id: str = "",
    milestone_id: str = "",
) -> str:
    """Boot-time resolver for autonomous agents.

    Call this BEFORE get_working_agreement/register_agent/claim_next. It lists available
    project boards, resolves task_id or lane to the correct project when possible, validates
    any explicit project choice, and returns a project-bound startup prompt plus exact first
    MCP calls. This prevents agents from silently landing on the default Maxwell board or
    doing Vulkan work on Helm.

    When deliverable_id or board_id/mission_id is set, the session is deliverable-first:
    project is the mission-home project that owns the deliverable record, and first_calls
    include get_mission_status before task work begins.
    """
    return _services().dumps(session_boot.prepare_agent_session(
        runtime=runtime,
        agent_id=agent_id,
        project=project,
        task_id=task_id,
        lane=lane,
        model=model,
        intent=intent,
        deliverable_id=deliverable_id,
        board_id=board_id,
        mission_id=mission_id,
        milestone_id=milestone_id,
    ))


BOOT_TOOL_NAMES = (
    "get_project_contract",
    "prepare_agent_session",
)


def register_boot_tools(
    mcp: Any,
    services: BootToolServices,
) -> dict[str, Callable[..., str]]:
    """Configure and register session-boot tools on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in BOOT_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
