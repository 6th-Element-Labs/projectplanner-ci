"""Shared durable review-verdict command for REST and MCP (COORD-18)."""
from __future__ import annotations

from typing import Any, Mapping

from pydantic import ValidationError

from switchboard.contracts import validation_error_message
from switchboard.contracts.reviews import RecordReviewVerdictCommand
from switchboard.storage.repositories.review_verdicts import (
    ReviewVerdictError,
    ReviewVerdictRepository,
    default_review_verdict_repository,
)


def execute_mapping(
        data: Mapping[str, Any], *, actor: str, principal_id: str = "",
        project: str,
        repository: ReviewVerdictRepository = default_review_verdict_repository,
        raise_errors: bool = False) -> dict[str, Any]:
    """Validate, authenticate, and persist one exact-head review verdict."""
    try:
        command = RecordReviewVerdictCommand.from_mapping(data)
        return repository.record(
            command.to_repository_data(), actor=actor, principal_id=principal_id,
            project=project,
        )
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
