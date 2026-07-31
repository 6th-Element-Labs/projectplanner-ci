"""The fenced ADR-0008 W2 Mission Bot v4 pager.

The worker interprets no GitHub, CI, review, or merge facts.  It validates one
coordination-scope lease, reads the four-state journal and Capacity's canonical
liveness registry, then either waits or copies the persisted role into the
single ``start_task`` capacity door.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from switchboard.domain.mission_bot_v4 import decide_mission_transition
from switchboard.storage.repositories.mission_journal import (
    MissionJournalError,
    MissionJournalRepository,
    default_mission_journal_repository,
)


ReadMapping = Callable[..., Mapping[str, Any] | None]
StartTask = Callable[..., Mapping[str, Any]]


@dataclass(frozen=True)
class ScopedMissionWorkerPorts:
    """Explicit ports keep the pager independent of adapters and providers."""

    validate_scope: ReadMapping
    get_task: ReadMapping
    has_live_execution: Callable[..., bool]
    start_task: StartTask
    journal: MissionJournalRepository = default_mission_journal_repository


def _wait(reason: str, *, task_id: str, **details: Any) -> dict[str, Any]:
    return {
        "schema": "switchboard.mission_worker_tick.v4",
        "task_id": task_id,
        "action": "wait",
        "reason": reason,
        "mutations": 0,
        **details,
    }


def _start_was_admitted(receipt: Mapping[str, Any]) -> bool:
    """Recognize Task Execution admission/readback, never optimistic fallback."""
    if receipt.get("error") or receipt.get("refused"):
        return False
    if receipt.get("started") or receipt.get("attached"):
        return True
    return str(receipt.get("action") or "") in {
        "started", "starting", "attach", "attached",
    }


def tick_scoped_mission(
    task_id: str,
    *,
    project: str,
    scope_authority: Mapping[str, Any],
    actor: str,
    ports: ScopedMissionWorkerPorts,
) -> dict[str, Any]:
    """Run one fenced pager tick with at most one ``start_task`` call."""
    authority = ports.validate_scope(
        dict(scope_authority), project=project, task_project=project, task_id=task_id,
    ) or {}
    if not authority.get("allowed"):
        return _wait(
            str(authority.get("error") or "scope_inactive"),
            task_id=task_id,
            reason_codes=list(authority.get("reason_codes") or []),
        )

    item = ports.journal.get_item(task_id, project=project)
    if not item:
        return _wait("mission_not_found", task_id=task_id)
    task = ports.get_task(task_id, project=project)
    if not task:
        return _wait("task_not_found", task_id=task_id)
    dependency_state = task.get("dependency_state")
    dependency_state = (
        dependency_state if isinstance(dependency_state, Mapping) else {}
    )
    decision = decide_mission_transition({
        "scope_active": True,
        "terminal_provenance": (
            item.get("state") == "DONE"
            and bool(item.get("terminal_kind"))
            and bool(item.get("terminal_ref"))
        ),
        "dependencies_satisfied": dependency_state.get("satisfied") is True,
        "mission_state": item.get("state"),
        "runner_live": ports.has_live_execution(task_id, project=project),
        "requested_role": item.get("requested_role"),
        "handled_through": item.get("handled_through"),
        "latest_sequence": item.get("latest_sequence"),
    })
    if decision["action"] != "start_task":
        return _wait(str(decision["reason"]), task_id=task_id)

    event_pointer = int(decision["event_pointer"])
    events = ports.journal.list_events(
        task_id, project=project, after_sequence=event_pointer - 1, limit=1,
    )
    if not events or int(events[0]["sequence"]) != event_pointer:
        return _wait("event_cursor_changed", task_id=task_id)
    event = events[0]

    # Re-read both the journal cursor and W2 authority immediately before the
    # only work-driving call.  A concurrent yield, terminal projection, scope
    # takeover, or another tick therefore fails closed.
    current = ports.journal.get_item(task_id, project=project) or {}
    if (
        int(current.get("version") or 0) != int(item.get("version") or 0)
        or int(current.get("handled_through") or 0)
        != int(item.get("handled_through") or 0)
        or str(current.get("state") or "") != str(item.get("state") or "")
    ):
        return _wait("event_cursor_changed", task_id=task_id)
    boundary = ports.validate_scope(
        dict(scope_authority), project=project, task_project=project, task_id=task_id,
    ) or {}
    if not boundary.get("allowed"):
        return _wait(
            str(boundary.get("error") or "scope_inactive"),
            task_id=task_id,
            reason_codes=list(boundary.get("reason_codes") or []),
        )

    role = str(item["requested_role"])
    mission_key = (
        f"{scope_authority.get('generation')}:{task_id}:{event_pointer}:{role}"
    )
    pointer = {
        "schema": "switchboard.mission_launch_pointer.v4",
        "project": project,
        "task_id": task_id,
        "event_sequence": event_pointer,
        "event_id": event.get("event_id"),
        "event_type": event.get("event_type"),
    }
    receipt = ports.start_task(
        task_id,
        project=project,
        actor=actor,
        role=role,
        source_sha=str(
            event.get("head_sha")
            or (task.get("git_state") or {}).get("head_sha")
            or ""
        ),
        mission_key=mission_key,
        instruction=json.dumps(pointer, sort_keys=True, separators=(",", ":")),
        scope_authority=dict(scope_authority),
    )
    if not _start_was_admitted(receipt):
        return {
            **_wait("start_not_admitted", task_id=task_id),
            "mutations": 1,
            "start_receipt": dict(receipt),
        }

    try:
        updated = ports.journal.update_item(
            task_id,
            project=project,
            state="ACTIVE",
            requested_role=role,
            expected_version=int(item["version"]),
            handled_through=event_pointer,
        )
    except MissionJournalError as exc:
        # Admission is idempotent by mission_key.  Leaving the cursor behind is
        # safe: replay reads back/attaches to the same Task Execution generation.
        return {
            "schema": "switchboard.mission_worker_tick.v4",
            "task_id": task_id,
            "action": "start_task",
            "requested_role": role,
            "event_pointer": event_pointer,
            "mission_key": mission_key,
            "mutations": 1,
            "cursor_advanced": False,
            "cursor_error": exc.code,
            "start_receipt": dict(receipt),
        }
    return {
        "schema": "switchboard.mission_worker_tick.v4",
        "task_id": task_id,
        "action": "start_task",
        "requested_role": role,
        "event_pointer": event_pointer,
        "mission_key": mission_key,
        "mutations": 1,
        "cursor_advanced": True,
        "handled_through": updated["handled_through"],
        "start_receipt": dict(receipt),
    }
