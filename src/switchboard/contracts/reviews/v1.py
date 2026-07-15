"""Versioned contracts for durable, SHA-fenced code-review verdicts (COORD-18)."""
from __future__ import annotations

import re
from typing import Any, ClassVar, Mapping

from pydantic import ConfigDict, Field, field_validator, model_validator

from ..base import VersionedModel
from ..registry import register


REVIEW_VERDICT_SCHEMA = "switchboard.review_verdict.v1"
REVIEW_FINDING_SCHEMA = "switchboard.review_finding.v1"
RECORD_REVIEW_VERDICT_COMMAND_SCHEMA = "switchboard.review_verdict.record_command.v1"
GET_REVIEW_VERDICT_QUERY_SCHEMA = "switchboard.review_verdict.get_query.v1"
LIST_REVIEW_FINDINGS_QUERY_SCHEMA = "switchboard.review_finding.list_query.v1"
RESOLVE_REVIEW_FINDING_COMMAND_SCHEMA = "switchboard.review_finding.resolve_command.v1"

REVIEW_STATUSES = frozenset({"pass", "changes_requested"})
REVIEW_MODES = frozenset({"standard", "adversarial"})
REVIEW_FINDING_CLASSES = frozenset({"auto", "escalate"})
REVIEW_FINDING_STATES = frozenset({"open", "fixed", "waived", "overridden"})
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LOCATION_RE = re.compile(r"^.+:\d+$")


