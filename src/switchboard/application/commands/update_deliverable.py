"""Shared partial-update command for deliverable contracts."""
from __future__ import annotations

from typing import Any

from switchboard.storage.repositories.deliverables import (
    update_deliverable as repo_update_deliverable,
)


def execute_mapping_result(
        deliverable_id: str, data: dict[str, Any], *, actor: str,
        project: str) -> dict[str, Any]:
    """Update the explicitly supplied fields on one deliverable."""
    return repo_update_deliverable(
        deliverable_id, data or {}, actor=actor, project=project)
