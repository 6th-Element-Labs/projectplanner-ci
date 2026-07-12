"""Project registry contracts — ``switchboard.project.v2``."""
from __future__ import annotations

from typing import Any, ClassVar, Mapping

from pydantic import ConfigDict, Field, field_validator

from ..base import VersionedModel
from ..registry import register
from switchboard.domain.projects.lifecycle import (
    default_lifecycle_status,
    normalize_lifecycle_status,
)

PROJECT_RECORD_SCHEMA = "switchboard.project.v2"
PROJECT_UPDATE_COMMAND_SCHEMA = "switchboard.project.update_command.v2"

_EDITABLE_ACCESS_FIELDS = (
    "org_id", "owner_user_id", "purpose", "boundary", "visibility",
)
_EDITABLE_PROJECT_FIELDS = ("label", "pretitle")


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class ProjectRecord(VersionedModel):
    """Canonical registry projection for one routable project board."""

    SCHEMA: ClassVar[str] = PROJECT_RECORD_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=PROJECT_RECORD_SCHEMA, alias="schema")
    id: str
    label: str
    pretitle: str = ""
    db_path: str | None = None
    seed_path: str | None = None
    created_at: float | None = None
    created_by: str | None = None
    updated_at: float | None = None
    updated_by: str | None = None
    org_id: str = ""
    owner_user_id: str = ""
    purpose: str = ""
    boundary: str = ""
    visibility: str | None = None
    lifecycle_status: str = default_lifecycle_status()
    archived_at: float | None = None
    archived_by: str | None = None
    archive_reason: str | None = None
    is_protected: bool = False
    is_system: bool = False
    replacement_project_id: str | None = None
    replacement_deliverable_id: str | None = None
    is_builtin: bool = False

    @field_validator("id", "label", mode="before")
    @classmethod
    def _strip_required_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("pretitle", mode="before")
    @classmethod
    def _blank_pretitle(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("org_id", "owner_user_id", "purpose", "boundary", mode="before")
    @classmethod
    def _blank_access_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("archived_by", "archive_reason", "replacement_project_id",
                     "replacement_deliverable_id", "updated_by", "created_by",
                     mode="before")
    @classmethod
    def _blank_optional_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("lifecycle_status", mode="before")
    @classmethod
    def _normalize_status(cls, value: Any) -> str:
        return normalize_lifecycle_status(value) or default_lifecycle_status()

    @field_validator("visibility", mode="before")
    @classmethod
    def _normalize_visibility(cls, value: Any) -> str | None:
        vis = str(value or "").strip().lower()
        if not vis:
            return None
        return vis if vis in {"private", "org"} else None

    @field_validator("is_protected", "is_system", "is_builtin", mode="before")
    @classmethod
    def _coerce_flags(cls, value: Any) -> bool:
        return _coerce_bool(value)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ProjectRecord:
        return cls.model_validate(dict(value or {}))

    def is_active(self) -> bool:
        return self.lifecycle_status == "active"


class ProjectUpdateCommand(VersionedModel):
    """Editable project metadata — project id remains immutable."""

    SCHEMA: ClassVar[str] = PROJECT_UPDATE_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=PROJECT_UPDATE_COMMAND_SCHEMA, alias="schema")
    project_id: str
    label: str | None = None
    pretitle: str | None = None
    org_id: str | None = None
    owner_user_id: str | None = None
    purpose: str | None = None
    boundary: str | None = None
    visibility: str | None = None
    lifecycle_status: str | None = None
    archive_reason: str | None = None
    replacement_project_id: str | None = None
    replacement_deliverable_id: str | None = None
    updated_by: str | None = None

    @field_validator("project_id", mode="before")
    @classmethod
    def _strip_project_id(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("visibility", mode="before")
    @classmethod
    def _normalize_visibility(cls, value: Any) -> str | None:
        if value is None:
            return None
        vis = str(value).strip().lower()
        if not vis:
            return None
        return vis if vis in {"private", "org"} else None

    @field_validator("lifecycle_status", mode="before")
    @classmethod
    def _normalize_status(cls, value: Any) -> str | None:
        if value is None:
            return None
        status = normalize_lifecycle_status(value)
        return status or None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ProjectUpdateCommand:
        data = dict(value or {})
        allowed = {
            "project_id", *_EDITABLE_PROJECT_FIELDS, *_EDITABLE_ACCESS_FIELDS,
            "lifecycle_status", "archive_reason", "replacement_project_id",
            "replacement_deliverable_id", "updated_by",
        }
        filtered = {key: data[key] for key in allowed if key in data}
        if "project_id" not in filtered and data.get("id"):
            filtered["project_id"] = data["id"]
        return cls.model_validate(filtered)

    def editable_fields(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key in (*_EDITABLE_PROJECT_FIELDS, *_EDITABLE_ACCESS_FIELDS,
                    "lifecycle_status", "archive_reason",
                    "replacement_project_id", "replacement_deliverable_id"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


register(ProjectRecord)
register(ProjectUpdateCommand)
