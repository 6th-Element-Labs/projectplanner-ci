"""Versioned project consolidation workflow contracts (ACCESS-23)."""
from __future__ import annotations

import hashlib
import json
from typing import Any, ClassVar, Literal, Optional

from pydantic import ConfigDict, Field, field_validator, model_validator

from ..base import VersionedModel
from ..registry import register


PROJECT_CONSOLIDATION_PLAN_COMMAND_SCHEMA = (
    "switchboard.project_consolidation.plan_command.v1"
)
PROJECT_CONSOLIDATION_PLAN_SCHEMA = "switchboard.project_consolidation.plan.v1"
PROJECT_CONSOLIDATION_PLAN_RECEIPT_SCHEMA = (
    "switchboard.project_consolidation.plan_receipt.v1"
)
PROJECT_CONSOLIDATION_APPLY_COMMAND_SCHEMA = (
    "switchboard.project_consolidation.apply_command.v1"
)
PROJECT_CONSOLIDATION_ROLLBACK_COMMAND_SCHEMA = (
    "switchboard.project_consolidation.rollback_command.v1"
)


class ConsolidationApproval(VersionedModel):
    """Explicit operator classification; agents cannot infer consolidation approval."""

    SCHEMA: ClassVar[str] = "switchboard.project_consolidation.approval.v1"
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=SCHEMA, alias="schema")
    decision: Literal["consolidate"]
    approved_by: str
    approved_at: float
    rationale: str

    @field_validator("approved_by", "rationale", mode="before")
    @classmethod
    def _required_text(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("value is required")
        return text

    @field_validator("approved_at", mode="before")
    @classmethod
    def _positive_time(cls, value: Any) -> float:
        timestamp = float(value or 0)
        if timestamp <= 0:
            raise ValueError("approved_at must be a positive unix timestamp")
        return timestamp


class PlanProjectConsolidationCommand(VersionedModel):
    SCHEMA: ClassVar[str] = PROJECT_CONSOLIDATION_PLAN_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=SCHEMA, alias="schema")
    source_project_id: str
    replacement_project_id: str
    replacement_board_id: Optional[str] = None
    replacement_mission_id: Optional[str] = None
    replacement_deliverable_id: Optional[str] = None
    safe_routing_keys: tuple[str, ...] = ()
    reason: str
    actor: str = "system"
    approval: ConsolidationApproval

    @field_validator("source_project_id", "replacement_project_id", "reason", "actor",
                     mode="before")
    @classmethod
    def _required_text(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("value is required")
        return text

    @field_validator("replacement_board_id", "replacement_mission_id",
                     "replacement_deliverable_id", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> Optional[str]:
        text = str(value or "").strip()
        return text or None

    @field_validator("safe_routing_keys", mode="before")
    @classmethod
    def _routing_keys(cls, value: Any) -> tuple[str, ...]:
        if value in (None, ""):
            return ()
        if isinstance(value, str):
            value = [part.strip() for part in value.replace(",", " ").split()]
        return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))

    @model_validator(mode="after")
    def _different_projects(self):
        if self.source_project_id == self.replacement_project_id:
            raise ValueError("source and replacement projects must differ")
        if (self.replacement_board_id and self.replacement_mission_id
                and self.replacement_board_id != self.replacement_mission_id):
            raise ValueError("replacement_board_id and replacement_mission_id must agree")
        return self


class ApplyProjectConsolidationCommand(VersionedModel):
    SCHEMA: ClassVar[str] = PROJECT_CONSOLIDATION_APPLY_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=SCHEMA, alias="schema")
    plan: dict[str, Any]
    confirmation: str
    actor: str = "system"

    @field_validator("plan", mode="before")
    @classmethod
    def _plan_required(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or not value:
            raise ValueError("plan is required")
        return dict(value)

    @field_validator("confirmation", "actor", mode="before")
    @classmethod
    def _required_text(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("value is required")
        return text


class RollbackProjectConsolidationCommand(VersionedModel):
    SCHEMA: ClassVar[str] = PROJECT_CONSOLIDATION_ROLLBACK_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=SCHEMA, alias="schema")
    source_project_id: str
    consolidation_id: str
    reason: str
    actor: str = "system"

    @field_validator("source_project_id", "consolidation_id", "reason", "actor",
                     mode="before")
    @classmethod
    def _required_text(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("value is required")
        return text


def build_consolidation_plan_receipt(plan: dict[str, Any]) -> dict[str, Any]:
    body = dict(plan or {})
    body.pop("receipt", None)
    body.setdefault("schema", PROJECT_CONSOLIDATION_PLAN_SCHEMA)
    encoded = json.dumps(
        body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return {
        "schema": PROJECT_CONSOLIDATION_PLAN_RECEIPT_SCHEMA,
        "source_project_id": str(body.get("source_project_id") or ""),
        "replacement_project_id": str(body.get("replacement_project_id") or ""),
        "plan_hash": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }


for model in (
    ConsolidationApproval,
    PlanProjectConsolidationCommand,
    ApplyProjectConsolidationCommand,
    RollbackProjectConsolidationCommand,
):
    register(model)
