"""Progress watchdog: a live lease with a silent PTY is a fault, not health (WATCH-19).

Liveness (heartbeat / lease renewal) and progress (PTY output age) are different
measurements. Existing reapers only take the first; this monitor takes the second
and escalates — it never auto-kills.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Iterable, Optional


DEFAULT_PROGRESS_STALL_S = 1800.0  # 30 minutes — generous for implementation roles
FAULT_KIND = "runner_progress_stalled"
FAULT_SCHEMA = "switchboard.runner_progress_fault.v1"
_WATCHABLE_STATUSES = {"ready", "running"}
_LOCK = threading.RLock()
_active_sessions: dict[str, set[str]] = {}
_task_by_session: dict[str, dict[str, str]] = {}
_faults: dict[str, dict[str, dict[str, Any]]] = {}


def _bound_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get(
            "PM_RUNNER_PROGRESS_STALL_S",
            str(DEFAULT_PROGRESS_STALL_S),
        )))
    except (TypeError, ValueError):
        return DEFAULT_PROGRESS_STALL_S


def _metadata(row: dict) -> dict:
    value = row.get("metadata")
    return value if isinstance(value, dict) else {}


def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_live_lease(row: dict) -> bool:
    """Progress faults only apply while the lease itself still looks alive."""
    status = str(row.get("status") or "").strip().lower()
    if status not in _WATCHABLE_STATUSES:
        return False
    if row.get("stale") is True:
        return False
    if row.get("live") is False:
        return False
    metadata = _metadata(row)
    if metadata.get("terminal_cleanup_requested_at"):
        return False
    if metadata.get("terminalized_by"):
        return False
    if metadata.get("lease_surrender"):
        return False
    return True


def _eligible(row: dict) -> bool:
    """Scope to native PTY-backed runs that should be producing output."""
    metadata = _metadata(row)
    return bool(
        _is_live_lease(row)
        and metadata.get("native_host_execution") is True
        and (metadata.get("pty") is True or metadata.get("log_path")
             or metadata.get("last_output_at") is not None)
    )


def evaluate_progress(
    row: dict,
    *,
    now: Optional[float] = None,
    bound_s: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Return a typed progress fault when a live lease has gone silent."""
    if not _eligible(row):
        return None
    ts = float(time.time() if now is None else now)
    bound = _bound_seconds() if bound_s is None else max(0.0, float(bound_s))
    metadata = _metadata(row)
    last_output_at = _float_or_none(metadata.get("last_output_at"))
    if last_output_at is None:
        # Old hosts that never report progress cannot invent a stall signal.
        return None
    output_age_s = max(0.0, ts - last_output_at)
    if output_age_s < bound:
        return None
    existing = metadata.get("progress_fault")
    raised_at = (
        _float_or_none((existing or {}).get("raised_at"))
        if isinstance(existing, dict) and existing.get("kind") == FAULT_KIND
        else None
    )
    if raised_at is None:
        raised_at = ts
    cpu = metadata.get("cpu_percent")
    if cpu is None:
        cpu = metadata.get("cpu")
    return {
        "schema": FAULT_SCHEMA,
        "kind": FAULT_KIND,
        "runner_session_id": str(row.get("runner_session_id") or ""),
        "task_id": str(row.get("task_id") or ""),
        "host_id": str(row.get("host_id") or ""),
        "last_output_at": last_output_at,
        "output_age_s": round(output_age_s, 3),
        "bound_s": bound,
        "output_bytes": metadata.get("output_bytes"),
        "cpu_percent": cpu,
        "log_tail": str(metadata.get("log_tail") or "")[-4000:],
        "raised_at": raised_at,
        "message": (
            f"live runner lease with no PTY output for {int(output_age_s)}s "
            f"(bound {int(bound)}s)"
        ),
    }


