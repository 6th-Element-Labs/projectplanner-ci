"""Wire authenticated Human requests into the Mission Bot v4 journal.

``agent_requires_human`` fences the runner and parks the board, but the v4
pager reads only the mission journal: without a HUMAN transition the fenced
runner's terminal receipt looks like ordinary runner loss and the pager boots
a replacement over the open Human request (BUG-250).  The request side flips
the mission to HUMAN at promotion time; the answer side is deliberately
pull-based — ``run_v4_tick`` reconciles against the durable attention request,
so a dropped delivery can never strand a HUMAN mission.  Neither side
interprets the request: they copy persisted facts, exactly like the GitHub
and capacity projectors.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from switchboard.storage.repositories.mission_journal import (
    MissionJournalError,
    MissionJournalRepository,
    default_mission_journal_repository,
)

#: Attention states that carry a recorded human decision.  pending still waits;
#: failed/expired/cancelled/orphaned died unanswered — the mission deliberately
#: stays HUMAN so the board keeps telling the truth that a human is still owed.
ANSWERED_ATTENTION_STATES = frozenset({
    "decision_recorded", "delivering", "resolved",
})

RequestReader = Callable[..., dict]


def _default_get_request(request_id: str, *, project: str) -> dict:
    from switchboard.storage.repositories import attention as attention_repo

    try:
        with attention_repo._conn(project) as c:
            return attention_repo.get_attention_request_in(
                c, request_id, project=project)
    except attention_repo.AttentionStoreError:
        return {}


def _flip_state(repository: MissionJournalRepository, task_id: str, *,
                project: str, from_state: str, to_state: str,
                human_request_id: str = "") -> dict[str, Any]:
    """Move the item between exactly two states, tolerating one version race."""
    for _ in range(2):
        item = repository.get_item(task_id, project=project)
        if item is None or str(item.get("state")) != from_state:
            return item or {}
        try:
            return repository.update_item(
                task_id, project=project, state=to_state,
                requested_role=str(item["requested_role"]),
                expected_version=int(item["version"]),
                human_request_id=human_request_id,
            )
        except MissionJournalError as exc:
            if exc.code != "stale_generation":
                raise
    return repository.get_item(task_id, project=project) or {}


def record_human_requested(
    *, project: str, task_id: str, request_id: str, reason: str = "",
    repository: MissionJournalRepository = default_mission_journal_repository,
) -> dict[str, Any]:
    """Append one human_requested fact and park the mission on HUMAN."""
    request_id = str(request_id or "").strip()
    if not request_id:
        return {"recorded": False, "reason": "request_id_missing"}
    item = repository.get_item(task_id, project=project)
    if item is None:
        return {"recorded": False, "reason": "mission_not_found"}
    if str(item.get("state")) == "DONE":
        return {"recorded": False, "reason": "mission_terminal"}
    event = repository.append_event(
        task_id,
        project=project,
        event_type="human_requested",
        source_plane="coordination",
        idempotency_key=f"human_requested:{request_id}",
        external_ref=request_id,
        payload={"request_id": request_id, "reason": str(reason or "")},
    )
    updated = _flip_state(
        repository, task_id, project=project, from_state=str(item["state"]),
        to_state="HUMAN", human_request_id=request_id,
    )
    return {
        "recorded": True,
        "event_created": bool(event.get("created", True)),
        "state": str(updated.get("state") or ""),
        "human_request_id": str(updated.get("human_request_id") or ""),
    }


def reconcile_human_answer(
    *, project: str, task_id: str,
    repository: MissionJournalRepository = default_mission_journal_repository,
    get_request: RequestReader | None = None,
) -> dict[str, Any]:
    """Copy an answered attention request into the journal and resume ACTIVE."""
    item = repository.get_item(task_id, project=project)
    if item is None or str(item.get("state")) != "HUMAN":
        return {"action": "ignored", "reason": "not_human"}
    request_id = str(item.get("human_request_id") or "").strip()
    if not request_id:
        # HUMAN without a request pointer cannot self-answer; leaving it is the
        # visible red signal, not a silent fallback.
        return {"action": "ignored", "reason": "human_request_id_missing"}
    reader = get_request or _default_get_request
    request = reader(request_id, project=project) or {}
    status = str(request.get("status") or "").strip().lower()
    if status not in ANSWERED_ATTENTION_STATES:
        return {"action": "waiting", "status": status or "unknown"}
    event = repository.append_event(
        task_id,
        project=project,
        event_type="human_answered",
        source_plane="coordination",
        idempotency_key=f"human_answered:{request_id}",
        external_ref=request_id,
        payload={"request_id": request_id, "status": status},
    )
    updated = _flip_state(
        repository, task_id, project=project, from_state="HUMAN",
        to_state="ACTIVE",
    )
    return {
        "action": "human_answered",
        "status": status,
        "event_created": bool(event.get("created", True)),
        "state": str(updated.get("state") or ""),
    }
