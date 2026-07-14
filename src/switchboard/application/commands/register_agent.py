"""Register-agent application command.

REST and MCP adapters both call :func:`execute_mapping_result`. Authentication
and response serialization stay at their edges. Persistence remains on
``store.register_agent``.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import ValidationError

import store

from switchboard.contracts import validation_error_message

from ..contracts.agents import RegisterAgentCommand

RegisterAgentFn = Callable[..., dict[str, Any]]


class RegisterAgentError(ValueError):
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
        command: RegisterAgentCommand,
        *,
        actor: str,
        principal_id: str = "",
        register: Optional[RegisterAgentFn] = None) -> dict[str, Any]:
    """Validate and register one live agent session."""
    if not command.agent_id:
        raise RegisterAgentError("invalid_register_agent", "agent_id is required")
    if not command.runtime:
        raise RegisterAgentError("invalid_register_agent", "runtime is required")

    registrar = register or store.register_agent
    return registrar(
        agent_id=command.agent_id,
        runtime=command.runtime,
        model=command.model,
        lane=command.lane,
        task_id=command.task_id,
        ttl_s=command.ttl_s,
        control=command.control,
        protocol=command.protocol,
        principal_id=principal_id,
        actor=actor,
        project=command.project,
    )


def execute_mapping_result(data: dict[str, Any], *, actor: str,
                           principal_id: str = "",
                           register: Optional[RegisterAgentFn] = None) -> dict[str, Any]:
    """Execute adapter input and return the store result or a structured error."""
    try:
        return execute(RegisterAgentCommand.from_mapping(data), actor=actor,
                       principal_id=principal_id, register=register)
    except RegisterAgentError as exc:
        return exc.as_dict()
    except ValidationError as exc:
        return RegisterAgentError(
            "invalid_register_agent", validation_error_message(exc)).as_dict()
