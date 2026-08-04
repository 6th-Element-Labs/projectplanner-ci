"""Runner registry and Agent Host control-delivery MCP tools.

Operators control executions through the task-execution tools. These tools keep
only the physical registry and the host-side request delivery protocol.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth
from switchboard.application.commands import runner_control as runner_control_command
from switchboard.mcp.tools import read_summaries


@dataclass(frozen=True)
class RunnerToolServices:
    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: RunnerToolServices | None = None

_DEFAULT_COMPACT_LIMIT = 50
_DEFAULT_FULL_LIMIT = 10
_MAX_COMPACT_LIMIT = 200
_MAX_FULL_LIMIT = 25
_MAX_RUNNER_RESPONSE_BYTES = 1_000_000


def _services() -> RunnerToolServices:
    if _SERVICES is None:
        raise RuntimeError("runner MCP tools must be registered before use")
    return _SERVICES


def list_runner_sessions(project: str = "maxwell", host_id: str = "",
                         runtime: str = "", task_id: str = "",
                         status: str = "", include_stale: bool = False,
                         full: bool = False, limit: int = 0,
                         before_heartbeat_at: float | None = None,
                         before_runner_session_id: str = "") -> str:
    """List one bounded keyset page, newest first.

    For the next page, pass the last row's heartbeat_at and runner_session_id
    back as before_heartbeat_at and before_runner_session_id. ``full`` controls
    fields, never cardinality; use get_runner_session for exact full detail.
    """
    services = _services()
    maximum = _MAX_FULL_LIMIT if full else _MAX_COMPACT_LIMIT
    requested = int(limit or (_DEFAULT_FULL_LIMIT if full else _DEFAULT_COMPACT_LIMIT))
    if requested < 1 or requested > maximum:
        return services.dumps({
            "error": "runner_session_limit_out_of_range",
            "failure_class": "invalid_input",
            "requested_limit": requested,
            "maximum_limit": maximum,
            "message": "Use bounded keyset pages or get_runner_session for one full record.",
        })
    if ((before_heartbeat_at is None) != (not before_runner_session_id)):
        return services.dumps({
            "error": "runner_session_cursor_incomplete",
            "failure_class": "invalid_input",
            "message": "before_heartbeat_at and before_runner_session_id must be supplied together.",
        })
    result = runner_control_command.list_sessions(
        host_id=host_id, runtime=runtime, task_id=task_id, status=status,
        include_stale=include_stale, limit=requested,
        before_heartbeat_at=before_heartbeat_at,
        before_runner_session_id=before_runner_session_id,
        include_claim=False, summary=not full, project=project)
    payload = result if full else read_summaries.runner_sessions(result)
    serialized = services.dumps(payload)
    if len(serialized.encode("utf-8")) > _MAX_RUNNER_RESPONSE_BYTES:
        return services.dumps({
            "error": "runner_session_result_too_large",
            "failure_class": "failed_gate",
            "returned_rows": len(result),
            "maximum_bytes": _MAX_RUNNER_RESPONSE_BYTES,
            "message": "Retry with a smaller limit or use get_runner_session for exact detail.",
        })
    return serialized


def get_runner_session(runner_session_id: str,
                       project: str = "maxwell") -> str:
    """Return one exact full runner record instead of expanding list history."""
    services = _services()
    result = runner_control_command.get_session(
        runner_session_id, project=project)
    if result is None:
        return services.dumps({
            "error": "runner_session_not_found",
            "runner_session_id": runner_session_id,
        })
    serialized = services.dumps(result)
    if len(serialized.encode("utf-8")) > _MAX_RUNNER_RESPONSE_BYTES:
        return services.dumps({
            "error": "runner_session_result_too_large",
            "failure_class": "failed_gate",
            "runner_session_id": runner_session_id,
            "maximum_bytes": _MAX_RUNNER_RESPONSE_BYTES,
            "message": "Use get_execution_transcript or the audit export for oversized evidence.",
        })
    return serialized


def register_runner_session(runner_session_json: str, ctx: Context,
                            project: str = "maxwell") -> str:
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        record = json.loads(runner_session_json or "{}")
    except Exception:
        return services.dumps(
            {"error": "runner_session_json must be a JSON object string"})
    if not isinstance(record, dict):
        return services.dumps(
            {"error": "runner_session_json must be a JSON object string"})
    return services.dumps(runner_control_command.upsert_session_mapping_result(
        {**record, "project": project},
        principal_id=principal["id"], actor=auth.actor(principal)))


def list_runner_control_requests(project: str = "maxwell", status: str = "",
                                 host_id: str = "",
                                 runner_session_id: str = "") -> str:
    services = _services()
    return services.dumps(runner_control_command.list_control_requests(
        status=status, host_id=host_id, runner_session_id=runner_session_id,
        project=project))


def claim_runner_control(host_id: str, request_id: str, ctx: Context,
                         project: str = "maxwell") -> str:
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(runner_control_command.claim_mapping_result(
        {"host_id": host_id, "request_id": request_id, "project": project},
        actor=auth.actor(principal)))


def complete_runner_control(request_id: str, ctx: Context,
                            result_json: str = "{}", snapshot_json: str = "{}",
                            status: str = "", project: str = "maxwell") -> str:
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        result = json.loads(result_json or "{}")
        snapshot = json.loads(snapshot_json or "{}")
    except Exception:
        return services.dumps(
            {"error": "result_json and snapshot_json must be JSON object strings"})
    return services.dumps(runner_control_command.complete_mapping_result(
        {"request_id": request_id, "result": result, "snapshot": snapshot,
         "status": status, "project": project},
        actor=auth.actor(principal)))


RUNNER_TOOL_NAMES = (
    "list_runner_sessions",
    "get_runner_session",
    "register_runner_session",
    "list_runner_control_requests",
    "claim_runner_control",
    "complete_runner_control",
)


def register_runner_tools(
        mcp: Any, services: RunnerToolServices) -> dict[str, Callable[..., str]]:
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in RUNNER_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
