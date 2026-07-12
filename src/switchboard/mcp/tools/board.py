"""Board-oriented MCP read tools.

This module establishes the reusable read-only registration pattern for MCP
adapters: transport serialization is injected by the host while board behavior
continues to use the green store/agent facades.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import agent
import signals
import store


@dataclass(frozen=True)
class BoardToolServices:
    """Host services needed by the board read adapter."""

    dumps: Callable[[Any], str]


_SERVICES: BoardToolServices | None = None


def _services() -> BoardToolServices:
    if _SERVICES is None:
        raise RuntimeError("board MCP tools must be registered before use")
    return _SERVICES


def list_projects() -> str:
    """List all routable project boards. Returns [{id,label,pretitle}] plus the default id."""
    return _services().dumps({"projects": store.projects(), "default": store.DEFAULT_PROJECT})


def board_summary(project: str = "maxwell") -> str:
    """Full board snapshot: project name + rollups, then one line per task.
    Use ONCE at session start for orientation. For recurring 'has anything changed?' checks
    use get_lane_delta instead — it returns only what changed and costs ~50 tokens when nothing
    did vs ~3000-5000 tokens here. project selects the board ('maxwell' default, 'helm',
    or 'switchboard')."""
    return (f"Project: {store.get_meta('project', project=project)}\n"
            f"Rollups: {_services().dumps(store.board_rollups(project=project))}\n\n"
            f"{agent.board_summary_text(project=project)}")


def get_lane_delta(project: str = "maxwell", lane: str = "",
                   since_cursor: int = 0) -> str:
    """Efficient poll replacement — returns ONLY tasks that changed since your last call.
    Use this instead of board_summary in any polling loop. Costs ~50 tokens when nothing
    changed (empty updates list) vs 3000-5000 tokens for a full board_summary.

    project: 'maxwell', 'helm', or 'switchboard'. lane: workstream id to filter (e.g. 'ENGINE',
    'CHART', 'OWNSHIP', 'ADAPTER') — leave blank for all workstreams. since_cursor: the cursor value from your
    last response; pass 0 on first call.

    Returns {cursor, updates: [{task_id, status, title, workstream_id, kinds}]}.
    Save the returned cursor and pass it on your next call. kinds lists the activity types
    that occurred (edit, comment, create). Call get_task for full detail on any changed task."""
    return _services().dumps(store.get_activity_delta(
        since_cursor=since_cursor, lane=lane, project=project))


def get_plan_signals(project: str = "maxwell") -> str:
    """Derived plan health: counts + overdue/due-soon/blocked/ready tasks, critical-path slips,
    past-due decisions, and each owner's next-best 1-2 tasks. Use for 'what's slipping?' or digests.
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    return _services().dumps(signals.compute_plan_signals(project=project))


BOARD_TOOL_NAMES = (
    "list_projects", "board_summary", "get_lane_delta", "get_plan_signals",
)


def register_board_tools(mcp: Any,
                         services: BoardToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the board read tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in BOARD_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
