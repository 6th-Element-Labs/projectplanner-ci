"""The one execution-liveness vocabulary and predicate (ADR-0008 plane 1).

Capacity owns physical execution presence. This module is the single place that
answers *"is this managed execution alive?"* — and it answers it from exactly two
facts: the execution's lifecycle status, and whether its renewable lease has
expired.

Before SIMPLIFY-18 the repo carried at least six spellings of the terminal
status set and several independent staleness computations, so the answer varied
by caller. Everything that needs the answer now imports it from here.

Deliberately pure: no storage, no I/O. The DB-backed authority that resolves a
task's live execution lives with ``runner_sessions`` in the runner repository,
and consumes these predicates. Claims, Work Sessions, agent presence, and wake
intents are ownership, evidence, diagnostics, or transport — never liveness, and
never unioned in.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

SCHEMA = "switchboard.execution_liveness.v1"

#: A managed execution that has reached one of these states is finished. The
#: state is durable: no heartbeat can bring it back, because a fenced or stopped
#: generation may not renew (SIMPLIFY-17 / SIMPLIFY-20).
TERMINAL_EXECUTION_STATES: frozenset[str] = frozenset({
    "completed", "failed", "cancelled", "expired", "lost", "killed",
    "exited", "stopped",
})

#: States a supervised execution passes through while it is still ours to
#: control. ``unknown`` is included on purpose: an execution that registered but
#: has not yet reported a status is not evidence of death, and its lease expiry
#: is what will eventually settle the question.
LIVE_EXECUTION_STATES: frozenset[str] = frozenset({
    "starting", "ready", "running", "stopping", "unknown",
})

#: Execution roles. ``start_task`` permits only one nonterminal physical
#: generation per task; role is part of the identity used to decide whether a
#: repeated start is an idempotent attach or a conflicting request.
EXECUTION_ROLES: frozenset[str] = frozenset({
    "implementation", "review_merge", "remediation",
})

DEFAULT_HEARTBEAT_TTL_S = 60
#: Floor matching the runner repository's own clamp, so a pathological TTL
#: cannot make an execution immortal or instantly dead.
MIN_HEARTBEAT_TTL_S = 10


def normalize_status(status: Any) -> str:
    return str(status or "").strip().lower()


def is_terminal(status: Any) -> bool:
    """True when this lifecycle status is durably finished."""
    return normalize_status(status) in TERMINAL_EXECUTION_STATES


def ttl_seconds(row: Mapping[str, Any]) -> int:
    try:
        ttl = int(row.get("heartbeat_ttl_s") or DEFAULT_HEARTBEAT_TTL_S)
    except (TypeError, ValueError):
        ttl = DEFAULT_HEARTBEAT_TTL_S
    return max(MIN_HEARTBEAT_TTL_S, ttl)


def expires_at(row: Mapping[str, Any]) -> float:
    """When this execution's renewable lease lapses without a heartbeat."""
    try:
        heartbeat = float(row.get("heartbeat_at") or 0.0)
    except (TypeError, ValueError):
        heartbeat = 0.0
    return heartbeat + ttl_seconds(row)


def is_expired(row: Mapping[str, Any], *, now: float) -> bool:
    return float(now) >= expires_at(row)


def is_live(row: Mapping[str, Any], *, now: float) -> bool:
    """The one liveness predicate.

    Alive means both halves hold: the status is not terminal, and the lease has
    not lapsed. Either alone is insufficient — a terminal row with a fresh
    heartbeat is dead, and a ``running`` row whose host stopped heartbeating is
    dead once its TTL passes.
    """
    if is_terminal(row.get("status")):
        return False
    return not is_expired(row, now=now)


def execution_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    """Server-owned execution identity carried on a runner_sessions row.

    Fleet and every operator surface render exactly these fields, so a live row
    can always be traced to one generation and one fence epoch.
    """
    metadata = row.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    return {
        "schema": SCHEMA,
        "execution_id": str(metadata.get("execution_id") or "") or None,
        "generation": _int_or_none(metadata.get("execution_generation")),
        "role": str(metadata.get("execution_role")
                    or metadata.get("role") or "") or None,
        "head_sha": str(metadata.get("execution_head_sha")
                        or metadata.get("head_sha") or "") or None,
        "assignment_id": str(metadata.get("assignment_id") or "") or None,
        "fence_epoch": _int_or_none(metadata.get("lease_epoch")),
        "lease_state": str(metadata.get("lease_state") or "") or None,
        "status": normalize_status(row.get("status")),
        "expires_at": expires_at(row),
        "heartbeat_ttl_s": ttl_seconds(row),
    }


def fence_epoch_of(row: Mapping[str, Any]) -> int:
    metadata = row.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    return _int_or_none(metadata.get("lease_epoch")) or 0


def heartbeat_is_fenced(row: Mapping[str, Any], *, claimed_epoch: Any) -> bool:
    """True when a heartbeat does not carry the exact current fence epoch.

    A superseded generation may still have a live process briefly. Its renewals
    must not resurrect the lease, or completion could never be terminal
    (ADR-0008 C2/C3). Future and malformed epochs also fail closed: the fence is
    server-owned, so a host may renew only the exact epoch it was assigned.
    """
    current = fence_epoch_of(row)
    if claimed_epoch in (None, ""):
        return current > 0
    try:
        claimed = int(claimed_epoch)
    except (TypeError, ValueError):
        return True
    return claimed != current


def _int_or_none(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed


__all__ = [
    "SCHEMA",
    "TERMINAL_EXECUTION_STATES",
    "LIVE_EXECUTION_STATES",
    "EXECUTION_ROLES",
    "DEFAULT_HEARTBEAT_TTL_S",
    "MIN_HEARTBEAT_TTL_S",
    "normalize_status",
    "is_terminal",
    "ttl_seconds",
    "expires_at",
    "is_expired",
    "is_live",
    "execution_identity",
    "fence_epoch_of",
    "heartbeat_is_fenced",
]
