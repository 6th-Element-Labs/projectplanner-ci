"""Versioned contract primitives shared by REST, MCP, and events."""
from __future__ import annotations

from typing import Any, ClassVar, Iterable

from pydantic import BaseModel, ConfigDict, Field


SCHEMA_ID_PREFIX = "https://plan.taikunai.com/schemas/"


class VersionedModel(BaseModel):
    """Base for wire DTOs that carry a stable ``switchboard.*.vN`` schema id."""

    SCHEMA: ClassVar[str] = ""
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    schema_id: str = Field(
        default="",
        alias="schema",
        description="Contract schema id, e.g. switchboard.task.v1",
    )

    @classmethod
    def schema_name(cls) -> str:
        return cls.SCHEMA

    @property
    def schema(self) -> str:
        return self.schema_id

    def schema_uri(self) -> str:
        return f"{SCHEMA_ID_PREFIX}{self.schema_id.removeprefix('switchboard.')}"


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
