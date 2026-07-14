"""NARRATE-13 narration health / control MCP tools (ARCH-MS-70).

Transport adapter extracted from ``mcp_server_impl``. Authentication and MCP
serialization remain edge concerns; the ``narration_ops`` module owns policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth


@dataclass(frozen=True)
class NarrationToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: NarrationToolServices | None = None


def _services() -> NarrationToolServices:
    if _SERVICES is None:
        raise RuntimeError("narration MCP tools must be registered before use")
    return _SERVICES


def get_narration_health(project: str = "switchboard") -> str:
    """NARRATE-13: bounded narration queue + generation-receipt snapshot — attempt-state depth,
    oldest-pending age, success/failure/fallback rates, model-token-cost totals, and alert flags
    (queue age, failure rate, dead letters). Read-only; indexed aggregates only."""
    services = _services()
    import narration_ops
    return services.dumps(narration_ops.narration_health(project))


def narrate_now(entity_type: str, entity_id: str, ctx: Context,
               project: str = "switchboard", reason: str = "") -> str:
    """NARRATE-13: force (re)generation of an entity's current narration revision now. Audited,
    deduped (re-queues the current revision, no new visible effect), and still budget-gated —
    it does not bypass the NARRATE-12 generation policy. entity_type is task or deliverable."""
    services = _services()
    import narration_ops
    principal = services.require_write(ctx, project, ("write:system",))
    return services.dumps(narration_ops.narrate_now(project, entity_type, entity_id,
                                            actor=auth.actor(principal), reason=reason))


def reactivate_narration(event_id: str, ctx: Context, project: str = "switchboard",
                         action: str = "retry", reason: str = "") -> str:
    """NARRATE-13: authorized retry / dead-letter recovery on one narration request (audited).
    action='retry' returns a dead-lettered/errored request to the queue; action='dead_letter'
    parks a poison request. Operates on the existing row; immutable event fields are untouched."""
    services = _services()
    import narration_ops
    principal = services.require_write(ctx, project, ("write:system",))
    return services.dumps(narration_ops.reactivate_request(project, event_id, actor=auth.actor(principal),
                                                   action=action, reason=reason))


NARRATION_TOOL_NAMES = ("get_narration_health", "narrate_now", "reactivate_narration")


def register_narration_tools(mcp: Any, services: NarrationToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the narration tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in NARRATION_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
