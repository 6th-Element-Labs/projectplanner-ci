"""Task command/query contracts — ``switchboard.task.*.v1``."""
from __future__ import annotations

from typing import Any, ClassVar, Mapping

from pydantic import ConfigDict, Field, field_validator

from ..base import VersionedModel, normalize_dependency_ids
from ..registry import register

CREATE_TASK_COMMAND_SCHEMA = "switchboard.task.create_command.v1"
UPDATE_TASK_COMMAND_SCHEMA = "switchboard.task.update_command.v1"
GET_TASK_QUERY_SCHEMA = "switchboard.task.get_query.v1"

UPDATE_TASK_FIELDS: tuple[str, ...] = (
    "title", "description", "owner_org", "owner_person_or_role", "assignee",
    "phase", "status", "effort_days", "duration_days", "start_date",
    "finish_date", "risk_level", "is_blocking", "sort_order",
    "entry_criteria", "exit_criteria", "deliverable",
)

_CLEAR_DEPENDS_ON = frozenset({"none", "clear", "[]"})
_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})


def coerce_is_blocking(value: Any) -> bool:
    """Coerce REST/MCP ``is_blocking`` inputs to a bool the store can persist."""
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_TOKENS
    return bool(value)


def normalize_depends_on_replacement(value: Any) -> tuple[str, ...]:
    """Return the replacement dependency edge list for an update."""
    if isinstance(value, str) and value.strip().lower() in _CLEAR_DEPENDS_ON:
        return ()
    return normalize_dependency_ids(value)


class CreateTaskCommand(VersionedModel):
    """Transport-neutral input for creating one task."""

    SCHEMA: ClassVar[str] = CREATE_TASK_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=CREATE_TASK_COMMAND_SCHEMA, alias="schema")
    workstream_id: str
    title: str
    description: str | None = None
    workstream_name: str | None = None
    owner_org: str | None = None
    owner_person_or_role: str | None = None
    assignee: str | None = None
    phase: str | None = None
    status: str | None = None
    effort_days: float | None = None
    duration_days: float | None = None
    start_date: str | None = None
    finish_date: str | None = None
    depends_on: tuple[str, ...] = ()
    entry_criteria: str | None = None
    exit_criteria: str | None = None
    deliverable: str | None = None
    risk_level: str | None = None
    is_blocking: bool = False

    @field_validator("workstream_id", "title", mode="before")
    @classmethod
    def _strip_required_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("depends_on", mode="before")
    @classmethod
    def _normalize_depends_on(cls, value: Any) -> tuple[str, ...]:
        return normalize_dependency_ids(value)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CreateTaskCommand:
        data = dict(value or {})
        data.pop("schema", None)
        return cls.model_validate(data)

    def to_store_data(self) -> dict[str, Any]:
        return {
            "workstream_id": self.workstream_id,
            "title": self.title,
            "description": self.description,
            "workstream_name": self.workstream_name,
            "owner_org": self.owner_org,
            "owner_person_or_role": self.owner_person_or_role,
            "assignee": self.assignee,
            "phase": self.phase,
            "status": self.status,
            "effort_days": self.effort_days,
            "duration_days": self.duration_days,
            "start_date": self.start_date,
            "finish_date": self.finish_date,
            "depends_on": list(self.depends_on),
            "entry_criteria": self.entry_criteria,
            "exit_criteria": self.exit_criteria,
            "deliverable": self.deliverable,
            "risk_level": self.risk_level,
            "is_blocking": self.is_blocking,
        }


class GetTaskQuery(VersionedModel):
    """Transport-neutral input for reading one project-scoped task."""

    SCHEMA: ClassVar[str] = GET_TASK_QUERY_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=GET_TASK_QUERY_SCHEMA, alias="schema")
    task_id: str
    project: str

    @field_validator("task_id", "project", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @classmethod
    def from_inputs(cls, task_id: Any, *, project: Any) -> GetTaskQuery:
        return cls(task_id=task_id, project=project)


class UpdateTaskCommand(VersionedModel):
    """Transport-neutral input for a sparse task update."""

    SCHEMA: ClassVar[str] = UPDATE_TASK_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=UPDATE_TASK_COMMAND_SCHEMA, alias="schema")
    task_id: str
    fields: dict[str, Any] = Field(default_factory=dict)
    depends_on: tuple[str, ...] | None = None

    @field_validator("task_id", mode="before")
    @classmethod
    def _strip_task_id(cls, value: Any) -> str:
        return str(value or "").strip()

    @classmethod
    def from_mapping(cls, task_id: Any, value: Mapping[str, Any]) -> UpdateTaskCommand:
        data = dict(value or {})
        data.pop("schema", None)
        fields: dict[str, Any] = {}
        for key in UPDATE_TASK_FIELDS:
            if key in data:
                fields[key] = (coerce_is_blocking(data[key])
                               if key == "is_blocking" else data[key])
        depends_on = (normalize_depends_on_replacement(data["depends_on"])
                      if "depends_on" in data else None)
        return cls(task_id=str(task_id or "").strip(), fields=fields,
                   depends_on=depends_on)

    def to_store_fields(self) -> dict[str, Any]:
        fields = dict(self.fields)
        if self.depends_on is not None:
            fields["depends_on"] = list(self.depends_on)
        return fields


register(CreateTaskCommand)
register(UpdateTaskCommand)
register(GetTaskQuery)
