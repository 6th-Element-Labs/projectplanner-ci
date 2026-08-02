"""Explicit production port binding for one operator-invoked v4 pager tick.

This module does not schedule, loop, initialize missions, project events, or
select tasks.  Those are separate increments.  It only binds historical
COORD-110 to the current authoritative repositories so the native capability
can be exercised without giving it global production authority.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import time
from typing import Any

from switchboard.application.commands import (
    capacity_mission_events,
    github_mission_events,
    task_execution,
)
from switchboard.application.mission_bot_v4.worker import (
    ScopedMissionWorkerPorts,
    tick_scoped_mission,
)
from switchboard.domain.mission_bot_v4 import active_mission_failure
from switchboard.storage.repositories import autopilot_scopes, coordination, runner, tasks
from switchboard.storage.repositories.mission_journal import (
    MissionJournalError,
    MissionJournalRepository,
    default_mission_journal_repository,
)


def task_has_pending_capacity_attempt(
    task_id: str,
    *,
    project: str,
    list_wakes: Callable[..., list[Mapping[str, Any]]] = coordination.list_wake_intents,
    now: float | None = None,
) -> bool:
    """Read an outstanding Capacity request without treating it as liveness."""
    observed_at = time.time() if now is None else float(now)
    rows = list_wakes(project=project, task_id=task_id)
    for wake in rows:
        if str(wake.get("status") or "").strip().lower() not in {"pending", "claimed"}:
            continue
        deadline = wake.get("deadline")
        if deadline is not None and float(deadline) <= observed_at:
            continue
        return True
    return False


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
    wake_reader = resolve("list_wake_intents", coordination.list_wake_intents)

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

    def has_pending(task_id: str, *, project: str) -> bool:
        return task_has_pending_capacity_attempt(
            task_id, project=project, list_wakes=wake_reader,
        )

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
        has_pending_capacity_attempt=has_pending,
        journal=journal,
    )


def project_terminal_provenance(
    task_id: str,
    *,
    project: str,
    actor: str,
    journal: MissionJournalRepository = default_mission_journal_repository,
    task_reader: Callable[..., Mapping[str, Any] | None] = tasks.get_task,
) -> dict[str, Any]:
    """Project already-persisted canonical Done truth into the v4 journal.

    This staged v4 adapter is deliberately outside the v1 provenance path.  It
    creates no mission and performs no lifecycle effect beyond closing the
    existing passive v4 item from canonical truth.
    """
    task = task_reader(task_id, project=project)
    if not task:
        return {
            "projected": False,
            "release_blocked": True,
            "reason": "task_not_found",
        }
    if str(task.get("status") or "") != "Done":
        return {
            "projected": False,
            "release_blocked": False,
            "reason": "task_not_terminal",
        }
    git_state = task.get("git_state")
    git_state = git_state if isinstance(git_state, Mapping) else {}
    merged_sha = str(git_state.get("merged_sha") or "").strip().lower()
    if not merged_sha or not bool(git_state.get("in_main_content")):
        return {
            "projected": False,
            "release_blocked": True,
            "reason": "canonical_terminal_provenance_missing",
        }
    try:
        receipt = journal.record_terminal_provenance(
            task_id,
            project=project,
            terminal_kind="github_merge",
            terminal_ref=merged_sha,
            actor=actor,
        )
    except MissionJournalError as exc:
        return {
            "projected": False,
            "release_blocked": True,
            "reason": exc.code,
            "message": str(exc),
        }
    if receipt.get("recorded") is not True:
        return {
            "projected": False,
            "release_blocked": True,
            "reason": str(receipt.get("reason") or "terminal_projection_failed"),
            "receipt": dict(receipt),
        }
    return {
        "projected": True,
        "release_blocked": False,
        "reason": "canonical_terminal_provenance_projected",
        "terminal_kind": "github_merge",
        "terminal_ref": merged_sha,
        "receipt": dict(receipt),
    }


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
    review_handoff_projection: dict[str, Any] | None = None
    terminal_projection: dict[str, Any] | None = None
    if authority.get("allowed") is True:
        task_reader = getattr(store_mod, "get_task", None)
        if not callable(task_reader):
            task_reader = tasks.get_task
        terminal_projection = project_terminal_provenance(
            task_id,
            project=project,
            actor=actor,
            journal=journal,
            task_reader=task_reader,
        )
        if terminal_projection.get("release_blocked") is True:
            return {
                "schema": "switchboard.mission_worker_tick.v4",
                "task_id": task_id,
                "action": "block_release",
                "reason": str(terminal_projection.get("reason")),
                "release_blocked": True,
                "mutations": 0,
                "terminal_projection": terminal_projection,
            }
        try:
            review_handoff_projection = journal.project_review_handoff(
                task_id,
                project=project,
                actor=actor,
                task=task_reader(task_id, project=project) or {},
            )
        except MissionJournalError as exc:
            return {
                "schema": "switchboard.mission_worker_tick.v4",
                "task_id": task_id,
                "action": "block_release",
                "reason": exc.code,
                "release_blocked": True,
                "mutations": 0,
                "message": str(exc),
            }
        if review_handoff_projection.get("release_blocked") is True:
            return {
                "schema": "switchboard.mission_worker_tick.v4",
                "task_id": task_id,
                "action": "block_release",
                "reason": str(review_handoff_projection.get("reason")),
                "release_blocked": True,
                "mutations": 0,
                "review_handoff_projection": review_handoff_projection,
            }
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
        github_mission_events.append_due_observations(
            project=project,
            task_id=task_id,
            repository=journal,
        )
    result = tick_scoped_mission(
        task_id,
        project=project,
        scope_authority=scope_authority,
        actor=actor,
        ports=ports,
    )
    if projection is not None:
        result["capacity_projection"] = projection
    if review_handoff_projection is not None:
        result["review_handoff_projection"] = review_handoff_projection
    if terminal_projection is not None:
        result["terminal_projection"] = terminal_projection
    return result


def assess_stuck_mission_invariant(
    *,
    project: str,
    journal: MissionJournalRepository = default_mission_journal_repository,
    has_live_execution: Callable[..., bool] = runner.task_has_live_execution,
    has_pending_capacity_attempt: Callable[..., bool] = task_has_pending_capacity_attempt,
) -> dict[str, Any]:
    """Read every staged mission and fail release readiness on blind waits.

    The scan is read-only and deliberately narrow: a green result proves only
    this invariant.  It never authorizes cutover and never treats wakes,
    claims, Work Sessions, or agent presence as Capacity liveness.
    """
    blockers: list[dict[str, Any]] = []
    task_ids = journal.active_task_ids(project=project)
    for task_id in task_ids:
        item = journal.get_item(task_id, project=project)
        if item is None:
            blockers.append({
                "schema": "switchboard.mission_stuck_invariant.v1",
                "invariant": "active_requires_runner_human_or_unhandled_event",
                "release_blocked": True,
                "reason": "mission_disappeared_during_read",
                "failure_class": "missing_data",
                "severity": "critical",
                "message": "Mission disappeared during the release-readiness scan.",
                "expected_signal": "The same mission row is readable for the bounded scan.",
                "missing_producer": False,
                "evidence": {"project": project, "task_id": task_id},
            })
            continue
        failure = active_mission_failure({
            "project": project,
            "task_id": task_id,
            "mission_state": item.get("state"),
            "requested_role": item.get("requested_role"),
            "terminal_provenance": (
                item.get("state") == "DONE"
                and bool(item.get("terminal_kind"))
                and bool(item.get("terminal_ref"))
            ),
            "runner_live": bool(has_live_execution(task_id, project=project)),
            "capacity_attempt_pending": bool(
                has_pending_capacity_attempt(task_id, project=project)
            ),
            "handled_through": item.get("handled_through"),
            "latest_sequence": item.get("latest_sequence"),
        })
        if failure is not None:
            blockers.append(failure)
    return {
        "schema": "switchboard.mission_stuck_release_gate.v1",
        "project": project,
        "checked_task_count": len(task_ids),
        "passed": not blockers,
        "release_blocked": bool(blockers),
        "cutover_authorized": False,
        "blocker_count": len(blockers),
        "blockers": blockers,
        "runner_liveness_source": "runner_sessions",
    }


__all__ = [
    "assess_stuck_mission_invariant",
    "project_terminal_provenance",
    "production_ports",
    "run_scoped_mission_tick",
    "task_has_pending_capacity_attempt",
]
