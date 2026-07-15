"""Shared durable review-verdict command for REST and MCP (COORD-18)."""
from __future__ import annotations

from typing import Any, Mapping

from pydantic import ValidationError

from switchboard.contracts import validation_error_message
from switchboard.contracts.reviews import (
    RecordReviewVerdictCommand,
    ResolveReviewFindingCommand,
)
from switchboard.storage.repositories.review_verdicts import (
    ReviewVerdictError,
    ReviewVerdictRepository,
    default_review_verdict_repository,
)
from switchboard.storage.repositories.review_remediations import (
    ReviewRemediationRepository,
    default_review_remediation_repository,
)


def execute_mapping(
        data: Mapping[str, Any], *, actor: str, principal_id: str = "",
        project: str,
        repository: ReviewVerdictRepository = default_review_verdict_repository,
        remediation_repository: ReviewRemediationRepository = default_review_remediation_repository,
        raise_errors: bool = False) -> dict[str, Any]:
    """Persist one verdict and idempotently advance its remediation state."""
    try:
        command = RecordReviewVerdictCommand.from_mapping(data)
        result = repository.record(
            command.to_repository_data(), actor=actor, principal_id=principal_id,
            project=project,
        )
        verdict = result.get("verdict") or {}
        if verdict:
            try:
                result["auto_remediation"] = remediation_repository.handle_verdict(
                    verdict, actor="switchboard/auto-remediation", project=project)
            except Exception as exc:  # the durable verdict must not be hidden by a follow-up failure
                result["auto_remediation"] = {
                    "schema": "switchboard.review_remediation.v1",
                    "status": "failed",
                    "failure_class": "failed_gate",
                    "message": str(exc),
                }
        return result
    except ValidationError as exc:
        if raise_errors:
            raise
        return {
            "error": "invalid_review_verdict",
            "error_code": "invalid_review_verdict",
            "message": validation_error_message(exc),
        }
    except ReviewVerdictError as exc:
        if raise_errors:
            raise
        return exc.as_dict()


def resolve_finding_mapping(
        data: Mapping[str, Any], *, actor: str, principal_id: str = "",
        authorized: bool = False, project: str,
        repository: ReviewVerdictRepository = default_review_verdict_repository,
        remediation_repository: ReviewRemediationRepository = default_review_remediation_repository,
        raise_errors: bool = False) -> dict[str, Any]:
    """Validate and persist one authorized exact-head finding waiver/override."""
    try:
        command = ResolveReviewFindingCommand.from_mapping(data)
        result = repository.resolve_finding(
            command.to_repository_data(), actor=actor, principal_id=principal_id,
            authorized=authorized, project=project,
        )
        verdict = result.get("verdict") or {}
        if verdict.get("status") == "pass":
            try:
                result["auto_remediation"] = (
                    remediation_repository.resolve_human_authority(
                        command.task_id, head_sha=command.head_sha,
                        actor=actor, project=project)
                )
            except Exception as exc:
                result["auto_remediation"] = {
                    "schema": "switchboard.review_remediation.v1",
                    "status": "failed",
                    "failure_class": "failed_gate",
                    "message": str(exc),
                }
        return result
    except ValidationError as exc:
        if raise_errors:
            raise
        return {
            "error": "invalid_review_finding_resolution",
            "error_code": "invalid_review_finding_resolution",
            "message": validation_error_message(exc),
        }
    except ReviewVerdictError as exc:
        if raise_errors:
            raise
        return exc.as_dict()
