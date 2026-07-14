"""Read-side queries for durable review verdicts and findings (COORD-18)."""
from __future__ import annotations

from typing import Any

from switchboard.contracts.reviews import GetReviewVerdictQuery, ListReviewFindingsQuery
from switchboard.storage.repositories.review_verdicts import (
    ReviewVerdictRepository,
    default_review_verdict_repository,
)


def get(query: GetReviewVerdictQuery, *,
        repository: ReviewVerdictRepository = default_review_verdict_repository
        ) -> dict[str, Any] | None:
    return repository.get(
        query.task_id, head_sha=query.head_sha, project=query.project)


def get_for(task_id: str, *, project: str, head_sha: str = "",
            repository: ReviewVerdictRepository = default_review_verdict_repository
            ) -> dict[str, Any] | None:
    return get(
        GetReviewVerdictQuery(task_id=task_id, project=project, head_sha=head_sha),
        repository=repository,
    )


def list_findings(query: ListReviewFindingsQuery, *,
                  repository: ReviewVerdictRepository = default_review_verdict_repository
                  ) -> list[dict[str, Any]]:
    return repository.list_findings(
        task_id=query.task_id,
        head_sha=query.head_sha,
        state=query.state,
        finding_class=query.finding_class,
        severity=query.severity,
        current_head_only=query.current_head_only,
        project=query.project,
    )


def list_findings_for(task_id: str, *, project: str, head_sha: str = "",
                      state: str = "", finding_class: str = "",
                      severity: str = "", current_head_only: bool = False,
                      repository: ReviewVerdictRepository = default_review_verdict_repository
                      ) -> list[dict[str, Any]]:
    return list_findings(
        ListReviewFindingsQuery(
            task_id=task_id,
            project=project,
            head_sha=head_sha,
            state=state,
            finding_class=finding_class,
            severity=severity,
            current_head_only=current_head_only,
        ),
        repository=repository,
    )
