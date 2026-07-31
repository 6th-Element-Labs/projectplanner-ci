"""Narrow commands for the staged Mission Bot journal protocol.

The journal remains passive except for ``yield_mission``: that command records
one exact-execution handoff, then asks Capacity to expire that same renewable
lease.  It never stops a process directly and has no GitHub, Human, task-status,
or coordinator effects.
"""
from __future__ import annotations

from typing import Any, Mapping

from switchboard.storage.repositories import runner as runner_repository
from switchboard.storage.repositories.mission_journal import (
    MissionJournalError,
    MissionJournalRepository,
    default_mission_journal_repository,
)


def create_mission(
    task_id: str,
    *,
    project: str,
    requested_role: str = "implementation",
    repository: MissionJournalRepository = default_mission_journal_repository,
) -> dict[str, Any]:
    return repository.create_mission(
        task_id, project=project, requested_role=requested_role,
    )


def append_material_event(
    task_id: str,
    *,
    project: str,
    event_type: str,
    source_plane: str,
    idempotency_key: str,
    payload: Mapping[str, Any] | None = None,
    repository: MissionJournalRepository = default_mission_journal_repository,
    **identity: Any,
) -> dict[str, Any]:
    return repository.append_event(
        task_id,
        project=project,
        event_type=event_type,
        source_plane=source_plane,
        idempotency_key=idempotency_key,
        payload=payload,
        **identity,
    )


def transition_mission(
    task_id: str,
    *,
    project: str,
    state: str,
    requested_role: str,
    expected_version: int,
    repository: MissionJournalRepository = default_mission_journal_repository,
    **fields: Any,
) -> dict[str, Any]:
    return repository.update_item(
        task_id,
        project=project,
        state=state,
        requested_role=requested_role,
        expected_version=expected_version,
        **fields,
    )


def yield_mission(
    task_id: str,
    *,
    project: str,
    execution_id: str,
    generation: int,
    observed_through: int,
    outcome: str,
    requested_role: str,
    actor: str,
    head_sha: str = "",
    repository: MissionJournalRepository = default_mission_journal_repository,
) -> dict[str, Any]:
    result = repository.yield_execution(
        task_id,
        project=project,
        execution_id=execution_id,
        generation=generation,
        observed_through=observed_through,
        outcome=outcome,
        requested_role=requested_role,
        actor=actor,
        head_sha=head_sha,
    )
    identity = dict(result.pop("execution_identity"))
    # Capacity surrender is independently idempotent. Retry it even when the
    # journal event already exists; otherwise a crash between the two writes
    # could permanently strand a live execution behind a "successful" replay.
    surrender = runner_repository.make_runner_lease_due(
        identity["runner_session_id"],
        reason=f"mission yielded: {outcome}",
        authority="completion_owner",
        actor=actor,
        project=project,
        expected_identity=identity,
    )
    if not surrender.get("updated"):
        raise MissionJournalError(
            "execution_surrender_failed",
            str(surrender.get("error") or "exact execution surrender failed"),
        )
    result["surrender"] = surrender
    result["surrender_requested"] = True
    return result


__all__ = [
    "MissionJournalError",
    "append_material_event",
    "create_mission",
    "transition_mission",
    "yield_mission",
]
