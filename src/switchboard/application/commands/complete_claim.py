"""Complete-claim application command.

REST and MCP adapters both call :func:`execute_mapping_result`. Authentication,
write-actor binding, and response serialization stay at their edges.
Persistence remains on ``store.complete_claim`` / :class:`ClaimsRepository`.

BREAKDOWN 5: before persistence, mark any PR evidence non-draft (or fail closed)
so MCP callers that bypass the local adapter cannot park a draft in In Review.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import ValidationError

import store

from switchboard.application.pr_ready import attach_pr_ready_evidence, ensure_pr_ready
from switchboard.contracts import validation_error_message

from ..contracts.claims import CompleteClaimCommand

CompleteClaimFn = Callable[..., dict[str, Any]]
EnsurePrReadyFn = Callable[..., dict[str, Any]]


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


def _github_token_for_project(project: str) -> str:
    del project  # token resolution is process-env / SCM lease scoped today
    try:
        from switchboard.storage.repositories import provenance
        return str(provenance._github_token() or "")
    except Exception:
        return ""


def execute(
        command: CompleteClaimCommand,
        *,
        actor: str,
        complete: Optional[CompleteClaimFn] = None,
        ensure_ready: Optional[EnsurePrReadyFn] = None) -> dict[str, Any]:
    """Validate and complete one claim with optional evidence."""
    if not command.claim_id:
        raise CompleteClaimError("invalid_complete_claim", "claim_id is required")

    ensure = ensure_ready or ensure_pr_ready
    token = _github_token_for_project(command.project)
    pr_ready = ensure(command.evidence, token=token)
    # Defence-in-depth for BREAKDOWN 5: never hand a draft PR to merge authorization.
    if pr_ready.get("pr_number") and pr_ready.get("is_draft"):
        return {
            "completed": False,
            "error": "pr_still_draft",
            "error_code": "pr_still_draft",
            "failure_class": "failed_gate",
            "message": (
                pr_ready.get("message")
                or "Worker completion contract requires a non-draft PR before complete_claim."
            ),
            "pr_ready": pr_ready,
        }

    evidence = attach_pr_ready_evidence(command.evidence, pr_ready)

    completer = complete or store.complete_claim
    return completer(
        command.claim_id,
        evidence=evidence,
        final_status=command.final_status,
        actor=actor,
        project=command.project,
        mission_project=command.mission_project,
    )


def execute_mapping_result(data: dict[str, Any], *, actor: str,
                           complete: Optional[CompleteClaimFn] = None,
                           ensure_ready: Optional[EnsurePrReadyFn] = None) -> dict[str, Any]:
    """Execute adapter input and return the store result or a structured error."""
    try:
        return execute(CompleteClaimCommand.from_mapping(data), actor=actor,
                       complete=complete, ensure_ready=ensure_ready)
    except CompleteClaimError as exc:
        return exc.as_dict()
    except ValidationError as exc:
        return CompleteClaimError(
            "invalid_complete_claim", validation_error_message(exc)).as_dict()
