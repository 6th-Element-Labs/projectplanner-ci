"""Versioned read model for project dependency and sprawl impact audits."""
from __future__ import annotations

from typing import Any, ClassVar

from pydantic import ConfigDict, Field, field_validator

from ..base import VersionedModel
from ..registry import register


PROJECT_IMPACT_REPORT_SCHEMA = "switchboard.project_impact_report.v1"


class ProjectImpactReport(VersionedModel):
    """Deterministic, bounded, read-only project lifecycle impact report."""

    SCHEMA: ClassVar[str] = PROJECT_IMPACT_REPORT_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=PROJECT_IMPACT_REPORT_SCHEMA, alias="schema")
    project_id: str
    project: dict[str, Any]
    bounds: dict[str, Any]
    tasks: dict[str, Any]
    provenance: dict[str, Any]
    coordination: dict[str, Any]
    hosted_outcomes: dict[str, Any]
    cross_project_links: dict[str, Any]
    repo_ci_webhooks: dict[str, Any]
    access: dict[str, Any]
    communications: dict[str, Any]
    automation: dict[str, Any]
    activity: dict[str, Any]
    storage: dict[str, Any]
    blocking_findings: list[dict[str, Any]]
    recommendation: dict[str, Any]

    @field_validator("project_id", mode="before")
    @classmethod
    def _project_id_required(cls, value: Any) -> str:
        project_id = str(value or "").strip()
        if not project_id:
            raise ValueError("project_id required")
        return project_id


register(ProjectImpactReport)
