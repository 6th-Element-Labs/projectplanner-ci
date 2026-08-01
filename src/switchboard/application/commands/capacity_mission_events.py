"""Project durable Capacity facts into the staged Mission Bot v4 journal.

The projector is deliberately passive.  It reads only Capacity's canonical
wake and ``runner_sessions`` records and appends typed, idempotent facts.  It
never selects a role, advances a mission cursor, calls ``start_task``, or treats
the journal as execution liveness.  Running it immediately before a scoped v4
decision gives the pager a replay-safe edge for failures that otherwise have no
runner receipt (BUG-248) and terminal runners that cannot report for themselves
(BUG-253).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from switchboard.domain.coordination.terminal import TERMINAL_WAKE_STATUSES
from switchboard.domain.coordination.wake_intents import (
    is_control_plane_unavailable,
)
from switchboard.domain.execution_liveness import TERMINAL_EXECUTION_STATES
from switchboard.storage.repositories.mission_journal import (
    MissionJournalRepository,
    default_mission_journal_repository,
)

CapacityLister = Callable[..., list]
PRE_RUNNER_TERMINAL_WAKE_STATUSES = TERMINAL_WAKE_STATUSES - {"completed"}


class CapacityMissionProjectionError(RuntimeError):
    """A durable Capacity fact is unavailable or cannot be identified safely."""

    failure_class = "missing_data"

    def __init__(
        self, code: str, message: str, *, details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "failure_class": self.failure_class,
            "message": str(self),
            "details": dict(self.details),
        }


def _default_wake_lister(**kwargs: Any) -> list:
    from switchboard.storage.repositories import coordination

    return coordination.list_wake_intents(**kwargs)


def _default_runner_lister(**kwargs: Any) -> list:
    from switchboard.storage.repositories import runner

    return runner.list_runner_sessions(**kwargs)


def _capacity_rows(
    value: Any, *, operation: str, identity_field: str,
) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise CapacityMissionProjectionError(
            "capacity_read_invalid",
            f"{operation} returned {type(value).__name__}, expected a list",
            details={"operation": operation},
        )
    rows: list[Mapping[str, Any]] = []
    for index, row in enumerate(value):
        if is_control_plane_unavailable(row):
            reason = str(row.get("reason") or row.get("error") or "unknown cause")
            raise CapacityMissionProjectionError(
                "capacity_read_unavailable",
                f"{operation} failed: {reason}",
                details={"operation": operation, "cause": reason},
            )
        if not isinstance(row, Mapping) or not str(row.get(identity_field) or "").strip():
            raise CapacityMissionProjectionError(
                "capacity_row_malformed",
                f"{operation} row {index} has no {identity_field}",
                details={"operation": operation, "row_index": index},
            )
        rows.append(row)
    return rows


def _read_capacity_rows(
    lister: CapacityLister,
    *,
    operation: str,
    identity_field: str,
    kwargs: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    try:
        value = lister(**dict(kwargs))
    except Exception as exc:
        cause = f"{type(exc).__name__}: {exc}"
        raise CapacityMissionProjectionError(
            "capacity_read_failed",
            f"{operation} failed: {cause}",
            details={"operation": operation, "cause": cause},
        ) from exc
    return _capacity_rows(
        value, operation=operation, identity_field=identity_field,
    )


def _timestamp(value: Any, *, field: str, identity: str) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        timestamp = 0.0
    if timestamp <= 0:
        raise CapacityMissionProjectionError(
            "capacity_timestamp_missing",
            f"{identity} has no valid {field}",
            details={"identity": identity, "field": field},
        )
    return timestamp


def _identity_from_surfaces(
    surfaces: list[tuple[str, Any]], *, identity: str,
) -> tuple[str, int]:
    found: list[tuple[str, str, int]] = []
    for name, surface in surfaces:
        if not isinstance(surface, Mapping):
            continue
        execution_id = str(surface.get("execution_id") or "").strip()
        raw_generation = surface.get("generation")
        try:
            generation = int(raw_generation or 0)
        except (TypeError, ValueError):
            generation = 0
        if execution_id or raw_generation not in (None, ""):
            found.append((name, execution_id, generation))
    if not found or not found[0][1] or found[0][2] <= 0:
        raise CapacityMissionProjectionError(
            "execution_identity_missing",
            f"{identity} has no exact execution id and generation",
            details={"identity": identity},
        )
    expected = found[0][1:]
    if any(candidate[1:] != expected for candidate in found[1:]):
        raise CapacityMissionProjectionError(
            "execution_identity_conflict",
            f"{identity} carries conflicting execution identities",
            details={
                "identity": identity,
                "surfaces": [name for name, _execution_id, _generation in found],
            },
        )
    return expected


def _wake_execution_identity(wake: Mapping[str, Any]) -> tuple[str, int]:
    policy = wake.get("policy")
    policy = policy if isinstance(policy, Mapping) else {}
    wake_id = str(wake.get("wake_id") or "")
    return _identity_from_surfaces(
        [
            ("execution_assignment", policy.get("execution_assignment")),
            ("lifecycle", policy.get("lifecycle")),
        ],
        identity=f"wake {wake_id}",
    )


def _runner_execution_identity(session: Mapping[str, Any]) -> tuple[str, int]:
    metadata = session.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    execution = session.get("execution")
    execution = execution if isinstance(execution, Mapping) else {}
    runner_id = str(session.get("runner_session_id") or "")
    metadata_identity = {
        "execution_id": metadata.get("execution_id"),
        "generation": metadata.get("execution_generation"),
    }
    return _identity_from_surfaces(
        [("runner_metadata", metadata_identity), ("execution", execution)],
        identity=f"runner {runner_id}",
    )


def _wake_failure_reason(wake: Mapping[str, Any]) -> str:
    result = wake.get("result")
    result = result if isinstance(result, Mapping) else {}
    recovery = result.get("recovery")
    recovery = recovery if isinstance(recovery, Mapping) else {}
    for surface in (result, recovery):
        for field in ("reason", "failure_reason", "error", "message"):
            value = str(surface.get(field) or "").strip()
            if value:
                return value
    return ""


def append_terminal_wake_events(
    *,
    project: str,
    task_id: str,
    repository: MissionJournalRepository = default_mission_journal_repository,
    list_wakes: CapacityLister | None = None,
    list_runners: CapacityLister | None = None,
) -> dict[str, Any]:
    """Append one ``execution_ended`` fact per terminal pre-runner wake."""
    item = repository.get_item(task_id, project=project)
    if item is None:
        return {"action": "ignored", "reason": "mission_not_found", "events": []}
    mission_created_at = _timestamp(
        item.get("created_at"), field="created_at", identity=f"mission {task_id}",
    )
    lister = list_wakes or _default_wake_lister
    rows = _read_capacity_rows(
        lister,
        operation="list_wake_intents",
        identity_field="wake_id",
        kwargs={
            "task_id": task_id,
            "project": project,
            "include_archived": True,
        },
    )
    registered_runner_ids: set[str] = set()
    allocated_runner_ids = {
        str(wake.get("runner_session_id") or "").strip()
        for wake in rows
        if str(wake.get("runner_session_id") or "").strip()
    }
    if allocated_runner_ids:
        runner_lister = list_runners or _default_runner_lister
        runner_rows = _read_capacity_rows(
            runner_lister,
            operation="list_runner_sessions",
            identity_field="runner_session_id",
            kwargs={
                "task_id": task_id, "include_stale": True, "project": project,
            },
        )
        for runner in runner_rows:
            row_task = str(runner.get("task_id") or "").strip()
            if row_task and row_task != task_id:
                raise CapacityMissionProjectionError(
                    "capacity_task_mismatch",
                    f"runner {runner['runner_session_id']} belongs to {row_task}, "
                    f"not {task_id}",
                )
            registered_runner_ids.add(str(runner["runner_session_id"]))

    events: list[dict[str, Any]] = []
    for wake in rows:
        row_task = str(wake.get("task_id") or "").strip()
        if row_task and row_task != task_id:
            raise CapacityMissionProjectionError(
                "capacity_task_mismatch",
                f"wake {wake['wake_id']} belongs to {row_task}, not {task_id}",
            )
        status = str(wake.get("status") or "").strip().lower()
        if status not in TERMINAL_WAKE_STATUSES:
            continue
        # Agent Host allocates a runner id before workspace materialization and
        # registration. The string stored on a terminal wake is correlation,
        # not physical execution presence. ADR-0008 C1 permits only an actual
        # runner_sessions row to prove that a runner existed.
        if str(wake.get("runner_session_id") or "") in registered_runner_ids:
            continue
        wake_id = str(wake["wake_id"])
        requested_at = _timestamp(
            wake.get("requested_at"), field="requested_at", identity=f"wake {wake_id}",
        )
        if requested_at < mission_created_at:
            continue
        if status == "completed":
            raise CapacityMissionProjectionError(
                "completed_wake_without_runner",
                f"wake {wake['wake_id']} is completed without a runner session",
                details={"wake_id": str(wake["wake_id"])},
            )
        if status not in PRE_RUNNER_TERMINAL_WAKE_STATUSES:
            continue
        execution_id, generation = _wake_execution_identity(wake)
        reason = _wake_failure_reason(wake)
        if not reason:
            raise CapacityMissionProjectionError(
                "capacity_failure_cause_missing",
                f"wake {wake_id} ended as {status} without a preserved cause",
                details={"wake_id": wake_id, "status": status},
            )
        receipt_ref = f"wake:{wake_id}"
        occurred_at = wake.get("completed_at")
        event = repository.append_event(
            task_id,
            project=project,
            event_type="execution_ended",
            source_plane="capacity",
            idempotency_key=f"execution_ended:{wake_id}",
            **(
                {"occurred_at": float(occurred_at)}
                if isinstance(occurred_at, (int, float)) and occurred_at > 0
                else {}
            ),
            execution_id=execution_id,
            generation=generation,
            external_ref=receipt_ref,
            payload={
                "wake_id": wake_id,
                "terminal_status": status,
                "reason_code": reason,
                "receipt_ref": receipt_ref,
            },
        )
        events.append({
            "event_id": event.get("event_id"),
            "sequence": event.get("sequence"),
            "wake_id": wake_id,
            "created": bool(event.get("created")),
        })
    return {"action": "terminal_wake_events_projected", "events": events}


def append_terminal_runner_events(
    *,
    project: str,
    task_id: str,
    repository: MissionJournalRepository = default_mission_journal_repository,
    list_runners: CapacityLister | None = None,
) -> dict[str, Any]:
    """Append one ``runner_ended`` fact per terminal Capacity generation."""
    item = repository.get_item(task_id, project=project)
    if item is None:
        return {"action": "ignored", "reason": "mission_not_found", "events": []}
    mission_created_at = _timestamp(
        item.get("created_at"), field="created_at", identity=f"mission {task_id}",
    )
    lister = list_runners or _default_runner_lister
    rows = _read_capacity_rows(
        lister,
        operation="list_runner_sessions",
        identity_field="runner_session_id",
        kwargs={"task_id": task_id, "include_stale": True, "project": project},
    )

    events: list[dict[str, Any]] = []
    finalized_handoffs: list[dict[str, Any]] = []
    for session in rows:
        row_task = str(session.get("task_id") or "").strip()
        if row_task and row_task != task_id:
            raise CapacityMissionProjectionError(
                "capacity_task_mismatch",
                f"runner {session['runner_session_id']} belongs to {row_task}, not {task_id}",
            )
        status = str(session.get("status") or "").strip().lower()
        if status not in TERMINAL_EXECUTION_STATES:
            continue
        runner_id = str(session["runner_session_id"])
        started_at = _timestamp(
            session.get("started_at"), field="started_at", identity=f"runner {runner_id}",
        )
        if started_at < mission_created_at:
            continue
        execution_id, generation = _runner_execution_identity(session)
        handoff = repository.finalize_terminal_handoff(
            task_id,
            project=project,
            execution_id=execution_id,
            generation=generation,
            now=(
                float(session["updated_at"])
                if isinstance(session.get("updated_at"), (int, float))
                and float(session["updated_at"]) > 0
                else None
            ),
        )
        if handoff.get("accepted") is True:
            finalized_handoffs.append({
                "runner_session_id": runner_id,
                "execution_id": execution_id,
                "generation": generation,
                **dict(handoff),
            })
            continue
        metadata = session.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        execution = session.get("execution")
        execution = execution if isinstance(execution, Mapping) else {}
        head_sha = str(
            metadata.get("execution_head_sha")
            or metadata.get("head_sha")
            or execution.get("head_sha")
            or ""
        ).strip()
        reason = str(
            metadata.get("terminal_reason_code")
            or metadata.get("failure_class")
            or session.get("failure_reason")
            or ""
        ).strip()
        receipt_ref = f"runner_session:{runner_id}"
        payload = {
            "runner_session_id": runner_id,
            "terminal_status": status,
            "receipt_ref": receipt_ref,
        }
        if reason:
            payload["reason_code"] = reason
        occurred_at = session.get("updated_at")
        event = repository.append_event(
            task_id,
            project=project,
            event_type="runner_ended",
            source_plane="capacity",
            idempotency_key=f"runner_ended:{runner_id}",
            **(
                {"occurred_at": float(occurred_at)}
                if isinstance(occurred_at, (int, float)) and occurred_at > 0
                else {}
            ),
            execution_id=execution_id,
            generation=generation,
            head_sha=head_sha or None,
            external_ref=receipt_ref,
            payload=payload,
        )
        events.append({
            "event_id": event.get("event_id"),
            "sequence": event.get("sequence"),
            "runner_session_id": runner_id,
            "created": bool(event.get("created")),
        })
    return {
        "action": "terminal_runner_events_projected",
        "events": events,
        "finalized_handoffs": finalized_handoffs,
    }


def project_capacity_events(
    *,
    project: str,
    task_id: str,
    repository: MissionJournalRepository = default_mission_journal_repository,
    list_wakes: CapacityLister | None = None,
    list_runners: CapacityLister | None = None,
) -> dict[str, Any]:
    """Project both durable Capacity edges before one scoped pager decision."""
    return {
        "schema": "switchboard.capacity_mission_projection.v1",
        "task_id": task_id,
        "wake_projection": append_terminal_wake_events(
            project=project,
            task_id=task_id,
            repository=repository,
            list_wakes=list_wakes,
            list_runners=list_runners,
        ),
        "runner_projection": append_terminal_runner_events(
            project=project,
            task_id=task_id,
            repository=repository,
            list_runners=list_runners,
        ),
    }


__all__ = [
    "CapacityMissionProjectionError",
    "append_terminal_runner_events",
    "append_terminal_wake_events",
    "project_capacity_events",
]
