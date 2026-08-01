"""Release-blocking invariants for the four-state Mission Bot v4 pager."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


STUCK_MISSION_SCHEMA = "switchboard.mission_stuck_invariant.v1"


def active_mission_failure(
    context: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Name an ACTIVE mission that has no truthful reason to be quiescent.

    This is a detector only.  It does not manufacture an event, infer liveness
    from a claim/Work Session/wake, or request Capacity.  ``runner_live`` must
    already come from ADR-0008 C1 ``runner_sessions`` truth.
    """
    state = str(context.get("mission_state") or "").strip().upper()
    if state != "ACTIVE":
        return None
    if context.get("terminal_provenance") or context.get("runner_live"):
        return None

    handled_through = int(context.get("handled_through") or 0)
    latest_sequence = int(context.get("latest_sequence") or 0)
    if latest_sequence > handled_through:
        return None

    reason = (
        "mission_cursor_ahead"
        if handled_through > latest_sequence
        else "missing_mission_event"
    )
    failure_class = "invalid_input" if reason == "mission_cursor_ahead" else "missing_data"
    message = (
        "Mission cursor is ahead of its append-only journal."
        if reason == "mission_cursor_ahead"
        else (
            "Active mission has no live runner, Human park, or unhandled event; "
            "a required production event is missing."
        )
    )
    return {
        "schema": STUCK_MISSION_SCHEMA,
        "invariant": "active_requires_runner_human_or_unhandled_event",
        "release_blocked": True,
        "reason": reason,
        "failure_class": failure_class,
        "severity": "critical",
        "message": message,
        "expected_signal": (
            "Capacity, Communication, or Coordination publishes the exact durable "
            "fact before the mission is evaluated again."
        ),
        "missing_producer": reason == "missing_mission_event",
        "evidence": {
            "project": str(context.get("project") or ""),
            "task_id": str(context.get("task_id") or ""),
            "mission_state": state,
            "requested_role": str(context.get("requested_role") or ""),
            "handled_through": handled_through,
            "latest_sequence": latest_sequence,
            "runner_live": False,
            "runner_liveness_source": "runner_sessions",
            "human_parked": False,
            "terminal_provenance": False,
        },
    }


__all__ = ["STUCK_MISSION_SCHEMA", "active_mission_failure"]
