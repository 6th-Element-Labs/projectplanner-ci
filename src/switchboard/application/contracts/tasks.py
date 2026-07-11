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
