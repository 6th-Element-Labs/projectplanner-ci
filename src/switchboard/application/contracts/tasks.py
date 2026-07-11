"""Typed task contracts shared by the REST and MCP adapters."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


def normalize_dependency_ids(value: Any) -> tuple[str, ...]:
    """Return dependency ids in the canonical, ordered, de-duplicated form."""
    if value is None:
        values: Iterable[Any] = ()
    elif isinstance(value, str):
        values = value.replace("\n", ",").replace(" ", ",").split(",")
    elif isinstance(value, Iterable):
        values = value
    else:
        values = (value,)

    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        task_id = str(raw or "").strip().upper()
        if task_id and task_id not in seen:
            seen.add(task_id)
            result.append(task_id)
    return tuple(result)


@dataclass(frozen=True)
class CreateTaskCommand:
    """The transport-neutral input for creating one task."""

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

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CreateTaskCommand":
        data = dict(value or {})
        return cls(
            workstream_id=str(data.get("workstream_id") or "").strip(),
            title=str(data.get("title") or "").strip(),
            description=data.get("description") or None,
            workstream_name=data.get("workstream_name") or None,
            owner_org=data.get("owner_org") or None,
            owner_person_or_role=data.get("owner_person_or_role") or None,
            assignee=data.get("assignee") or None,
            phase=data.get("phase") or None,
            status=data.get("status") or None,
            effort_days=data.get("effort_days"),
            duration_days=data.get("duration_days"),
            start_date=data.get("start_date") or None,
            finish_date=data.get("finish_date") or None,
            depends_on=normalize_dependency_ids(data.get("depends_on")),
            entry_criteria=data.get("entry_criteria") or None,
            exit_criteria=data.get("exit_criteria") or None,
            deliverable=data.get("deliverable") or None,
            risk_level=data.get("risk_level") or None,
            is_blocking=bool(data.get("is_blocking")),
        )

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


@dataclass(frozen=True)
class GetTaskQuery:
    """The transport-neutral input for reading one project-scoped task."""

    task_id: str
    project: str

    @classmethod
    def from_inputs(cls, task_id: Any, *, project: Any) -> "GetTaskQuery":
        # store.get_task resolves task ids case-insensitively, so only strip
        # surrounding whitespace here and let the store own canonical casing.
        return cls(
            task_id=str(task_id or "").strip(),
            project=str(project or "").strip(),
        )


# The task fields the update command accepts. Mirrors store.EDITABLE minus the
# dependency edge (handled separately so unknown ids can fail closed) and the
# columns the write adapters never expose. Keeping it here means REST and MCP
# agree on exactly which keys are writable.
UPDATE_TASK_FIELDS: tuple[str, ...] = (
    "title", "description", "owner_org", "owner_person_or_role", "assignee",
    "phase", "status", "effort_days", "duration_days", "start_date",
    "finish_date", "risk_level", "is_blocking", "sort_order",
    "entry_criteria", "exit_criteria", "deliverable",
)

_CLEAR_DEPENDS_ON = ("none", "clear", "[]")
_TRUE_TOKENS = ("1", "true", "yes", "on")


def coerce_is_blocking(value: Any) -> bool:
    """Coerce a REST/MCP is_blocking input to a bool the store can persist.

    MCP passes 'true'/'false' strings; REST passes JSON booleans. A bare
    ``bool(value)`` would make the string 'false' truthy, so decode string
    tokens explicitly and fall back to truthiness for real booleans/ints.
    """
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_TOKENS
    return bool(value)


def normalize_depends_on_replacement(value: Any) -> tuple[str, ...]:
    """Return the replacement dependency edge list for an update.

    The clear sentinels ('none'/'clear'/'[]') and an empty list all mean
    "remove every dependency"; anything else is canonicalized like create.
    """
    if isinstance(value, str) and value.strip().lower() in _CLEAR_DEPENDS_ON:
        return ()
    return normalize_dependency_ids(value)


@dataclass(frozen=True)
class UpdateTaskCommand:
    """The transport-neutral input for a sparse task update.

    ``fields`` holds only the columns the caller wants to change (already
    coerced), so an absent key leaves that column untouched. ``depends_on`` is
    ``None`` when the caller did not touch the edge list, or the canonical
    replacement tuple (possibly empty) when it did.
    """

    task_id: str
    fields: Mapping[str, Any]
    depends_on: tuple[str, ...] | None = None

    @classmethod
    def from_mapping(cls, task_id: Any, value: Mapping[str, Any]) -> "UpdateTaskCommand":
        data = dict(value or {})
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
        """The full field map to hand the store, including the edge list."""
        fields = dict(self.fields)
        if self.depends_on is not None:
            fields["depends_on"] = list(self.depends_on)
        return fields
