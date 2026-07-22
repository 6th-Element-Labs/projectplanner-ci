"""Trailing-window alarm for live runner rows without a relay host bridge (WATCH-6)."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional


DEFAULT_UNATTACHED_WINDOW_S = 300.0
DEFAULT_WATCHABILITY_TARGET = 0.99
DEFAULT_STARTUP_GRACE_S = 300.0
_WATCHABLE_STATUSES = {"ready", "running"}
_LOCK = threading.RLock()
_first_unattached: dict[str, dict[str, float]] = {}
_active_sessions: dict[str, set[str]] = {}
_task_by_session: dict[str, dict[str, str]] = {}
_slo_state: dict[str, dict] = {}
_slo_target: dict[str, float] = {}


def _window_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get(
            "PM_RUNNER_BRIDGE_UNATTACHED_WINDOW_S",
            str(DEFAULT_UNATTACHED_WINDOW_S),
        )))
    except (TypeError, ValueError):
        return DEFAULT_UNATTACHED_WINDOW_S


def _startup_grace_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get(
            "PM_RUNNER_WATCHABILITY_STARTUP_GRACE_S",
            str(DEFAULT_STARTUP_GRACE_S),
        )))
    except (TypeError, ValueError):
        return DEFAULT_STARTUP_GRACE_S


def _watchability_target(project: str) -> float:
    """Return the committed target; environment and runtime may only tighten it."""
    committed = DEFAULT_WATCHABILITY_TARGET
    try:
        payload = json.loads((Path(__file__).parent / "perf" / "watchability_slo.json").read_text(
            encoding="utf-8"))
        committed = float(payload.get("target", DEFAULT_WATCHABILITY_TARGET))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        committed = DEFAULT_WATCHABILITY_TARGET
    committed = min(1.0, max(DEFAULT_WATCHABILITY_TARGET, committed))
    try:
        configured = float(os.environ.get(
            "PM_RUNNER_WATCHABILITY_TARGET", str(committed)))
    except (TypeError, ValueError):
        configured = committed
    configured = min(1.0, max(committed, configured))
    target = max(_slo_target.get(project, committed), configured)
    _slo_target[project] = target
    return target


def _metadata(row: dict) -> dict:
    value = row.get("metadata")
    return value if isinstance(value, dict) else {}


def _expected_watchable(row: dict) -> bool:
    """Scope the denominator to live native PTY runs with a bridge contract."""
    metadata = _metadata(row)
    control = row.get("control") if isinstance(row.get("control"), dict) else {}
    status = str(row.get("status") or "").strip().lower()
    return bool(
        status in _WATCHABLE_STATUSES
        and metadata.get("native_host_execution") is True
        and metadata.get("pty") is True
        and control.get("runner_open") is not False
        and not metadata.get("terminal_cleanup_requested_at")
    )


def _sample_watchability(
    project: str,
    rows: list[dict],
    resolve_attachment: Callable[[str], Optional[bool]],
    ts: float,
) -> dict:
    """Accumulate sampled eligible seconds, globally and per host."""
    state = _slo_state.setdefault(project, {
        "sessions": {}, "eligible_s": 0.0, "attached_s": 0.0,
        "hosts": {}, "started_at": ts,
    })
    grace_s = _startup_grace_seconds()
    seen: set[str] = set()
    for row in rows:
        sid = str(row.get("runner_session_id") or "").strip()
        if not sid:
            continue
        seen.add(sid)
        session = state["sessions"].setdefault(sid, {
            "first_seen": ts, "last_seen": ts, "attached": None,
            "ever_attached": False, "eligible": False,
            "host_id": str(row.get("host_id") or "unknown"),
        })
        eligible_from = float(session["last_seen"])
        if not session["ever_attached"]:
            eligible_from = max(eligible_from, float(session["first_seen"]) + grace_s)
        eligible_s = max(0.0, ts - eligible_from) if session["eligible"] else 0.0
        if eligible_s:
            host = state["hosts"].setdefault(
                session["host_id"], {"eligible_s": 0.0, "attached_s": 0.0})
            state["eligible_s"] += eligible_s
            host["eligible_s"] += eligible_s
            if session["attached"] is True:
                state["attached_s"] += eligible_s
                host["attached_s"] += eligible_s
        attached = None
        currently_eligible = _expected_watchable(row)
        if currently_eligible:
            try:
                attached = resolve_attachment(sid)
            except Exception:
                attached = None
        session.update({
            "last_seen": ts,
            "attached": attached,
            "ever_attached": bool(session["ever_attached"] or attached is True),
            "eligible": currently_eligible,
            "host_id": str(row.get("host_id") or "unknown"),
        })

    # Disappeared/terminal rows do not accrue teardown time and are forgotten.
    for sid in list(state["sessions"]):
        if sid not in seen:
            state["sessions"].pop(sid, None)

    target = _watchability_target(project)
    eligible_s = float(state["eligible_s"])
    attached_s = float(state["attached_s"])

    def view(values: dict) -> dict:
        denominator = float(values["eligible_s"])
        numerator = float(values["attached_s"])
        ratio = numerator / denominator if denominator else None
        return {
            "watchability": round(ratio, 6) if ratio is not None else None,
            "attached_minutes": round(numerator / 60.0, 3),
            "eligible_running_minutes": round(denominator / 60.0, 3),
            "meeting_target": ratio >= target if ratio is not None else None,
        }

    return {
        "schema": "switchboard.runner_watchability_slo.v1",
        "target": target,
        "target_policy": "tighten_only",
        "startup_grace_s": grace_s,
        "measurement_started_at": round(float(state["started_at"]), 3),
        **view({"eligible_s": eligible_s, "attached_s": attached_s}),
        "by_host": {
            host_id: view(values) for host_id, values in sorted(state["hosts"].items())
        },
    }


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
    with _LOCK:
        watchability_slo = _sample_watchability(project, rows, resolve_attachment, ts)
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
        "watchability_slo": watchability_slo,
    }


def reset_for_tests() -> None:
    with _LOCK:
        _first_unattached.clear()
        _active_sessions.clear()
        _task_by_session.clear()
        _slo_state.clear()
        _slo_target.clear()
