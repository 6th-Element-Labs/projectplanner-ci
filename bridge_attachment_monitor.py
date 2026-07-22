"""Trailing-window alarm for live runner rows without a relay host bridge (WATCH-6)."""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Iterable, Optional


DEFAULT_UNATTACHED_WINDOW_S = 300.0
_WATCHABLE_STATUSES = {"ready", "running"}
_LOCK = threading.RLock()
_first_unattached: dict[str, dict[str, float]] = {}
_active_sessions: dict[str, set[str]] = {}
_task_by_session: dict[str, dict[str, str]] = {}


def _window_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get(
            "PM_RUNNER_BRIDGE_UNATTACHED_WINDOW_S",
            str(DEFAULT_UNATTACHED_WINDOW_S),
        )))
    except (TypeError, ValueError):
        return DEFAULT_UNATTACHED_WINDOW_S


def _default_sessions(project: str) -> Iterable[dict]:
    from switchboard.storage.repositories import runner

    return runner.list_runner_sessions(project=project)


def _default_attachment(runner_session_id: str) -> Optional[bool]:
    from switchboard.application import runner_pty_relay

    return runner_pty_relay.host_attached_for(runner_session_id)


def _emit_narration_event(project: str, *, active: bool,
                          task_ids: list[str], session_ids: list[str],
                          window_s: float, now: float) -> None:
    """Write one transition event; repeated health polls remain side-effect free."""
    try:
        from switchboard.storage.repositories.activity import append_activity

        state = "raised" if active else "cleared"
        append_activity(
            f"narration.runner_bridge_alarm_{state}",
            "switchboard/runner-bridge-monitor",
            {
                "schema": "switchboard.runner_bridge_alarm.v1",
                "state": state,
                "task_ids": task_ids,
                "runner_session_ids": session_ids,
                "window_s": window_s,
                "message": (
                    f"Runner relay bridge alarm {state}; affected tasks: "
                    f"{', '.join(task_ids) if task_ids else 'none'}"
                ),
                "observed_at": round(now, 3),
            },
            project=project,
        )
    except Exception:
        # Saturation must remain observable even if its audit/narration sink is down.
        return


def snapshot(
    project: str,
    *,
    sessions_provider: Optional[Callable[[str], Iterable[dict]]] = None,
    attachment_provider: Optional[Callable[[str], Optional[bool]]] = None,
    event_sink: Optional[Callable[..., None]] = None,
    now: Optional[float] = None,
    window_s: Optional[float] = None,
) -> dict:
    """Return current dark-runner state using continuous trailing-window semantics.

    A session qualifies only while its durable row remains ``ready``/``running`` and
    the owning RelayHub explicitly reports ``host_attached=False``. Reattachment,
    terminal status, disappearance, or an unknown off-process attachment signal
    immediately removes it from the window and clears any active alarm.
    """
    ts = float(time.time() if now is None else now)
    threshold = _window_seconds() if window_s is None else max(0.0, float(window_s))
    list_sessions = sessions_provider or _default_sessions
    resolve_attachment = attachment_provider or _default_attachment
    emit = event_sink or _emit_narration_event

    rows = list(list_sessions(project) or [])
    current: dict[str, dict] = {}
    observed_tasks: dict[str, str] = {}
    for row in rows:
        sid = str(row.get("runner_session_id") or "").strip()
        status = str(row.get("status") or "").strip().lower()
        if not sid or status not in _WATCHABLE_STATUSES:
            continue
        observed_tasks[sid] = str(row.get("task_id") or "").strip()
        try:
            attached = resolve_attachment(sid)
        except Exception:
            attached = None
        if attached is False:
            current[sid] = row

    with _LOCK:
        first = _first_unattached.setdefault(project, {})
        known_tasks = _task_by_session.setdefault(project, {})
        known_tasks.update(observed_tasks)
        previous_active = set(_active_sessions.get(project, set()))
        # Anything that no longer has an explicit negative attachment signal is
        # outside the trailing window. This is what makes the alarm self-clearing.
        for sid in list(first):
            if sid not in current:
                first.pop(sid, None)
        for sid in current:
            first.setdefault(sid, ts)

        active = {
            sid for sid in current
            if ts - float(first.get(sid, ts)) >= threshold
        }
        _active_sessions[project] = active

        if active != previous_active:
            event_sessions = active if active else previous_active
            task_ids = sorted({
                known_tasks.get(sid, "") for sid in event_sessions
                if known_tasks.get(sid, "")
            })
            emit(
                project,
                active=bool(active),
                task_ids=task_ids,
                session_ids=sorted(event_sessions),
                window_s=threshold,
                now=ts,
            )

        for sid in list(known_tasks):
            if sid not in observed_tasks and sid not in active:
                known_tasks.pop(sid, None)

        affected_rows = [current[sid] for sid in sorted(active) if sid in current]
        task_ids = sorted({
            str(row.get("task_id") or "").strip()
            for row in affected_rows if str(row.get("task_id") or "").strip()
        })
        durations = {
            sid: round(max(0.0, ts - float(first[sid])), 3) for sid in sorted(active)
        }

    return {
        "schema": "switchboard.runner_bridge_monitor.v1",
        "window_s": threshold,
        "active": bool(active),
        "count": len(active),
        "task_ids": task_ids,
        "runner_session_ids": sorted(active),
        "unattached_for_s": durations,
    }


def reset_for_tests() -> None:
    with _LOCK:
        _first_unattached.clear()
        _active_sessions.clear()
        _task_by_session.clear()
