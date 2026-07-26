"""Route-aware Autopilot candidate selection.

The completion state machine owns what happens next; board status is a coarse
projection of it (see ``docs/AUTOPILOT-COMPLETION-STATE-MACHINE.md``).  Two
completion routes both project onto board ``Blocked``:

``remediation``
    an automatic failed gate whose repair the machine drives itself;
``human``
    a sticky authority/policy blocker that must not be auto-dispatched.

Selecting candidates by status alone cannot tell those apart.  This module is
the single predicate every candidate layer uses so
``In Review -> Blocked(route=remediation) -> In Progress`` can land without
silently stopping remediation dispatch.

Dependency fields on ``dependency_state`` are not interchangeable:

* ``satisfied`` — deps are done (the gate for Blocked / automatic routes).
* ``ready`` — claimable as fresh Not Started work
  (``status == "Not Started" and satisfied``). Never use ``ready`` as a deps
  check for ``Blocked``; production always has ``ready=False`` there.

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


def _deps_satisfied(dependency_state: Mapping[str, Any] | None) -> bool:
    """True when no unfinished dependency blocks the task.

    Prefer ``satisfied``. Fall back to empty blocking / zero ``blocked_by_count``
    for partial projections that omit the boolean.
    """
    dep = dependency_state if isinstance(dependency_state, Mapping) else {}
    if "satisfied" in dep:
        return bool(dep.get("satisfied"))
    if "blocked_by_count" in dep:
        try:
            return int(dep.get("blocked_by_count") or 0) == 0
        except (TypeError, ValueError):
            return False
    blocking = dep.get("blocking")
    if isinstance(blocking, (list, tuple)):
        return len(blocking) == 0
    # Absent dependency projection: fail closed for route-keyed statuses that
    # explicitly require a deps check; callers with empty maps treat as ok only
    # when satisfied was set. Unknown shape → not satisfied.
    return False


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


def _human_work_session_blocker(detail: Mapping[str, Any]) -> bool:
    """COORD-69: a blocked WS with route=human is sticky — never auto-dispatch."""
    session = detail.get("work_session")
    if not isinstance(session, Mapping):
        session = (detail.get("session_health") or {}).get("latest_sessions")
        if isinstance(session, list) and session:
            session = session[0]
    if not isinstance(session, Mapping):
        return False
    if _text(session.get("status")) != "blocked":
        return False
    hygiene = session.get("hygiene") if isinstance(session.get("hygiene"), Mapping) else {}
    blocker = hygiene.get("blocker") if isinstance(hygiene.get("blocker"), Mapping) else {}
    return _text(blocker.get("route")) == "human"


def task_ready_for_dispatch(detail: Mapping[str, Any] | None, *,
                            route: str | None = None,
                            store: Any = None, project: str = "") -> bool:
    """The one predicate for "may Autopilot dispatch this task now?".

    Status keeps deciding the cases it can decide.  ``Blocked`` is decided by
    the completion route, and still has to satisfy dependency and claim safety.
    Dependency safety for route-keyed statuses uses ``satisfied``, not ``ready``
    (BREAKDOWN 42).
    """
    from switchboard.domain.board.tasks import READY_TASK_STATUSES

    if not isinstance(detail, Mapping):
        return False
    # COORD-69 / DOGFOOD-17: abandon_claim used to reset a human-blocked WS to
    # Not Started. Even if board status is wrong, a human-route WS blocker must
    # fail closed for Autopilot dispatch.
    if _human_work_session_blocker(detail):
        return False
    status = str(detail.get("status") or "").strip()
    claims = detail.get("active_claims") or []
    dep = detail.get("dependency_state") or {}
    dep = dep if isinstance(dep, Mapping) else {}
    # Claimability (Not Started only). Do not use for Blocked.
    ready = bool(dep.get("ready"))

    if status in ROUTE_KEYED_STATUSES:
        resolved = _text(route) if route is not None else resolve_completion_route(
            detail, store=store, project=project)
        # An automatic route makes a Blocked task visible again, but never
        # exempts it from dependency or ownership safety.
        return bool(
            route_allows_dispatch(resolved)
            and _deps_satisfied(dep)
            and not claims
        )
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
