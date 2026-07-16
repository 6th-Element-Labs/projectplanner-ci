"""Claim-wake application command.

REST and MCP adapters both call :func:`execute_mapping_result`. Authentication
and response serialization stay at their edges. Persistence remains on
``store.claim_wake`` / coordination repository.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import ValidationError

import store

from switchboard.contracts import validation_error_message

from ..contracts.wakes import ClaimWakeCommand

ClaimWakeFn = Callable[..., dict[str, Any]]


class ClaimWakeError(ValueError):
    """A command validation failure that adapters can render for their transport."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        # Keep claimed=false so TXP hosts that default missing claimed→True
        # (adapters/agent_host.py) treat validation failures as claim misses.
        return {"claimed": False, "error": self.message, "error_code": self.code,
                "message": self.message, **self.details}


def execute(
        command: ClaimWakeCommand,
        *,
        actor: str,
        principal_id: str = "",
        claim: Optional[ClaimWakeFn] = None) -> dict[str, Any]:
    """Validate and atomically claim one pending wake intent."""
    if not command.host_id:
        raise ClaimWakeError("invalid_claim_wake", "host_id is required")
    if not command.wake_id:
        raise ClaimWakeError("invalid_claim_wake", "wake_id is required")

    claimer = claim or store.claim_wake
    return claimer(
        command.host_id,
        command.wake_id,
        actor=actor,
        project=command.project,
        runner_session_id=command.runner_session_id,
        credential_lease_id=command.credential_lease_id,
        claim_id=command.claim_id,
        work_session_id=command.work_session_id,
        principal_id=principal_id,
    )


def execute_mapping_result(data: dict[str, Any], *, actor: str, principal_id: str = "",
                           claim: Optional[ClaimWakeFn] = None) -> dict[str, Any]:
    """Execute adapter input and return the store result or a structured error."""
    try:
        return execute(ClaimWakeCommand.from_mapping(data), actor=actor,
                       principal_id=principal_id, claim=claim)
    except ClaimWakeError as exc:
        return exc.as_dict()
    except ValidationError as exc:
        return ClaimWakeError(
            "invalid_claim_wake", validation_error_message(exc)).as_dict()
