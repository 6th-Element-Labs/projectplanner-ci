"""Messaging-focused MCP tools.

Transport adapter for send_agent_message / ack_message. Authentication and MCP
serialization remain edge concerns; the shared application commands used by
REST own transport-neutral validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth
from switchboard.application.commands import ack_message as ack_message_command
from switchboard.application.commands import send_agent_message as send_agent_message_command


@dataclass(frozen=True)
class MessagingToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: MessagingToolServices | None = None


def _services() -> MessagingToolServices:
    if _SERVICES is None:
        raise RuntimeError("messaging MCP tools must be registered before use")
    return _SERVICES


def send_agent_message(from_agent: str, to_agent: str, message: str,
                       ctx: Context, project: str = "maxwell", task_id: str = "",
                       requires_ack: bool = False,
                       ack_deadline_minutes: int = 0,
                       ack_timeout_seconds: float = 0,
                       ack_timeout_s: float = 0,
                       on_ack_timeout: str = "notify_sender",
                       signal: str = "", priority: int = 0,
                       idem_key: str = "") -> str:
    """Send a directed message to another agent session. Unlike add_comment (bulletin
    board, fire-and-forget), this has an ack/read-receipt so the sender can confirm
    the message landed before acting on the assumption it was received.

    from_agent / to_agent: stable agent-session identifiers (e.g. 'claude/ENGINE-11').
    task_id: the task this message is about (optional).
    requires_ack: if true, the receiving agent should call ack_message to confirm receipt.
    ack_deadline_minutes: how long the sender will wait for an ack (0 = no deadline).
    ack_timeout_seconds: equivalent seconds-based alias; used when minutes is 0.
    ack_timeout_s: short alias for ack_timeout_seconds (IXP field_aliases / ARCH-MS-43).

    Returns the message record including its id and a versioned delivery_receipt. A true
    mailbox_stored value means durable storage only, never runtime delivery. The receipt
    separately reports active-session reachability, Agent Host wakeability/queue state,
    visible task-comment fallback, and whether an ack proves handling. Pass the id to
    get_message_status to check whether the recipient has acked."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(send_agent_message_command.execute_mapping_result(
        {
            "from_agent": from_agent,
            "to_agent": to_agent,
            "message": message,
            "project": project,
            "task_id": task_id,
            "requires_ack": requires_ack,
            "ack_deadline_minutes": ack_deadline_minutes,
            "ack_timeout_seconds": ack_timeout_seconds,
            "ack_timeout_s": ack_timeout_s,
            "on_ack_timeout": on_ack_timeout,
            "signal": signal,
            "priority": priority,
            "idem_key": idem_key,
        },
        principal_id=principal["id"],
    ))


def ack_message(message_id: int, ctx: Context, project: str = "maxwell",
                response: str = "") -> str:
    """Acknowledge a directed message. Call this when you have received and understood a
    message that has requires_ack=true. response is optional — include it to give the
    sender a one-line confirmation (e.g. 'seen — will rebase before merging').
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(ack_message_command.execute_mapping_result(
        {
            "message_id": message_id,
            "project": project,
            "response": response,
        },
        actor=auth.actor(principal),
    ))


MESSAGING_TOOL_NAMES = ("send_agent_message", "ack_message")


def register_messaging_tools(
        mcp: Any, services: MessagingToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the messaging tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in MESSAGING_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
