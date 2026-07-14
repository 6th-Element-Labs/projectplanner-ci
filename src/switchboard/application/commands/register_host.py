"""Register-host application command.

REST and MCP adapters both call :func:`execute_mapping_result`. Authentication
and response serialization stay at their edges. Persistence remains on
``store.register_host``.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import ValidationError

import store

from switchboard.contracts import validation_error_message

from ..contracts.agents import RegisterHostCommand

RegisterHostFn = Callable[..., dict[str, Any]]


class RegisterHostError(ValueError):
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
        command: RegisterHostCommand,
        *,
        actor: str,
        principal_id: str = "",
        register: Optional[RegisterHostFn] = None) -> dict[str, Any]:
    """Validate and register one Agent Host inventory record."""
    if not command.host_id:
        raise RegisterHostError("invalid_register_host", "host_id is required")

    registrar = register or store.register_host
    return registrar(
        command.to_inventory(),
        principal_id=principal_id,
        actor=actor,
        project=command.project,
    )


def execute_mapping_result(data: dict[str, Any], *, actor: str,
                           principal_id: str = "",
                           register: Optional[RegisterHostFn] = None) -> dict[str, Any]:
    """Execute adapter input and return the store result or a structured error."""
    try:
        return execute(RegisterHostCommand.from_mapping(data), actor=actor,
                       principal_id=principal_id, register=register)
    except RegisterHostError as exc:
        return exc.as_dict()
    except ValidationError as exc:
        return RegisterHostError(
            "invalid_register_host", validation_error_message(exc)).as_dict()
