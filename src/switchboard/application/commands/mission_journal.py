"""Application commands for the Mission Bot v4 durable journal."""
from __future__ import annotations

from typing import Any, Mapping

from switchboard.storage.repositories.mission_journal import (
    MissionJournalError,
    MissionJournalRepository,
    default_mission_journal_repository,
)


def create_mission(
    task_id: str, *, project: str, requested_role: str = "implementation",
    repository: MissionJournalRepository = default_mission_journal_repository,
) -> dict[str, Any]:
    item = repository.ensure_item(
        task_id, project=project, requested_role=requested_role, state="ACTIVE",
    )
    event = repository.append_event(
        task_id, project=project, event_type="mission_started",
        source_plane="coordination", idempotency_key=f"mission_started:{task_id}",
    )
    return {"mission": repository.get_item(task_id, project=project) or item, "event": event}


def append_material_event(
    task_id: str, *, project: str, event_type: str, source_plane: str,
    idempotency_key: str, payload: Mapping[str, Any] | None = None,
    repository: MissionJournalRepository = default_mission_journal_repository,
    **identity: Any,
) -> dict[str, Any]:
    return repository.append_event(
        task_id, project=project, event_type=event_type, source_plane=source_plane,
        idempotency_key=idempotency_key, payload=payload, **identity,
    )


def transition_mission(
    task_id: str, *, project: str, state: str, requested_role: str,
    expected_version: int, repository: MissionJournalRepository = default_mission_journal_repository,
    **fields: Any,
) -> dict[str, Any]:
    return repository.update_item(
        task_id, project=project, state=state, requested_role=requested_role,
        expected_version=expected_version, **fields,
    )


__all__ = [
    "MissionJournalError", "append_material_event", "create_mission", "transition_mission",
]
