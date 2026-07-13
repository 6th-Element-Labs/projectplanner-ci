"""Move-task application command.

Application owns transport-neutral validation for cross-project moves. REST
and MCP adapters both call :func:`execute_mapping_result`; authentication and
response serialization stay at their edges. Persistence remains on
``store.move_task`` until a multi-project repository surface exists.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import ValidationError

import store

from switchboard.contracts import validation_error_message

from ..contracts.tasks import MoveTaskCommand

MoveTaskFn = Callable[..., dict[str, Any]]


class MoveTaskError(ValueError):
    """A command validation failure that adapters can render for their transport."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        # Keep MCP's historic human-readable `error` field while adding a stable code.
        return {"error": self.message, "error_code": self.code,
                "message": self.message, **self.details}


def execute(
        command: MoveTaskCommand,
        *,
        actor: str,
        move: Optional[MoveTaskFn] = None) -> dict[str, Any]:
    """Validate and move one task between project boards."""
    if not command.task_id:
        raise MoveTaskError("invalid_move_task", "task_id is required")
    if not command.project_from:
        raise MoveTaskError("invalid_move_task", "project_from is required")
    if not command.project_to:
        raise MoveTaskError("invalid_move_task", "project_to is required")
    if command.project_from == command.project_to:
        raise MoveTaskError(
            "same_project",
            "source and destination projects must differ",
            project=command.project_from,
            task_id=command.task_id,
        )

    mover = move or store.move_task
    return mover(
        command.task_id,
        project_from=command.project_from,
        project_to=command.project_to,
        reason=command.reason,
        actor=actor,
        new_task_id=command.new_task_id,
        dependency_policy=command.dependency_policy,
    )


def execute_mapping_result(task_id: str, data: dict[str, Any], *, actor: str,
                           move: Optional[MoveTaskFn] = None) -> dict[str, Any]:
    """Execute adapter input and return the store result or a structured error."""
    try:
        return execute(MoveTaskCommand.from_mapping(task_id, data),
                       actor=actor, move=move)
    except MoveTaskError as exc:
        return exc.as_dict()
    except ValidationError as exc:
        return MoveTaskError(
            "invalid_move_task", validation_error_message(exc)).as_dict()
