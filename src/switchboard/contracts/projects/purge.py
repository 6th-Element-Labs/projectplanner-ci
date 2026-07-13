"""Versioned guarded project-purge and cleanup-review contracts (ACCESS-24)."""
from __future__ import annotations

import re
from typing import Any, ClassVar, Literal

from pydantic import ConfigDict, Field, field_validator

from ..base import VersionedModel
from ..registry import register


PURGE_INTENT_COMMAND_SCHEMA = "switchboard.project_purge.intent_command.v1"
PURGE_VERIFY_COMMAND_SCHEMA = "switchboard.project_purge.verify_command.v1"
PURGE_EXECUTE_COMMAND_SCHEMA = "switchboard.project_purge.execute_command.v1"
CLEANUP_REVIEW_COMMAND_SCHEMA = "switchboard.project_cleanup.review_command.v1"
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class PurgeExportEvidence(VersionedModel):
    SCHEMA: ClassVar[str] = "switchboard.project_purge.export_evidence.v1"
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=SCHEMA, alias="schema")
    artifact_uri: str
    artifact_hash: str
    created_at: float = Field(gt=0)
    immutable: bool

    @field_validator("artifact_uri", mode="before")
    @classmethod
    def _uri(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("artifact_uri is required")
        return text

    @field_validator("artifact_hash", mode="before")
    @classmethod
    def _hash(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        if not SHA256_RE.fullmatch(text):
            raise ValueError("artifact_hash must be sha256:<64 lowercase hex>")
        return text


class CreatePurgeIntentCommand(VersionedModel):
    SCHEMA: ClassVar[str] = PURGE_INTENT_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=SCHEMA, alias="schema")
    project_id: str
    reason: str
    actor: str
    retention_days: int = Field(ge=1)
    typed_confirmation: str
    export: PurgeExportEvidence

    @field_validator("project_id", "reason", "actor", "typed_confirmation", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("value is required")
        return text


class VerifyPurgeIntentCommand(VersionedModel):
    SCHEMA: ClassVar[str] = PURGE_VERIFY_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=SCHEMA, alias="schema")
    project_id: str
    intent_id: str
    verifier: str
    typed_confirmation: str

    @field_validator("project_id", "intent_id", "verifier", "typed_confirmation", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("value is required")
        return text


class ExecutePurgeCommand(VersionedModel):
    SCHEMA: ClassVar[str] = PURGE_EXECUTE_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=SCHEMA, alias="schema")
    project_id: str
    intent_id: str
    actor: str
    explicit_authorization: str

    @field_validator("project_id", "intent_id", "actor", "explicit_authorization", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("value is required")
        return text


class RecordCleanupReviewCommand(VersionedModel):
    SCHEMA: ClassVar[str] = CLEANUP_REVIEW_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=SCHEMA, alias="schema")
    project_id: str
    decision: Literal["keep", "consolidate", "archive"]
    impact_report_receipt: dict[str, Any]
    approved_by: str
    approved_at: float
    rationale: str

    @field_validator("project_id", "approved_by", "rationale", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("value is required")
        return text


for _contract in (
        PurgeExportEvidence, CreatePurgeIntentCommand, VerifyPurgeIntentCommand,
        ExecutePurgeCommand, RecordCleanupReviewCommand):
    register(_contract)
