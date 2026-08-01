"""Explicit production port binding for one operator-invoked v4 pager tick.

This module does not schedule, loop, initialize missions, project events, or
select tasks.  Those are separate increments.  It only binds historical
COORD-110 to the current authoritative repositories so the native capability
can be exercised without giving it global production authority.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from switchboard.application.commands import capacity_mission_events, task_execution
from switchboard.application.mission_bot_v4.worker import (
    ScopedMissionWorkerPorts,
    tick_scoped_mission,
)
from switchboard.storage.repositories import autopilot_scopes, runner, tasks
from switchboard.storage.repositories.mission_journal import (
    MissionJournalRepository,
    default_mission_journal_repository,
)


def production_ports(
    *,
    actor: str,
    agent_id: str,
    scope_project: str,
    journal: MissionJournalRepository = default_mission_journal_repository,
    store_mod: Any = None,
) -> ScopedMissionWorkerPorts:
    """Bind W1/W2/C1 ports without installing an automatic caller."""

    def resolve(name: str, fallback: Any) -> Any:
        candidate = getattr(store_mod, name, None) if store_mod is not None else None
        return candidate if callable(candidate) else fallback

    scope_validator = resolve(
        "validate_autopilot_scope_authority",
        autopilot_scopes.validate_autopilot_scope_authority,
    )
    task_reader = resolve("get_task", tasks.get_task)
    liveness_reader = resolve("task_has_live_execution", runner.task_has_live_execution)

    def validate(
        authority: Mapping[str, Any], **kwargs: Any,
    ) -> Mapping[str, Any]:
        return scope_validator(
            dict(authority),
            project=scope_project,
            task_project=str(kwargs.get("task_project") or kwargs.get("project") or ""),
            task_id=str(kwargs.get("task_id") or ""),
        )

    def get_task(task_id: str, *, project: str) -> Mapping[str, Any] | None:
        return task_reader(task_id, project=project)

    def has_live(task_id: str, *, project: str) -> bool:
        # ADR-0008 C1: this port is bound only to runner_sessions truth.
        return bool(liveness_reader(task_id, project=project))

    def start(task_id: str, **kwargs: Any) -> Mapping[str, Any]:
        authority = dict(kwargs.pop("scope_authority"))
        verdict = validate(
            authority,
            project=str(kwargs["project"]),
            task_project=str(kwargs["project"]),
            task_id=task_id,
        )
        if verdict.get("allowed") is not True:
            return {
                "error": verdict.get("error") or "scope_authority_denied",
                "failure_class": "absent_permission",
                "reason_codes": list(verdict.get("reason_codes") or []),
                "refused": True,
            }
        try:
            return task_execution.start_task(
                task_id,
                project=str(kwargs["project"]),
                actor=actor,
                agent_id=agent_id,
                role=str(kwargs["role"]),
                source_sha=str(kwargs.get("source_sha") or ""),
                instruction=str(kwargs.get("instruction") or ""),
                mission_key=str(kwargs.get("mission_key") or ""),
            )
        except task_execution.TaskExecutionError as exc:
            # Preserve the typed Task Execution refusal; do not replace it with
            # a generic wait, retry, alternate start path, or optimistic receipt.
            return exc.as_dict()

    return ScopedMissionWorkerPorts(
        validate_scope=validate,
        get_task=get_task,
        has_live_execution=has_live,
        start_task=start,
        journal=journal,
    )


def run_scoped_mission_tick(
    task_id: str,
    *,
    project: str,
    scope_project: str,
    scope_authority: Mapping[str, Any],
    actor: str,
    agent_id: str,
    journal: MissionJournalRepository = default_mission_journal_repository,
    store_mod: Any = None,
) -> dict[str, Any]:
    """Project durable Capacity facts, then run one opt-in scoped tick.

    No daemon or production cutover calls this function yet.  Projection is
    allowed only after the exact W2 scope validates, and a broken Capacity read
    returns a named red signal instead of making the pager look idle.
    """
    ports = production_ports(
        actor=actor,
        agent_id=agent_id,
        scope_project=scope_project,
        journal=journal,
        store_mod=store_mod,
    )
    authority = ports.validate_scope(
        dict(scope_authority),
        project=project,
        task_project=project,
        task_id=task_id,
    ) or {}
    projection: dict[str, Any] | None = None
    if authority.get("allowed") is True:
        before_projection = journal.get_item(task_id, project=project) or {}
        before_sequence = int(before_projection.get("latest_sequence") or 0)
        try:
            projection = capacity_mission_events.project_capacity_events(
                project=project,
                task_id=task_id,
                repository=journal,
                list_wakes=getattr(store_mod, "list_wake_intents", None),
                list_runners=getattr(store_mod, "list_runner_sessions", None),
            )
        except capacity_mission_events.CapacityMissionProjectionError as exc:
            after_projection = journal.get_item(task_id, project=project) or {}
            projected_events = max(
                0,
                int(after_projection.get("latest_sequence") or 0) - before_sequence,
            )
            return {
                "schema": "switchboard.mission_worker_tick.v4",
                "task_id": task_id,
                "action": "wait",
                "reason": "capacity_projection_failed",
                "mutations": projected_events,
                "partial_projection": projected_events > 0,
                **exc.as_dict(),
            }
    result = tick_scoped_mission(
        task_id,
        project=project,
        scope_authority=scope_authority,
        actor=actor,
        ports=ports,
    )
    if projection is not None:
        result["capacity_projection"] = projection
    return result


__all__ = ["production_ports", "run_scoped_mission_tick"]