def apply_progress_fault(
    row: dict,
    *,
    now: Optional[float] = None,
    bound_s: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Stamp or clear ``metadata.progress_fault`` on a runner row in place."""
    metadata = dict(_metadata(row))
    fault = evaluate_progress(row, now=now, bound_s=bound_s)
    if fault is None:
        if "progress_fault" in metadata:
            metadata.pop("progress_fault", None)
            row["metadata"] = metadata
        return None
    previous = metadata.get("progress_fault")
    if (isinstance(previous, dict)
            and previous.get("kind") == FAULT_KIND
            and previous.get("raised_at") is not None):
        fault["raised_at"] = previous["raised_at"]
    metadata["progress_fault"] = fault
    row["metadata"] = metadata
    return fault


def _default_sessions(project: str) -> list[dict]:
    from switchboard.storage.repositories import runner as runner_repo

    return list(runner_repo.list_runner_sessions(
        include_stale=False, project=project) or [])


def _emit_narration_event(project: str, *, active: bool, task_ids: list[str],
                          session_ids: list[str], bound_s: float, now: float,
                          faults: dict[str, dict]) -> None:
    try:
        from switchboard.storage.repositories.activity import append_activity

        state = "raised" if active else "cleared"
        append_activity(
            f"narration.runner_progress_fault_{state}",
            "switchboard/runner-progress-monitor",
            {
                "schema": FAULT_SCHEMA,
                "state": state,
                "kind": FAULT_KIND,
                "task_ids": task_ids,
                "runner_session_ids": session_ids,
                "bound_s": bound_s,
                "faults": faults,
                "message": (
                    f"Runner progress fault {state}; affected tasks: "
                    f"{', '.join(task_ids) if task_ids else 'none'}"
                ),
                "observed_at": round(now, 3),
            },
            project=project,
        )
    except Exception:
        return


def snapshot(
    project: str,
    *,
    sessions_provider: Optional[Callable[[str], Iterable[dict]]] = None,
    event_sink: Optional[Callable[..., None]] = None,
    now: Optional[float] = None,
    bound_s: Optional[float] = None,
) -> dict:
    """Return current silent-PTY progress faults for live leases.

    A session qualifies only while its durable row remains live (ready/running,
    not stale) and ``last_output_at`` is older than the configured bound.
    Fresh output, terminalization, or lease expiry immediately clears the fault.
    Heartbeat renewal alone never clears it.
    """
    ts = float(time.time() if now is None else now)
    bound = _bound_seconds() if bound_s is None else max(0.0, float(bound_s))
    list_sessions = sessions_provider or _default_sessions
    emit = event_sink or _emit_narration_event

    rows = list(list_sessions(project) or [])
    current_faults: dict[str, dict] = {}
    observed_tasks: dict[str, str] = {}
    for row in rows:
        sid = str(row.get("runner_session_id") or "").strip()
        if not sid:
            continue
        observed_tasks[sid] = str(row.get("task_id") or "").strip()
        fault = evaluate_progress(row, now=ts, bound_s=bound)
        if fault is not None:
            current_faults[sid] = fault

    with _LOCK:
        known_tasks = _task_by_session.setdefault(project, {})
        known_tasks.update(observed_tasks)
        previous_active = set(_active_sessions.get(project, set()))
        active = set(current_faults)
        _active_sessions[project] = active
        _faults[project] = dict(current_faults)

        if active != previous_active:
            event_sessions = active if active else previous_active
            task_ids = sorted({
                known_tasks.get(sid, "")
                or str((current_faults.get(sid) or {}).get("task_id") or "")
                for sid in event_sessions
                if known_tasks.get(sid, "")
                or (current_faults.get(sid) or {}).get("task_id")
            })
            emit(
                project,
                active=bool(active),
                task_ids=task_ids,
                session_ids=sorted(event_sessions),
                bound_s=bound,
                now=ts,
                faults={sid: current_faults[sid] for sid in sorted(current_faults)},
            )

        for sid in list(known_tasks):
            if sid not in observed_tasks and sid not in active:
                known_tasks.pop(sid, None)

        task_ids = sorted({
            str(fault.get("task_id") or "").strip()
            for fault in current_faults.values()
            if str(fault.get("task_id") or "").strip()
        })
        ages = {
            sid: float(fault.get("output_age_s") or 0)
            for sid, fault in sorted(current_faults.items())
        }

    return {
        "schema": "switchboard.runner_progress_monitor.v1",
        "bound_s": bound,
        "active": bool(active),
        "count": len(active),
        "task_ids": task_ids,
        "runner_session_ids": sorted(active),
        "output_age_s": ages,
        "faults": {sid: current_faults[sid] for sid in sorted(current_faults)},
    }


def reset_for_tests() -> None:
    with _LOCK:
        _active_sessions.clear()
        _task_by_session.clear()
        _faults.clear()
