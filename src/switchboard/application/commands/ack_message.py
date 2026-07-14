"""Ack-message application command.

REST and MCP adapters both call :func:`execute_mapping_result`. Authentication
and response serialization stay at their edges. Persistence remains on
``store.ack_message`` / the coordination repository.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import ValidationError

import store

from switchboard.contracts import validation_error_message

from ..contracts.messaging import AckMessageCommand

AckMessageFn = Callable[..., dict[str, Any]]


class AckMessageError(ValueError):
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
        command: AckMessageCommand,
        *,
        actor: str,
        ack: Optional[AckMessageFn] = None) -> dict[str, Any]:
    """Validate and acknowledge one directed message."""
    if command.message_id is None:
        raise AckMessageError("invalid_ack_message", "message_id is required")

    acker = ack or store.ack_message
    return acker(
        command.message_id,
        response=command.response,
        actor=actor,
        project=command.project,
    )


def execute_mapping_result(data: dict[str, Any], *, actor: str,
                           ack: Optional[AckMessageFn] = None) -> dict[str, Any]:
    """Execute adapter input and return the store result or a structured error."""
    try:
        return execute(AckMessageCommand.from_mapping(data), actor=actor, ack=ack)
    except AckMessageError as exc:
        return exc.as_dict()
    except ValidationError as exc:
        return AckMessageError(
            "invalid_ack_message", validation_error_message(exc)).as_dict()
