"""MCP client-facing latency probe, process-local tool observability, and
box-saturation dashboard tools (ARCH-MS-70).

Transport adapter extracted from ``mcp_server_impl``. The composition root
still owns the ``MCPObservability`` singleton (it also wraps every tool call
for instrumentation); this module is handed that instance via
``ObservabilityToolServices`` rather than constructing its own.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

import store


@dataclass(frozen=True)
class ObservabilityToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    observability: Any


_SERVICES: ObservabilityToolServices | None = None


def _services() -> ObservabilityToolServices:
    if _SERVICES is None:
        raise RuntimeError("observability MCP tools must be registered before use")
    return _SERVICES


def control_plane_probe(project: str = "maxwell", lane: str = "",
                        include_heavy: bool = False) -> str:
    """Tiny latency probe for MCP clients. Compare your client wall time to server_elapsed_ms.
    A large gap means time is outside Switchboard's Python/SQLite path."""
    from switchboard.application.queries.control_plane_probe import execute
    services = _services()
    probe = execute(project=project, lane=lane, include_heavy=include_heavy)
    probe["mcp_framing"] = {
        "stateless_http": True,
        "approx_tool_payload_bytes": len(services.dumps(probe).encode("utf-8")),
    }
    return services.dumps(probe)


def get_mcp_observability(tool: str = "", slow_limit: int = 50) -> str:
    """Process-local MCP health: per-tool p50/p99/max latency, per-tool SQLite
    lock-wait counts, write-path latency p50/p99 (per tool and aggregate), failures,
    and a bounded slow-call log. No arguments, results, tokens, or other request
    content are retained. tool optionally filters by exact tool name; slow_limit is
    capped by PM_MCP_SLOW_LOG_LIMIT. The same snapshot is scrapeable over plain HTTP
    at GET /observability for operators/monitors that don't speak MCP."""
    services = _services()
    return services.dumps(services.observability.snapshot(tool=tool, slow_limit=slow_limit))


def get_saturation_signals(project: str = "switchboard") -> str:
    """Box saturation dashboard (PERF-7): PSI pressure, sqlite lock-waits, webhook inbox
    depth, HTTP/MCP SLO status, load-shed recommendation, and alert list."""
    services = _services()
    import saturation_signals as sat

    def _mcp_obs():
        window_s = float(os.environ.get("PM_SQLITE_LOCK_WAIT_WINDOW_S", "60"))
        snap = services.observability.snapshot()
        store_waits = store.sqlite_lock_wait_count()
        store_window = store.sqlite_lock_waits_in_window(window_s)
        snap["sqlite_lock_waits"] = max(int(snap.get("sqlite_lock_waits") or 0), store_waits)
        snap["sqlite_lock_waits_window"] = store_window
        snap["sqlite_lock_wait_window_s"] = window_s
        return snap

    return services.dumps(sat.compute_saturation_signals(
        project=project,
        mcp_obs_provider=_mcp_obs,
        request_obs_provider=lambda: {"routes": {}, "dropped_webhook_deliveries": 0},
    ))


OBSERVABILITY_TOOL_NAMES = (
    "control_plane_probe", "get_mcp_observability", "get_saturation_signals",
)


def register_observability_tools(
        mcp: Any, services: ObservabilityToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the observability tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in OBSERVABILITY_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
