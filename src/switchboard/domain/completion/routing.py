"""Route-aware Autopilot candidate selection.

The completion state machine owns what happens next; board status is a coarse
projection of it (see ``docs/AUTOPILOT-COMPLETION-STATE-MACHINE.md``).  Two
completion routes both project onto board ``Blocked``:

``remediation``
    an automatic failed gate whose repair the machine drives itself;
``human``
    a sticky authority/policy blocker that must not be auto-dispatched.

Selecting candidates by status alone cannot tell those apart, so it must treat
every ``Blocked`` task the same way -- and today that means producing no
candidate at all.  That is exactly why COORD-20 currently reopens remediation
to ``Not Started``.  This module is the single predicate every candidate layer
uses instead, so the ``In Review -> Blocked(route=remediation) -> In Progress``
projection can land without silently stopping remediation dispatch.

Fail-closed is the rule throughout: an unknown, absent, or unreadable route is
never dispatchable.
"""
from __future__ import annotations

from typing import Any, Mapping


#: Routes the completion machine drives to a fresh generation on its own.
AUTOMATIC_ROUTES = frozenset({
    "remediation", "review_merge", "coordination_retry", "reconcile",
})

#: Routes that exist but deliberately do not produce a dispatch candidate.
#: ``wait`` has an owner already; ``human`` and ``none`` are terminal for
#: automation.
NON_DISPATCH_ROUTES = frozenset({"wait", "human", "none"})

#: Board statuses whose dispatchability is decided by the completion route
#: rather than by the status itself.
ROUTE_KEYED_STATUSES = frozenset({"Blocked"})


def _text(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def route_allows_dispatch(route: Any) -> bool:
    """True only for routes the machine may automatically dispatch."""
    return _text(route) in AUTOMATIC_ROUTES


def completion_route(detail: Mapping[str, Any] | None) -> str:
    """Read the active completion route already carried by a task projection.

    Returns ``""`` when the projection carries no route.  Callers that can
    reach storage should use :func:`resolve_completion_route` instead.
    """
    if not isinstance(detail, Mapping):
        return ""
    candidates = (
        detail.get("completion_run"),
        detail.get("completion"),
        (detail.get("agent_state") or {}).get("completion_run")
        if isinstance(detail.get("agent_state"), Mapping) else None,
    )
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            route = _text(candidate.get("route"))
            if route:
                return route
    return _text(detail.get("completion_route"))


def resolve_completion_route(detail: Mapping[str, Any] | None, *,
                             store: Any = None, project: str = "") -> str:
    """Route from the projection, falling back to the durable completion run.

    A storage failure resolves to ``""`` rather than raising: the caller is a
    selection predicate, and an unreadable route must leave a ``Blocked`` task
    non-dispatchable instead of taking the whole coordinator tick down.
    """
    route = completion_route(detail)
    if route or store is None:
        return route
    task_id = str((detail or {}).get("task_id") or "").strip().upper()
    if not task_id:
        return ""
    try:
        run = store.get_active_completion_run(task_id, project=project) or {}
    except Exception:  # noqa: BLE001 - selection must fail closed, never raise
        return ""
    return _text(run.get("route")) if isinstance(run, Mapping) else ""


def task_ready_for_dispatch(detail: Mapping[str, Any] | None, *,
                            route: str | None = None,
                            store: Any = None, project: str = "") -> bool:
    """The one predicate for "may Autopilot dispatch this task now?".

    Status keeps deciding the cases it can decide.  ``Blocked`` is decided by
    the completion route, and still has to satisfy the same dependency and
    claim safety checks as any other candidate.
    """
    from switchboard.domain.board.tasks import READY_TASK_STATUSES

    if not isinstance(detail, Mapping):
        return False
    status = str(detail.get("status") or "").strip()
    claims = detail.get("active_claims") or []
    ready = bool((detail.get("dependency_state") or {}).get("ready"))

    if status in ROUTE_KEYED_STATUSES:
        resolved = _text(route) if route is not None else resolve_completion_route(
            detail, store=store, project=project)
        # An automatic route makes a Blocked task visible again, but never
        # exempts it from dependency or ownership safety.
        return bool(route_allows_dispatch(resolved) and ready and not claims)
    if status in READY_TASK_STATUSES:
        return bool(ready and not claims)
    return bool(status == "In Review" or (status == "In Progress" and not claims))


__all__ = [
    "AUTOMATIC_ROUTES",
    "NON_DISPATCH_ROUTES",
    "ROUTE_KEYED_STATUSES",
    "completion_route",
    "resolve_completion_route",
    "route_allows_dispatch",
    "task_ready_for_dispatch",
]
