"""Complete-wake application command.

REST and MCP adapters both call :func:`execute_mapping_result`. Authentication
and response serialization stay at their edges. Persistence remains on
``store.complete_wake`` / coordination repository.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import ValidationError

import store

from switchboard.contracts import validation_error_message

from ..contracts.wakes import CompleteWakeCommand

CompleteWakeFn = Callable[..., dict[str, Any]]


class CompleteWakeError(ValueError):
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
        command: CompleteWakeCommand,
        *,
        actor: str,
        complete: Optional[CompleteWakeFn] = None) -> dict[str, Any]:
    """Validate and record wake success/failure evidence."""
    if not command.wake_id:
        raise CompleteWakeError("invalid_complete_wake", "wake_id is required")

    completer = complete or store.complete_wake
    return completer(
        command.wake_id,
        runner_session_id=command.runner_session_id,
        agent_id=command.agent_id,
        result=command.result,
        actor=actor,
        project=command.project,
    )


def execute_mapping_result(data: dict[str, Any], *, actor: str,
                           complete: Optional[CompleteWakeFn] = None) -> dict[str, Any]:
    """Execute adapter input and return the store result or a structured error."""
    try:
        return execute(CompleteWakeCommand.from_mapping(data), actor=actor,
                       complete=complete)
    except CompleteWakeError as exc:
        return exc.as_dict()
    except ValidationError as exc:
        return CompleteWakeError(
            "invalid_complete_wake", validation_error_message(exc)).as_dict()
