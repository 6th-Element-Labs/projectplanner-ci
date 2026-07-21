"""Task-execution MCP tools (SIMPLIFY-10).

The MCP twin of ``/api/tasks/{task_id}/execution*``. Both adapters call
``switchboard.application.commands.task_execution.execute_mapping_result`` and
return whatever it produced, so an agent and an operator see identical bodies
and identical typed errors for the same request. No tool here accepts a runner
id, host id, or wake payload — the service resolves the current execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth
from switchboard.application.commands import task_execution as task_execution_command


@dataclass(frozen=True)
class TaskExecutionToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: TaskExecutionToolServices | None = None


def _services() -> TaskExecutionToolServices:
    if _SERVICES is None:
        raise RuntimeError("task execution MCP tools must be registered before use")
    return _SERVICES


def _run(command: str, task_id: str, ctx: Context, project: str, **kwargs: Any) -> str:
    services = _services()
    principal = services.require_write(ctx, project)
    return services.dumps(task_execution_command.execute_mapping_result(
        command, task_id, project=project, actor=auth.actor(principal),
        principal_id=str(principal.get("id") or ""), **kwargs))


def get_task_execution(task_id: str, project: str = "maxwell") -> str:
    """The one authoritative answer to "what is running" for a task.

    Returns {execution_id, lifecycle_phase, running, starting, execution, …} where
    ``execution`` is the full wake/claim/Work Session/runner projection and
    ``available_commands`` lists what is legal against the current state.
    Read-only: prefer this over assembling runner/wake/claim state yourself."""
    return _services().dumps(task_execution_command.execute_mapping_result(
        "get_task_execution", task_id, project=project))


def start_task(task_id: str, ctx: Context, project: str = "maxwell",
               role: str = "implementation", runtime: str = "codex") -> str:
    """Start or resume THE task session — identical to the UI Start button.
    Attaches when a live watchable runner exists, reports 'starting' when a dispatch
    is already in flight (idempotent), otherwise asks Connect for capacity matching
    ``runtime``. Callers never pick a host or runner and never assemble a wake;
    failures return the dispatcher's own truthful reason."""
    return _run("start_task", task_id, ctx, project, role=role, runtime=runtime)


def open_session(task_id: str, ctx: Context, project: str = "maxwell",
                 ttl_seconds: int = 0) -> str:
    """Open a watchable terminal on the task's live execution session.

    The server resolves the current runner and mints the relay capability ticket;
    you never supply a runner id. Returns {execution_id, relay_url, relay_path,
    ticket, scopes, expires_at, browser_safe}. When ``browser_safe`` is false the
    relay base is unset or loopback and ``reason`` says so — no URL is invented."""
    return _run("open_session", task_id, ctx, project, ttl_seconds=ttl_seconds)


def send_message(task_id: str, text: str, ctx: Context,
                 project: str = "maxwell") -> str:
    """Queue one message for the task's LIVE execution session (the running agent's
    terminal), bound to this task id. This is not the agent mailbox — use
    send_agent_message for that. Returns {queued: true, delivered: false,
    control_request_id}: the host executes the inject, so delivery is confirmed by
    polling get_task_execution, never asserted here."""
    return _run("send_message", task_id, ctx, project, text=text)


def stop_task(task_id: str, ctx: Context, project: str = "maxwell",
              reason: str = "operator stop", grace_seconds: int = 10) -> str:
    """Stop the task's execution: kill the live runner AND cancel any queued start.
    Both halves end together, because a queued wake left behind a killed runner is
    exactly how a "stopped" task restarts itself. Fails with not_running when
    nothing is live."""
    return _run("stop_task", task_id, ctx, project, reason=reason,
                grace_seconds=grace_seconds)


def retry_task(task_id: str, ctx: Context, project: str = "maxwell",
               role: str = "implementation", runtime: str = "",
               reason: str = "operator retry") -> str:
    """Replace the current attempt — supersede, never fork.

    A queued start is cancelled synchronously and the replacement launches in the
    same call. A LIVE runner cannot be replaced in one call (the host owns process
    death), so retry stops it and returns action='superseding'; poll
    get_task_execution and retry once it is terminal. Switchboard never runs two
    sessions for one task."""
    return _run("retry_task", task_id, ctx, project, role=role,
                runtime=runtime, reason=reason)


def get_execution_transcript(task_id: str = "", execution_id: str = "",
                             project: str = "maxwell", limit: int = 20) -> str:
    """The durable record for one execution, live or completed. Pass task_id for the
    current/most recent attempt, or execution_id (the runner session id) for an
    exact one. Returns host log tails, completed logs-control results, the vendor
    transcript_ref, and failure_reason. ``complete`` is always false with an
    explicit ``incomplete_reason``: full session capture lands with SIMPLIFY-9."""
    return _services().dumps(task_execution_command.execute_mapping_result(
        "get_execution_transcript", task_id, execution_id=execution_id,
        project=project, limit=limit))


TASK_EXECUTION_TOOL_NAMES = (
    "get_task_execution", "start_task", "open_session", "send_message",
    "stop_task", "retry_task", "get_execution_transcript",
)


def register_task_execution_tools(
        mcp: Any,
        services: TaskExecutionToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the task-execution tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in TASK_EXECUTION_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
