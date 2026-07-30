"""Project durable capacity launch failures into the Mission Bot v4 journal.

A wake that fails before Capacity creates a runner leaves no runner terminal
receipt and no GitHub fact, so none of the existing v4 wake edges fire.  This
command copies that already-persisted Capacity fact into the mission inbox as
one idempotent ``execution_ended`` event.  It is deliberately append-only: it
records the failure, never diagnoses it, never selects a role, and never calls
a start port.  The event carries no head_sha so the scoped worker's source_sha
fallback resolves the current default branch instead of re-pinning the fenced
stale base.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from switchboard.domain.coordination.wake_intents import genuine_wake_intents
from switchboard.storage.repositories.mission_journal import (
    MissionJournalRepository,
    default_mission_journal_repository,
)

WakeLister = Callable[..., list]


def _default_wake_lister(**kwargs: Any) -> list:
    from switchboard.storage.repositories import coordination

    return coordination.list_wake_intents(**kwargs)


def _execution_identity(wake: Mapping[str, Any]) -> tuple[str, int | None]:
    policy = wake.get("policy") or {}
    for surface in (policy.get("execution_assignment"), policy.get("lifecycle")):
        if isinstance(surface, Mapping) and surface.get("execution_id"):
            generation = surface.get("generation")
            return (
                str(surface["execution_id"]),
                int(generation) if generation is not None else None,
            )
    return "", None


def append_failed_wake_events(
    *, project: str, task_id: str,
    repository: MissionJournalRepository = default_mission_journal_repository,
    list_wakes: WakeLister | None = None,
) -> dict[str, Any]:
    """Append one ``execution_ended`` event per terminal pre-runner wake failure.

    Only wakes that (a) reached terminal ``failed`` status, (b) never produced a
    runner session, and (c) were requested at or after this mission's creation
    are this mission's capacity requests.  Replay is exact: the journal's
    idempotency key ``execution_ended:<wake_id>`` suppresses duplicates.
    """
    item = repository.get_item(task_id, project=project)
    if item is None:
        return {"action": "ignored", "reason": "mission_not_found", "events": []}
    mission_created_at = float(item.get("created_at") or 0.0)

    lister = list_wakes or _default_wake_lister
    wakes = genuine_wake_intents(lister(
        status="failed", task_id=task_id, project=project, include_archived=True,
    ))

    events: list[dict[str, Any]] = []
    for wake in wakes:
        if wake.get("runner_session_id"):
            continue  # a runner existed; the runner_ended projection owns it
        if float(wake.get("requested_at") or 0.0) < mission_created_at:
            continue
        wake_id = str(wake["wake_id"])
        execution_id, generation = _execution_identity(wake)
        result = wake.get("result") or {}
        # host_loss_recovery_exhausted stores its reason one level down.
        recovery = result.get("recovery") if isinstance(result, Mapping) else None
        recovery = recovery if isinstance(recovery, Mapping) else {}
        event = repository.append_event(
            task_id,
            project=project,
            event_type="execution_ended",
            source_plane="capacity",
            idempotency_key=f"execution_ended:{wake_id}",
            execution_id=execution_id or None,
            generation=generation,
            payload={
                "wake_id": wake_id,
                "status": str(wake.get("status") or "failed"),
                "reason": str(result.get("reason") or recovery.get("reason") or ""),
            },
        )
        events.append({
            "event_id": event.get("event_id"),
            "sequence": event.get("sequence"),
            "wake_id": wake_id,
            "created": bool(event.get("created", True)),
        })
    return {"action": "capacity_mission_events_projected", "events": events}
