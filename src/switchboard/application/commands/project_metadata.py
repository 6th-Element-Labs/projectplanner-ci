"""Safe ordinary project metadata updates shared by REST and MCP."""
from __future__ import annotations

from typing import Any, Mapping

from switchboard.contracts.projects.v2 import ProjectUpdateCommand
from switchboard.storage.repositories.protocols.access import AccessRepository


PROJECT_METADATA_UPDATE_SCHEMA = "switchboard.project_metadata_update.v1"
ORDINARY_METADATA_FIELDS = frozenset({"label", "pretitle", "purpose"})
TRUST_BOUNDARY_FIELDS = frozenset({"boundary", "visibility"})
SAFE_METADATA_FIELDS = ORDINARY_METADATA_FIELDS | TRUST_BOUNDARY_FIELDS


def execute(payload: Mapping[str, Any], *, actor: str,
            access_repository: AccessRepository) -> dict[str, Any]:
    """Apply only non-lifecycle metadata; trust-boundary fields fail closed."""
    data = dict(payload or {})
    project_id = str(data.get("project_id") or data.get("id") or "").strip()
    supplied = {
        key for key in data
        if key not in {"schema", "project_id", "id"} and data.get(key) is not None
    }
    unsafe = sorted(supplied - SAFE_METADATA_FIELDS)
    if unsafe:
        return {
            "schema": PROJECT_METADATA_UPDATE_SCHEMA,
            "error": "unsafe_project_metadata_fields",
            "message": "lifecycle, ownership, organization, and replacement fields require a system command",
            "fields": unsafe,
        }
    fields = {key: data[key] for key in SAFE_METADATA_FIELDS if key in data}
    if not project_id or not fields:
        return {
            "schema": PROJECT_METADATA_UPDATE_SCHEMA,
            "error": "invalid_project_metadata_update",
            "message": "project_id and at least one safe metadata field are required",
        }
    try:
        command = ProjectUpdateCommand.from_mapping({"project_id": project_id, **fields})
    except Exception as exc:  # noqa: BLE001
        return {
            "schema": PROJECT_METADATA_UPDATE_SCHEMA,
            "error": "invalid_project_metadata_update",
            "message": str(exc),
        }
    result = access_repository.update_project_metadata(command, actor=actor)
    if result.get("error"):
        return {"schema": PROJECT_METADATA_UPDATE_SCHEMA, **result}
    return {
        "schema": PROJECT_METADATA_UPDATE_SCHEMA,
        "project": result,
        "updated_fields": sorted(fields),
    }
