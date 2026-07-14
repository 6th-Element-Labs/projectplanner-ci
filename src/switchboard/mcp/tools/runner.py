"""Runner session / control MCP tools.

Transport adapter extracted in ARCH-MS-67. Authentication and MCP serialization
remain edge concerns; shared runner_control commands own transport-neutral mapping.
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
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: RunnerToolServices | None = None


def _services() -> RunnerToolServices:
    if _SERVICES is None:
        raise RuntimeError("runner MCP tools must be registered before use")
    return _SERVICES


def list_runner_sessions(project: str = "maxwell", host_id: str = "", runtime: str = "",
                         task_id: str = "", status: str = "",
                         include_stale: bool = False) -> str:
    """List live runner sessions with host/runtime/task/claim/fidelity and available actions."""
    services = _services()
    return services.dumps(runner_control_command.list_sessions(
        host_id=host_id, runtime=runtime, task_id=task_id, status=status,
        include_stale=include_stale, project=project))


def register_runner_session(runner_session_json: str, ctx: Context,
                            project: str = "maxwell") -> str:
    """Register or heartbeat one supervised runner session.

    runner_session_json should include runner_session_id, host_id, agent_id, runtime,
    task_id/claim_id when known, status, and control. runner_kill is accepted only for
    host-owned managed_process sessions.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        record = json.loads(runner_session_json or "{}")
    except Exception:
        return services.dumps({"error": "runner_session_json must be a JSON object string"})
    if not isinstance(record, dict):
        return services.dumps({"error": "runner_session_json must be a JSON object string"})
    return services.dumps(runner_control_command.upsert_session_mapping_result(
        {**record, "project": project},
        principal_id=principal["id"], actor=auth.actor(principal)))


def request_runner_snapshot(runner_session_id: str, ctx: Context,
                            reason: str = "", project: str = "maxwell") -> str:
    """Request a host-side snapshot for a managed runner session."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(runner_control_command.request_mapping_result(
        {"runner_session_id": runner_session_id, "action": "snapshot",
         "reason": reason, "project": project},
        actor=auth.actor(principal), principal_id=principal["id"]))


def request_runner_kill(runner_session_id: str, ctx: Context,
                        reason: str = "", grace_seconds: float = 5.0,
                        signal: str = "TERM", project: str = "maxwell") -> str:
    """Request a host-side runner kill. The request is audited and carries a pre-kill snapshot."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(runner_control_command.request_mapping_result(
        {"runner_session_id": runner_session_id, "action": "kill", "reason": reason,
         "options": {"grace_seconds": grace_seconds, "signal": signal or "TERM"},
         "project": project},
        actor=auth.actor(principal), principal_id=principal["id"]))


def request_runner_health(runner_session_id: str, ctx: Context,
                          reason: str = "", project: str = "maxwell") -> str:
    """Request host-side runner health from an environment that supports it.

    Unsupported runtimes return a refused control request with reason=not_supported.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(runner_control_command.request_mapping_result(
        {"runner_session_id": runner_session_id, "action": "health",
         "reason": reason, "project": project},
        actor=auth.actor(principal), principal_id=principal["id"]))


def request_runner_logs(runner_session_id: str, ctx: Context,
                        reason: str = "", project: str = "maxwell") -> str:
    """Request host-side runner logs from an environment that supports it.

    Unsupported runtimes return a refused control request with reason=not_supported.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(runner_control_command.request_mapping_result(
        {"runner_session_id": runner_session_id, "action": "logs",
         "reason": reason, "project": project},
        actor=auth.actor(principal), principal_id=principal["id"]))


def request_runner_open(runner_session_id: str, ctx: Context,
                        reason: str = "", project: str = "maxwell") -> str:
    """Request a host-side open action when the runtime explicitly advertises runner_open.

    Unsupported runtimes return a refused control request with reason=not_supported.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(runner_control_command.request_mapping_result(
        {"runner_session_id": runner_session_id, "action": "open",
         "reason": reason, "project": project},
        actor=auth.actor(principal), principal_id=principal["id"]))


def list_runner_control_requests(project: str = "maxwell", status: str = "",
                                 host_id: str = "",
                                 runner_session_id: str = "") -> str:
    """List pending/completed runner snapshot/kill/restart/health/log/open control requests."""
    services = _services()
    return services.dumps(runner_control_command.list_control_requests(
        status=status, host_id=host_id, runner_session_id=runner_session_id,
        project=project))


def claim_runner_control(host_id: str, request_id: str, ctx: Context,
                         project: str = "maxwell") -> str:
    """Agent Host claims a pending runner control request for one of its sessions."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(runner_control_command.claim_mapping_result(
        {"host_id": host_id, "request_id": request_id, "project": project},
        actor=auth.actor(principal)))


def complete_runner_control(request_id: str, ctx: Context, result_json: str = "{}",
                            snapshot_json: str = "{}", status: str = "",
                            project: str = "maxwell") -> str:
    """Agent Host completes a runner control request after snapshot/kill execution."""
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
    "request_runner_snapshot",
    "request_runner_kill",
    "request_runner_health",
    "request_runner_logs",
    "request_runner_open",
    "list_runner_control_requests",
    "claim_runner_control",
    "complete_runner_control",
)


def register_runner_tools(mcp: Any, services: RunnerToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in RUNNER_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
