"""Claim-task application command.

REST and MCP adapters both call :func:`execute_mapping_result`. Authentication
and response serialization stay at their edges. Persistence remains on
``store.claim_task`` / :class:`ClaimsRepository`.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import ValidationError

import store

from switchboard.contracts import validation_error_message

from ..contracts.claims import ClaimTaskCommand

ClaimTaskFn = Callable[..., dict[str, Any]]


class ClaimTaskError(ValueError):
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
        command: ClaimTaskCommand,
        *,
        actor: str,
        principal_id: str = "",
        claim: Optional[ClaimTaskFn] = None) -> dict[str, Any]:
    """Validate and claim one exact ready task."""
    if not command.task_id:
        raise ClaimTaskError("invalid_claim_task", "task_id is required")
    if not command.agent_id:
        raise ClaimTaskError("invalid_claim_task", "agent_id is required")

    claimer = claim or store.claim_task
    return claimer(
        task_id=command.task_id,
        agent_id=command.agent_id,
        principal_id=principal_id,
        actor=actor,
        ttl_seconds=command.ttl_seconds,
        idem_key=command.idem_key,
        override_identity_risk=command.override_identity_risk,
        work_session_id=command.work_session_id,
        work_session=command.work_session,
        session_policy_profile=command.session_policy_profile,
        require_work_session=command.require_work_session,
        project=command.project,
    )


def execute_mapping_result(data: dict[str, Any], *, actor: str,
                           principal_id: str = "",
                           claim: Optional[ClaimTaskFn] = None) -> dict[str, Any]:
    """Execute adapter input and return the store result or a structured error."""
    try:
        return execute(ClaimTaskCommand.from_mapping(data), actor=actor,
                       principal_id=principal_id, claim=claim)
    except ClaimTaskError as exc:
        return exc.as_dict()
    except ValidationError as exc:
        return ClaimTaskError(
            "invalid_claim_task", validation_error_message(exc)).as_dict()
