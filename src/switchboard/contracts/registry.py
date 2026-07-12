"""In-process registry of versioned JSON Schemas emitted by switchboard.contracts."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

_SCHEMAS: dict[str, dict[str, Any]] = {}


def register(model: type[BaseModel]) -> None:
    """Record a model's JSON Schema under its ``SCHEMA`` class constant."""
    schema_id = getattr(model, "SCHEMA", None)
    if not schema_id:
        return
    payload = model.model_json_schema()
    payload["$id"] = schema_id
    _SCHEMAS[schema_id] = payload


def get_schema(schema_id: str) -> dict[str, Any] | None:
    return _SCHEMAS.get(schema_id)


def list_schemas() -> dict[str, dict[str, Any]]:
    return dict(_SCHEMAS)
