"""Control-plane latency probe query (ARCH-MS-63).

Moved from ``repositories/shell.py``. Fan-in read model over activity cursor,
host list, and lane delta — not a SQL repository.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List

from constants import DEFAULT_PROJECT
from db.core import _json_size_bytes, _sqlite_busy


__all__ = ["control_plane_probe", "execute", "execute_mapping_result"]


def _store():
    import store
    return store


def execute(project: str = DEFAULT_PROJECT, lane: str = "",
            include_heavy: bool = False) -> Dict[str, Any]:
    """Tiny read-only timing probe for separating server work from bridge/client time."""
    store = _store()
    started = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    lane_filter = (lane or "").strip()

    def measure(name: str, fn):
        op_started = time.perf_counter()
        try:
            summary = fn()
            ok = not (isinstance(summary, dict) and summary.get("error"))
        except sqlite3.OperationalError as exc:
            if _sqlite_busy(exc):
                summary = store._control_plane_unavailable(name, project, time.time(), exc)
                ok = False
            else:
                raise
        except Exception as exc:
            summary = {"error": type(exc).__name__, "message": str(exc)}
            ok = False
        checks.append({
            "name": name,
            "ok": ok,
            "elapsed_ms": round((time.perf_counter() - op_started) * 1000, 3),
            "payload_bytes": _json_size_bytes(summary),
            "summary": summary,
        })
        return summary

    cursor_summary = measure("activity_cursor", lambda: {"cursor": store._activity_cursor(project)})
    cursor = int(cursor_summary.get("cursor") or 0) if isinstance(cursor_summary, dict) else 0

    def host_summary() -> Dict[str, Any]:
        hosts = store.list_agent_hosts(project=project)
        if hosts and isinstance(hosts[0], dict) and hosts[0].get("error"):
            return hosts[0]
        return {
            "host_count": len(hosts),
            "stale_count": sum(1 for h in hosts if h.get("stale")),
        }

    measure("list_agent_hosts", host_summary)

    def delta_summary() -> Dict[str, Any]:
        delta = store.get_activity_delta(since_cursor=cursor, lane=lane_filter, project=project)
        return {
            "cursor": delta.get("cursor"),
            "update_count": len(delta.get("updates") or []),
            "lane": lane_filter,
        }

    measure("get_lane_delta_empty", delta_summary)

    if include_heavy:
        def board_summary_probe() -> Dict[str, Any]:
            payload = store.board_payload(project=project)
            return {
                "task_count": payload.get("rollups", {}).get("total_tasks"),
                "workstream_count": payload.get("rollups", {}).get("total_workstreams"),
                "payload_under_test_bytes": _json_size_bytes(payload),
            }

        measure("board_payload_heavy", board_summary_probe)

    result = {
        "project": project,
        "lane": lane_filter,
        "include_heavy": include_heavy,
        "server_elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "checks": checks,
        "interpretation": (
            "Compare client wall time to server_elapsed_ms. If client wall time is much larger, "
            "the excess is outside Switchboard Python/SQLite: TLS/network, MCP bridge dispatch, "
            "response framing, payload transfer, or client-side scheduling."
        ),
    }
    result["approx_response_bytes"] = _json_size_bytes(result)
    return result


def execute_mapping_result(project: str = DEFAULT_PROJECT, lane: str = "",
                           include_heavy: bool = False) -> Dict[str, Any]:
    return execute(project=project, lane=lane, include_heavy=include_heavy)


control_plane_probe = execute
