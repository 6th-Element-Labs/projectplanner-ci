"""Board/git-provenance reconcile MCP tools (ARCH-MS-70).

Transport adapter extracted from ``mcp_server_impl``. Authentication and MCP
serialization remain edge concerns; persistence stays behind ``store``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import store


@dataclass(frozen=True)
class ReconcileToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: ReconcileToolServices | None = None


def _services() -> ReconcileToolServices:
    if _SERVICES is None:
        raise RuntimeError("reconcile MCP tools must be registered before use")
    return _SERVICES


def reconcile(project: str = "maxwell") -> str:
    """Run the local board/git-provenance drift report. This first pass catches board-internal
    contradictions such as Done without merged_sha or In Review without PR/branch evidence."""
    services = _services()
    return services.dumps(store.reconcile(project=project))


def reconcile_alerts(ctx: Context, project: str = "maxwell",
                     alert_to: str = "switchboard/operator",
                     min_severity: str = "medium",
                     requires_ack: bool = False) -> str:
    """Run the scheduled reconcile alert path now: reconcile, filter actionable findings,
    dedupe inside the configured window, and emit a directed agent message when needed.

    Reconcile alerts are fire-and-forget by default (requires_ack=false) so the ack inbox
    stays reserved for coordinator/agent handoffs. Legacy reconcile_alert backlog is
    auto-closed on each run."""
    services = _services()
    services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(store.run_reconcile_alerts(
        project=project, alert_to=alert_to, min_severity=min_severity,
        requires_ack=requires_ack))


RECONCILE_TOOL_NAMES = ("reconcile", "reconcile_alerts")


def register_reconcile_tools(mcp: Any, services: ReconcileToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the reconcile tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in RECONCILE_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
