"""The complete Mission Bot v5 state machine."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


STATES = frozenset({"ACTIVE", "WAITING", "HUMAN", "DONE"})
ROLES = frozenset({"implementation", "review_merge", "remediation"})


def decide_mission_transition(context: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the complete v5 pager rule without side effects.

    Authentication and scope fencing are command-boundary concerns.  This
    function does not infer them from claims, Work Sessions, hosts, messages,
    board status, wake intents, or provider data.
    """
    state = str(context.get("mission_state") or "").upper()
    role = str(context.get("requested_role") or "")
    if state not in STATES:
        raise ValueError(f"invalid mission_state: {state!r}")
    if role not in ROLES:
        raise ValueError(f"invalid requested_role: {role!r}")

    if not context.get("scope_active"):
        return {"state": state, "action": "wait", "reason": "scope_inactive"}
    if context.get("terminal_provenance"):
        return {"state": "DONE", "action": "wait", "reason": "terminal_provenance"}
    if not context.get("dependencies_satisfied", False):
        return {"state": state, "action": "wait", "reason": "dependencies_unmet"}
    if state == "HUMAN":
        return {"state": "HUMAN", "action": "wait", "reason": "human_requested"}
    if context.get("runner_live"):
        return {"state": state, "action": "wait", "reason": "runner_live"}

    handled = int(context.get("handled_through") or 0)
    latest = int(context.get("latest_sequence") or 0)
    if latest > handled:
        return {
            "state": "ACTIVE",
            "action": "start_task",
            "reason": "unhandled_event",
            "requested_role": role,
            "event_pointer": handled + 1,
        }
    return {"state": state, "action": "wait", "reason": "no_unhandled_event"}


__all__ = ["ROLES", "STATES", "decide_mission_transition"]
