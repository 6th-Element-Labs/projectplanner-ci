"""Send-agent-message application command.

REST and MCP adapters both call :func:`execute_mapping_result`. Authentication
and response serialization stay at their edges. Persistence remains on
``store.send_agent_message`` / the coordination repository.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import ValidationError

import store

from switchboard.contracts import validation_error_message

from ..contracts.messaging import SendAgentMessageCommand

SendAgentMessageFn = Callable[..., dict[str, Any]]


class SendAgentMessageError(ValueError):
    """A command validation failure that adapters can render for their transport."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.message, "error_code": self.code,
                "message": self.message, **self.details}


def execute(
        command: SendAgentMessageCommand,
        *,
        principal_id: str = "",
        send: Optional[SendAgentMessageFn] = None) -> dict[str, Any]:
    """Validate and send one directed agent message."""
    if not command.from_agent:
        raise SendAgentMessageError("invalid_send_agent_message", "from_agent is required")
    if not command.to_agent:
        raise SendAgentMessageError("invalid_send_agent_message", "to_agent is required")
    if not command.message:
        raise SendAgentMessageError("invalid_send_agent_message", "message is required")

    sender = send or store.send_agent_message
    return sender(
        from_agent=command.from_agent,
        to_agent=command.to_agent,
        message=command.message,
        task_id=command.task_id or None,
        requires_ack=command.requires_ack,
        ack_deadline_minutes=command.ack_deadline_minutes,
        ack_timeout_seconds=command.ack_timeout_seconds,
        on_ack_timeout=command.on_ack_timeout or "notify_sender",
        signal=command.signal or None,
        priority=command.priority,
        principal_id=principal_id,
        idem_key=command.idem_key,
        project=command.project,
    )


def execute_mapping_result(data: dict[str, Any], *, principal_id: str = "",
                           send: Optional[SendAgentMessageFn] = None) -> dict[str, Any]:
    """Execute adapter input and return the store result or a structured error."""
    try:
        return execute(SendAgentMessageCommand.from_mapping(data),
                       principal_id=principal_id, send=send)
    except SendAgentMessageError as exc:
        return exc.as_dict()
    except ValidationError as exc:
        return SendAgentMessageError(
            "invalid_send_agent_message", validation_error_message(exc)).as_dict()
