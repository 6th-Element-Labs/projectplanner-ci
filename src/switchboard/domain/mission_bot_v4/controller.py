"""The pure four-state Mission Bot v4 controller rule.

This module accepts already-hydrated authority facts.  It does not read Work
Sessions as liveness, infer Human from board status, or call a start port.  The
scoped worker may execute the returned ``start_task`` action later.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .invariants import active_mission_failure


_AGENT_BINDINGS = frozenset({"registered_agent", "direct_session"})
_ROLES = frozenset({"implementation", "review_merge", "remediation"})


def _authenticated_human_request(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        value.get("schema") == "switchboard.work_session_human_blocker.v1"
        and value.get("source_tool") == "agent_requires_human"
        and value.get("binding") in _AGENT_BINDINGS
        and bool(value.get("agent_id"))
        and value.get("provenance_stamp") == "switchboard.resolve_write_actor.v1"
    )


def decide_mission_transition(context: Mapping[str, Any]) -> dict[str, Any]:
    """Return the v4 state/action without performing a side effect.

    ``board_status`` is intentionally ignored: board Blocked is neither Human
    authority nor Capacity liveness.
    """
    requested_role = str(context.get("requested_role") or "")
    if requested_role not in _ROLES:
        raise ValueError(f"invalid requested_role: {requested_role!r}")

    if not context.get("scope_active"):
        return {
            "result": "wait",
            "state": "WAITING",
            "action": "wait",
            "reason": "scope_inactive",
        }
    if context.get("terminal_provenance"):
        return {
            "result": "done",
            "state": "DONE",
            "action": "wait",
            "reason": "terminal_provenance",
        }
    if not context.get("dependencies_satisfied", False):
        return {
            "result": "wait",
            "state": "WAITING",
            "action": "wait",
            "reason": "dependencies_unmet",
        }
    if (
        str(context.get("mission_state") or "").upper() == "HUMAN"
        or _authenticated_human_request(context.get("human_request"))
    ):
        return {
            "result": "human",
            "state": "HUMAN",
            "action": "wait",
            "reason": "authenticated_agent_request",
        }
    if context.get("runner_live"):
        return {
            "result": "wait",
            "state": "WAITING",
            "action": "wait",
            "reason": "runner_live",
        }
    if context.get("capacity_attempt_pending"):
        return {
            "result": "wait",
            "state": "WAITING",
            "action": "wait",
            "reason": "capacity_attempt_pending",
        }

    failure = active_mission_failure(context)
    if failure is not None:
        return {
            "result": "wait",
            "state": "ACTIVE",
            "action": "block_release",
            "reason": str(failure["reason"]),
            "release_blocked": True,
            "failure": failure,
        }

    handled_through = int(context.get("handled_through") or 0)
    latest_sequence = int(context.get("latest_sequence") or 0)
    if latest_sequence > handled_through:
        return {
            "result": "continue",
            "state": "ACTIVE",
            "action": "start_task",
            "reason": "unhandled_event",
            "requested_role": requested_role,
            "event_pointer": handled_through + 1,
        }
    return {
        "result": "wait",
        "state": "WAITING",
        "action": "wait",
        "reason": "no_unhandled_event",
    }
