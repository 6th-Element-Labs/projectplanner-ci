"""Complete-claim application command.

REST and MCP adapters both call :func:`execute_mapping_result`. Authentication,
write-actor binding, and response serialization stay at their edges.
Persistence remains on ``store.complete_claim`` / :class:`ClaimsRepository`.

BREAKDOWN 5: before persistence, require an injected SCM adapter to prove any PR
evidence non-draft (or fail closed), so every transport enforces the same rule.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Optional

from pydantic import ValidationError

import store

from switchboard.application.pr_ready import (
    PullRequestReadyGateway,
    attach_pr_ready_evidence,
    evidence_mapping,
    pr_number_from_evidence,
    pr_ready_is_proven,
    unavailable_pr_ready_result,
)
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
        complete: Optional[CompleteClaimFn] = None,
        ensure_ready: Optional[PullRequestReadyGateway] = None) -> dict[str, Any]:
    """Validate and complete one claim with optional evidence."""
    if not command.claim_id:
        raise CompleteClaimError("invalid_complete_claim", "claim_id is required")

    number = pr_number_from_evidence(command.evidence)
    if number:
        if ensure_ready is None:
            pr_ready = unavailable_pr_ready_result(command.evidence)
        else:
            try:
                pr_ready = ensure_ready(
                    command.evidence, project=command.project, actor=actor)
            except Exception:
                pr_ready = {
                    "schema": "switchboard.pr_ready.v1",
                    "pr_number": number,
                    "status": "error",
                    "is_draft": None,
                    "error_code": "pr_ready_adapter_failed",
                    "failure_class": "provider_unavailable",
                    "message": "The SCM readiness adapter failed before readiness was proven.",
                }
            if not isinstance(pr_ready, Mapping):
                pr_ready = {
                    "schema": "switchboard.pr_ready.v1",
                    "pr_number": number,
                    "status": "error",
                    "is_draft": None,
                    "error_code": "pr_ready_adapter_invalid_result",
                    "failure_class": "missing_data",
                    "message": "The SCM readiness adapter returned no valid readiness proof.",
                }
    else:
        pr_ready = unavailable_pr_ready_result(command.evidence)
    # Never persist PR completion unless the provider was re-read as non-draft.
    if number and not pr_ready_is_proven(pr_ready, pr_number=number):
        error_code = str(pr_ready.get("error_code") or "pr_ready_unproven")
        return {
            "completed": False,
            "error": error_code,
            "error_code": error_code,
            "failure_class": str(pr_ready.get("failure_class") or "failed_gate"),
            "message": (
                pr_ready.get("message")
                or "Completion requires provider proof that the PR is non-draft."
            ),
            "pr_ready": pr_ready,
        }

    if number:
        submitted_head = str(
            evidence_mapping(command.evidence).get("head_sha") or ""
        ).strip().lower()
        live_head = str(pr_ready.get("head_sha") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", live_head):
            return {
                "completed": False,
                "error": "completion_pr_head_unproven",
                "error_code": "completion_pr_head_unproven",
                "failure_class": "missing_data",
                "message": (
                    "GitHub did not return the authoritative 40-character PR head."
                ),
                "pr_ready": pr_ready,
            }
        if submitted_head != live_head:
            return {
                "completed": False,
                "error": "completion_pr_head_mismatch",
                "error_code": "completion_pr_head_mismatch",
                "failure_class": "stale_branch",
                "message": (
                    "Completion evidence head_sha does not match GitHub's live PR head."
                ),
                "submitted_head_sha": submitted_head or None,
                "live_head_sha": live_head,
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
                           ensure_ready: Optional[PullRequestReadyGateway] = None) -> dict[str, Any]:
    """Execute adapter input and return the store result or a structured error."""
    try:
        return execute(CompleteClaimCommand.from_mapping(data), actor=actor,
                       complete=complete, ensure_ready=ensure_ready)
    except CompleteClaimError as exc:
        return exc.as_dict()
    except ValidationError as exc:
        return CompleteClaimError(
            "invalid_complete_claim", validation_error_message(exc)).as_dict()
