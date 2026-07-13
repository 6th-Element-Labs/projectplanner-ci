"""Versioned project archive/restore command contracts (ACCESS-20)."""
from __future__ import annotations

from typing import Any, ClassVar

from pydantic import ConfigDict, Field, field_validator

from ..base import VersionedModel
from ..registry import register


ARCHIVE_PROJECT_COMMAND_SCHEMA = "switchboard.project.archive_command.v1"
RESTORE_PROJECT_COMMAND_SCHEMA = "switchboard.project.restore_command.v1"


class ArchiveProjectCommand(VersionedModel):
    SCHEMA: ClassVar[str] = ARCHIVE_PROJECT_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=ARCHIVE_PROJECT_COMMAND_SCHEMA, alias="schema")
    project_id: str
    reason: str
    impact_report_receipt: dict[str, Any]
    actor: str = "system"

    @field_validator("project_id", "reason", "actor", mode="before")
    @classmethod
    def _required_text(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("value is required")
        return text

    @field_validator("impact_report_receipt", mode="before")
    @classmethod
    def _receipt_required(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or not value:
            raise ValueError("impact_report_receipt is required")
        return dict(value)


class RestoreProjectCommand(VersionedModel):
    SCHEMA: ClassVar[str] = RESTORE_PROJECT_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=RESTORE_PROJECT_COMMAND_SCHEMA, alias="schema")
    project_id: str
    reason: str
    actor: str = "system"

    @field_validator("project_id", "reason", "actor", mode="before")
    @classmethod
    def _required_text(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("value is required")
        return text


register(ArchiveProjectCommand)
register(RestoreProjectCommand)
