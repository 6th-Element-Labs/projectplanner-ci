"""The complete work-driving loop for Mission Bot v5.

The worker has one effect: call ``start_task`` with the role already stored in
the mission journal.  It copies no diagnosis, dossier, finding, or provider
payload into the assignment.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from switchboard.domain.mission_bot_v5 import decide_mission_transition
from switchboard.storage.repositories.mission_journal import (
    MissionJournalError,
    MissionJournalRepository,
    default_mission_journal_repository,
)

ReadMapping = Callable[..., Mapping[str, Any] | None]
StartTask = Callable[..., Mapping[str, Any]]


@dataclass(frozen=True)
class ScopedMissionWorkerPorts:
    validate_scope: ReadMapping
    get_task: ReadMapping
    has_live_execution: Callable[..., bool]
    start_task: StartTask
    journal: MissionJournalRepository = default_mission_journal_repository
    launch_attempts: Any = None
    clock: Callable[[], float] = time.time
    max_launch_attempts: int = 3
    retry_base_seconds: int = 15


def _wait(reason: str, *, task_id: str, **details: Any) -> dict[str, Any]:
    return {
        "schema": "switchboard.mission_worker_tick.v5",
        "task_id": task_id,
        "action": "wait",
        "reason": reason,
        "mutations": 0,
        **details,
    }


def _start_was_admitted(receipt: Mapping[str, Any]) -> bool:
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
    """Run one fenced pager tick with at most one Capacity request."""
    authority = ports.validate_scope(
        dict(scope_authority), project=project, task_project=project, task_id=task_id,
    ) or {}
    if authority.get("allowed") is not True:
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
    dependency_state = dependency_state if isinstance(dependency_state, Mapping) else {}

    decision = decide_mission_transition({
        "scope_active": True,
        "mission_state": item.get("state"),
        "requested_role": item.get("requested_role"),
        "terminal_provenance": (
            item.get("state") == "DONE"
            and bool(item.get("terminal_kind"))
            and bool(item.get("terminal_ref"))
        ),
        "dependencies_satisfied": dependency_state.get("satisfied") is True,
        "runner_live": ports.has_live_execution(task_id, project=project),
        "handled_through": item.get("handled_through"),
        "latest_sequence": item.get("latest_sequence"),
    })
    if decision["action"] != "start_task":
        return _wait(str(decision["reason"]), task_id=task_id)

    event_pointer = int(decision["event_pointer"])
    events = ports.journal.list_events(
        task_id, project=project, after_sequence=event_pointer - 1, limit=1,
    )
    if not events or int(events[0].get("sequence") or 0) != event_pointer:
        return _wait("event_cursor_changed", task_id=task_id)

    # Recheck the row and W2 fence immediately before the sole side effect.
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
    if boundary.get("allowed") is not True:
        return _wait(
            str(boundary.get("error") or "scope_inactive"),
            task_id=task_id,
            reason_codes=list(boundary.get("reason_codes") or []),
        )

    role = str(item["requested_role"])
    mission_key = f"v5:{scope_authority.get('generation')}:{task_id}:{event_pointer}:{role}"
    if ports.launch_attempts is not None:
        prior = ports.launch_attempts.get(
            task_id, project=project, mission_key=mission_key,
        ) or {}
        if prior.get("exhausted"):
            return _wait(
                "launch_retry_exhausted", task_id=task_id,
                retry_count=int(prior.get("retry_count") or 0),
                start_error=str(prior.get("start_error") or ""),
            )
        next_retry_at = prior.get("next_retry_at")
        if next_retry_at is not None and float(next_retry_at) > float(ports.clock()):
            return _wait(
                "launch_retry_backoff", task_id=task_id,
                retry_count=int(prior.get("retry_count") or 0),
                next_retry_at=float(next_retry_at),
                start_error=str(prior.get("start_error") or ""),
            )
    instruction = json.dumps({
        "schema": "switchboard.mission_pointer.v5",
        "project": project,
        "task_id": task_id,
        "event_sequence": event_pointer,
    }, sort_keys=True, separators=(",", ":"))
    source_sha = (
        str((task.get("git_state") or {}).get("head_sha") or "")
        if role in {"review_merge", "remediation"}
        else ""
    )
    receipt = ports.start_task(
        task_id,
        project=project,
        actor=actor,
        role=role,
        source_sha=source_sha,
        mission_key=mission_key,
        instruction=instruction,
        scope_authority=dict(scope_authority),
    )
    if not _start_was_admitted(receipt):
        start_error = str(
            receipt.get("start_error") or receipt.get("error")
            or (receipt.get("last_dispatch_outcome") or {}).get("error") or ""
        )
        attempt = None
        if ports.launch_attempts is not None:
            attempt = ports.launch_attempts.record_failure(
                task_id,
                project=project,
                mission_key=mission_key,
                requested_role=role,
                reason="start_not_admitted",
                start_error=start_error,
                max_attempts=ports.max_launch_attempts,
                base_delay_seconds=ports.retry_base_seconds,
                now=float(ports.clock()),
            )
        return {
            **_wait(
                "launch_retry_exhausted"
                if attempt and attempt.get("exhausted")
                else "start_not_admitted",
                task_id=task_id,
            ),
            "mutations": 1,
            "retry_count": int((attempt or {}).get("retry_count") or 0),
            "next_retry_at": (attempt or {}).get("next_retry_at"),
            "start_error": start_error,
            "start_receipt": dict(receipt),
        }

    if ports.launch_attempts is not None:
        ports.launch_attempts.clear(
            task_id, project=project, mission_key=mission_key,
        )

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
        return {
            "schema": "switchboard.mission_worker_tick.v5",
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
        "schema": "switchboard.mission_worker_tick.v5",
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


__all__ = ["ScopedMissionWorkerPorts", "tick_scoped_mission"]
