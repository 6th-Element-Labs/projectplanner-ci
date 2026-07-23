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


@dataclass(frozen=True)
class RunnerToolServices:
    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: RunnerToolServices | None = None


def _services() -> RunnerToolServices:
    if _SERVICES is None:
        raise RuntimeError("runner MCP tools must be registered before use")
    return _SERVICES


def list_runner_sessions(project: str = "maxwell", host_id: str = "",
                         runtime: str = "", task_id: str = "",
                         status: str = "", include_stale: bool = False) -> str:
    services = _services()
    return services.dumps(runner_control_command.list_sessions(
        host_id=host_id, runtime=runtime, task_id=task_id, status=status,
        include_stale=include_stale, project=project))


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
