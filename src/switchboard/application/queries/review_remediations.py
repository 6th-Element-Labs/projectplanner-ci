"""Read-side queries for COORD-20 review-remediation state and proof metrics."""
from __future__ import annotations

from switchboard.storage.repositories.review_remediations import (
    ReviewRemediationRepository,
    default_review_remediation_repository,
)


def get(remediation_id: str, *, project: str,
        repository: ReviewRemediationRepository = default_review_remediation_repository):
    return repository.get(remediation_id, project=project)


def list_for(*, project: str, task_id: str = "", status: str = "",
             repository: ReviewRemediationRepository = default_review_remediation_repository):
    return repository.list(task_id=task_id, status=status, project=project)


def metrics_for(*, project: str, task_id: str = "",
                repository: ReviewRemediationRepository = default_review_remediation_repository):
    return repository.metrics(task_id=task_id, project=project)
