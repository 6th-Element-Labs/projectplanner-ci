"""Complete-claim application command.

REST and MCP adapters both call :func:`execute_mapping_result`. Authentication,
write-actor binding, and response serialization stay at their edges.
Persistence remains on ``store.complete_claim`` / :class:`ClaimsRepository`.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import ValidationError

import store

from switchboard.contracts import validation_error_message

from ..contracts.claims import CompleteClaimCommand

CompleteClaimFn = Callable[..., dict[str, Any]]


class CompleteClaimError(ValueError):
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
        command: CompleteClaimCommand,
        *,
        actor: str,
        complete: Optional[CompleteClaimFn] = None) -> dict[str, Any]:
    """Validate and complete one claim with optional evidence."""
    if not command.claim_id:
        raise CompleteClaimError("invalid_complete_claim", "claim_id is required")

    completer = complete or store.complete_claim
    return completer(
        command.claim_id,
        evidence=command.evidence,
        final_status=command.final_status,
        actor=actor,
        project=command.project,
        mission_project=command.mission_project,
    )


def execute_mapping_result(data: dict[str, Any], *, actor: str,
                           complete: Optional[CompleteClaimFn] = None) -> dict[str, Any]:
    """Execute adapter input and return the store result or a structured error."""
    try:
        return execute(CompleteClaimCommand.from_mapping(data), actor=actor,
                       complete=complete)
    except CompleteClaimError as exc:
        return exc.as_dict()
    except ValidationError as exc:
        return CompleteClaimError(
            "invalid_complete_claim", validation_error_message(exc)).as_dict()
