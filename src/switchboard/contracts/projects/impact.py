"""Versioned read model for project dependency and sprawl impact audits."""
from __future__ import annotations

import hashlib
import json
from typing import Any, ClassVar

from pydantic import ConfigDict, Field, field_validator

from ..base import VersionedModel
from ..registry import register


PROJECT_IMPACT_REPORT_SCHEMA = "switchboard.project_impact_report.v1"
PROJECT_IMPACT_RECEIPT_SCHEMA = "switchboard.project_impact_receipt.v1"


class ProjectImpactReceipt(VersionedModel):
    """Content-addressed proof that archive reviewed the current impact snapshot."""

    SCHEMA: ClassVar[str] = PROJECT_IMPACT_RECEIPT_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=PROJECT_IMPACT_RECEIPT_SCHEMA, alias="schema")
    project_id: str
    report_schema: str = PROJECT_IMPACT_REPORT_SCHEMA
    report_hash: str

    @field_validator("project_id", "report_schema", "report_hash", mode="before")
    @classmethod
    def _required_text(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("value is required")
        return text


def build_impact_receipt(report: dict[str, Any]) -> dict[str, Any]:
    """Hash the report body while excluding its self-referential receipt field."""
    body = dict(report or {})
    body.pop("receipt", None)
    body.setdefault("schema", PROJECT_IMPACT_REPORT_SCHEMA)
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    receipt = ProjectImpactReceipt(
        project_id=str(body.get("project_id") or ""),
        report_schema=str(body.get("schema") or PROJECT_IMPACT_REPORT_SCHEMA),
        report_hash="sha256:" + hashlib.sha256(encoded).hexdigest(),
    )
    return receipt.model_dump(by_alias=True)


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
    receipt: dict[str, Any]

    @field_validator("project_id", mode="before")
    @classmethod
    def _project_id_required(cls, value: Any) -> str:
        project_id = str(value or "").strip()
        if not project_id:
            raise ValueError("project_id required")
        return project_id


register(ProjectImpactReceipt)
register(ProjectImpactReport)
