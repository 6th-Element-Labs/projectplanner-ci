"""Claim-next application command.

REST and MCP adapters both call :func:`execute_mapping_result`. Authentication
and response serialization stay at their edges. Persistence remains on
``store.claim_next`` / :class:`ClaimsRepository`.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import ValidationError

import store

from switchboard.contracts import validation_error_message

from ..contracts.claims import ClaimNextCommand

ClaimNextFn = Callable[..., dict[str, Any]]


class ClaimNextError(ValueError):
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
        command: ClaimNextCommand,
        *,
        actor: str,
        principal_id: str = "",
        claim: Optional[ClaimNextFn] = None) -> dict[str, Any]:
    """Validate and claim the next eligible task for an agent."""
    if not command.agent_id:
        raise ClaimNextError("invalid_claim_next", "agent_id is required")

    claimer = claim or store.claim_next
    return claimer(
        agent_id=command.agent_id,
        lanes=list(command.lanes),
        capabilities=list(command.capabilities),
        max_risk=command.max_risk,
        max_budget_usd=command.max_budget_usd,
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
        deliverable_id=command.deliverable_id,
        board_id=command.board_id,
        mission_id=command.mission_id,
        milestone_id=command.milestone_id,
    )


def execute_mapping_result(data: dict[str, Any], *, actor: str,
                           principal_id: str = "",
                           claim: Optional[ClaimNextFn] = None) -> dict[str, Any]:
    """Execute adapter input and return the store result or a structured error."""
    try:
        return execute(ClaimNextCommand.from_mapping(data), actor=actor,
                       principal_id=principal_id, claim=claim)
    except ClaimNextError as exc:
        return exc.as_dict()
    except ValidationError as exc:
        return ClaimNextError(
            "invalid_claim_next", validation_error_message(exc)).as_dict()