class ReviewFinding(VersionedModel):
    """One actionable review finding with durable resolution metadata."""

    SCHEMA: ClassVar[str] = REVIEW_FINDING_SCHEMA
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    schema_id: str = Field(default=REVIEW_FINDING_SCHEMA, alias="schema")
    id: str
    location: str
    category: str
    severity: str
    invariant_violated: str
    repair_requirement: str
    finding_class: str = Field(alias="class")
    state: str = "open"
    resolved_by: str | None = None
    resolved_principal_id: str | None = None
    resolved_reason: str | None = None
    resolved_sha: str | None = None
    resolved_at: float | None = None

    @field_validator(
        "id", "location", "category", "severity", "invariant_violated",
        "repair_requirement", "finding_class", "state", mode="before",
    )
    @classmethod
    def _strip_required_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator(
        "resolved_by", "resolved_principal_id", "resolved_reason", "resolved_sha",
        mode="before",
    )
    @classmethod
    def _strip_optional_text(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("finding_class", "state", mode="after")
    @classmethod
    def _lower_enum(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def _validate_finding(self) -> "ReviewFinding":
        required = {
            "id": self.id,
            "location": self.location,
            "category": self.category,
            "severity": self.severity,
            "invariant_violated": self.invariant_violated,
            "repair_requirement": self.repair_requirement,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError("missing review finding field(s): " + ", ".join(missing))
        if not LOCATION_RE.match(self.location):
            raise ValueError("location must be file:line")
        if self.finding_class not in REVIEW_FINDING_CLASSES:
            raise ValueError("class must be auto or escalate")
        if self.state not in REVIEW_FINDING_STATES:
            raise ValueError("state must be open, fixed, waived, or overridden")
        legacy_resolution = (
            self.resolved_by, self.resolved_reason, self.resolved_sha,
        )
        authority_resolution = (
            self.resolved_principal_id, self.resolved_at,
        )
        if self.state == "open" and any((*legacy_resolution, *authority_resolution)):
            raise ValueError("open findings cannot carry resolution metadata")
        if self.state != "open":
            if not all(legacy_resolution):
                raise ValueError("resolved findings require resolved_by, resolved_reason, and resolved_sha")
            if not SHA_RE.match(self.resolved_sha or ""):
                raise ValueError("resolved_sha must be a 40-character lowercase git SHA")
        if self.state in {"waived", "overridden"} and not all(authority_resolution):
            raise ValueError(
                "waived/overridden findings require resolved_principal_id and resolved_at"
            )
        return self


class ResolveReviewFindingCommand(VersionedModel):
    """Authorized escape valve for one open finding at the exact current head."""

    SCHEMA: ClassVar[str] = RESOLVE_REVIEW_FINDING_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    schema_id: str = Field(default=RESOLVE_REVIEW_FINDING_COMMAND_SCHEMA, alias="schema")
    task_id: str
    head_sha: str
    finding_id: str
    state: str
    resolved_reason: str
    resolved_sha: str
    resolver_principal: str

    @field_validator(
        "task_id", "head_sha", "finding_id", "state", "resolved_reason",
        "resolved_sha", "resolver_principal", mode="before",
    )
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("state", mode="after")
    @classmethod
    def _lower_state(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def _validate_resolution(self) -> "ResolveReviewFindingCommand":
        required = {
            "task_id": self.task_id,
            "head_sha": self.head_sha,
            "finding_id": self.finding_id,
            "state": self.state,
            "resolved_reason": self.resolved_reason,
            "resolved_sha": self.resolved_sha,
            "resolver_principal": self.resolver_principal,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError("missing review resolution field(s): " + ", ".join(missing))
        if self.state not in {"waived", "overridden"}:
            raise ValueError("state must be waived or overridden")
        if not SHA_RE.match(self.head_sha):
            raise ValueError("head_sha must be a 40-character lowercase git SHA")
        if not SHA_RE.match(self.resolved_sha):
            raise ValueError("resolved_sha must be a 40-character lowercase git SHA")
        if self.resolved_sha != self.head_sha:
            raise ValueError("resolved_sha must match the exact reviewed head_sha")
        return self

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ResolveReviewFindingCommand":
        return cls.model_validate(dict(value or {}))

    def to_repository_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "head_sha": self.head_sha,
            "finding_id": self.finding_id,
            "state": self.state,
            "resolved_reason": self.resolved_reason,
            "resolved_sha": self.resolved_sha,
            "resolver_principal": self.resolver_principal,
        }


class ReviewVerdict(VersionedModel):
    """Persisted review judgment for exactly one task PR head."""

    SCHEMA: ClassVar[str] = REVIEW_VERDICT_SCHEMA
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    schema_id: str = Field(default=REVIEW_VERDICT_SCHEMA, alias="schema")
    verdict_id: str
    task_id: str
    pr_url: str
    head_sha: str
    reviewer_principal: str
    reviewer_principal_id: str | None = None
    review_mode: str = "standard"
    status: str
    created_at: float
    findings: tuple[ReviewFinding, ...] = ()
    finding_count: int = 0
    open_finding_count: int = 0
    valid_for_current_head: bool = True
    invalidated_by_head_sha: str | None = None
    source: str = "review_command"

    @field_validator(
        "verdict_id", "task_id", "pr_url", "head_sha", "reviewer_principal",
        "review_mode", "status", "source", mode="before",
    )
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("reviewer_principal_id", mode="before")
    @classmethod
    def _strip_principal_id(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("review_mode", "status", mode="after")
    @classmethod
    def _lower_status(cls, value: str) -> str:
        return value.lower()


class RecordReviewVerdictCommand(VersionedModel):
    """Transport-neutral command written by the independent reviewer principal."""

    SCHEMA: ClassVar[str] = RECORD_REVIEW_VERDICT_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    schema_id: str = Field(default=RECORD_REVIEW_VERDICT_COMMAND_SCHEMA, alias="schema")
    task_id: str
    pr_url: str
    head_sha: str
    reviewer_principal: str
    review_mode: str = "standard"
    status: str
    findings: tuple[ReviewFinding, ...] = ()

    @field_validator(
        "task_id", "pr_url", "head_sha", "reviewer_principal", "review_mode", "status",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("review_mode", "status", mode="after")
    @classmethod
    def _lower_status(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def _validate_verdict(self) -> "RecordReviewVerdictCommand":
        if not all((self.task_id, self.pr_url, self.head_sha, self.reviewer_principal)):
            raise ValueError("task_id, pr_url, head_sha, and reviewer_principal are required")
        if not (self.pr_url.startswith("https://") or self.pr_url.startswith("http://")):
            raise ValueError("pr_url must be an absolute HTTP(S) URL")
        if not SHA_RE.match(self.head_sha):
            raise ValueError("head_sha must be a 40-character lowercase git SHA")
        if self.status not in REVIEW_STATUSES:
            raise ValueError("status must be pass or changes_requested")
        if self.review_mode not in REVIEW_MODES:
            raise ValueError("review_mode must be standard or adversarial")
        ids = [finding.id for finding in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("finding ids must be unique inside one verdict")
        open_count = sum(1 for finding in self.findings if finding.state == "open")
        if self.status == "pass" and open_count:
            raise ValueError("pass verdict cannot contain open findings")
        if self.status == "changes_requested" and not open_count:
            raise ValueError("changes_requested verdict requires at least one open finding")
        return self

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RecordReviewVerdictCommand":
        return cls.model_validate(dict(value or {}))

    def to_repository_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "pr_url": self.pr_url,
            "head_sha": self.head_sha,
            "reviewer_principal": self.reviewer_principal,
            "review_mode": self.review_mode,
            "status": self.status,
            "findings": [finding.model_dump(by_alias=True) for finding in self.findings],
        }


class GetReviewVerdictQuery(VersionedModel):
    SCHEMA: ClassVar[str] = GET_REVIEW_VERDICT_QUERY_SCHEMA
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    schema_id: str = Field(default=GET_REVIEW_VERDICT_QUERY_SCHEMA, alias="schema")
    task_id: str
    project: str
    head_sha: str = ""

    @field_validator("task_id", "project", "head_sha", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()


class ListReviewFindingsQuery(GetReviewVerdictQuery):
    SCHEMA: ClassVar[str] = LIST_REVIEW_FINDINGS_QUERY_SCHEMA
    schema_id: str = Field(default=LIST_REVIEW_FINDINGS_QUERY_SCHEMA, alias="schema")
    state: str = ""
    finding_class: str = Field(default="", alias="class")
    severity: str = ""
    current_head_only: bool = False

    @field_validator("state", "finding_class", "severity", mode="before")
    @classmethod
    def _strip_filter(cls, value: Any) -> str:
        return str(value or "").strip().lower()


for _model in (
    ReviewFinding,
    ReviewVerdict,
    RecordReviewVerdictCommand,
    ResolveReviewFindingCommand,
    GetReviewVerdictQuery,
    ListReviewFindingsQuery,
):
    register(_model)
