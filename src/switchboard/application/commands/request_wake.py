"""Request-wake application command.

REST and MCP adapters both call :func:`execute_mapping_result`. Authentication
and response serialization stay at their edges. Persistence remains on
``store.request_wake`` / coordination repository.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import ValidationError

import store

from switchboard.contracts import validation_error_message

from ..contracts.wakes import RequestWakeCommand

RequestWakeFn = Callable[..., dict[str, Any]]


class RequestWakeError(ValueError):
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
        command: RequestWakeCommand,
        *,
        actor: str,
        principal_id: str = "",
        request: Optional[RequestWakeFn] = None) -> dict[str, Any]:
    """Validate and create one durable wake intent."""
    if not command.selector:
        raise RequestWakeError("invalid_request_wake", "selector is required")
    if not command.selector.get("runtime") and not command.selector.get("agent_id"):
        raise RequestWakeError(
            "invalid_request_wake",
            "selector.runtime or selector.agent_id required",
        )

    requester = request or store.request_wake
    return requester(
        selector=command.selector,
        reason=command.reason,
        source=command.source or actor,
        policy=command.policy,
        task_id=command.task_id or None,
        principal_id=principal_id,
        actor=actor,
        idem_key=command.idem_key,
        project=command.project,
    )


def execute_mapping_result(data: dict[str, Any], *, actor: str,
                           principal_id: str = "",
                           request: Optional[RequestWakeFn] = None) -> dict[str, Any]:
    """Execute adapter input and return the store result or a structured error."""
    try:
        return execute(RequestWakeCommand.from_mapping(data), actor=actor,
                       principal_id=principal_id, request=request)
    except RequestWakeError as exc:
        return exc.as_dict()
    except ValidationError as exc:
        return RequestWakeError(
            "invalid_request_wake", validation_error_message(exc)).as_dict()
