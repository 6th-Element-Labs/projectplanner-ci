"""Wakeable narration worker — claim, lease, coalesce, retry, recover (NARRATE-9, ADR-0008).

The consumer half of the event-driven narrator. It drains the NARRATE-8 outbox with
at-least-once delivery and idempotent, per-entity-monotonic progress:

- **Claim (boundary 2):** in one transaction, select actionable work in per-entity revision
  order, suppress stale/superseded revisions *without* a provider call (coalescing), and
  atomically move the current revision to ``claimed`` with a bounded lease and an incremented
  attempt count. No provider/network work happens inside the transaction.
- **Generate + settle:** the provider callback runs *after* the claim commits; success →
  ``delivered``, failure → bounded-backoff ``retry_wait`` and finally ``dead_letter``. A worker
  crash leaves ``claimed`` work recoverable only after its lease expires.
- **Recovery sweep:** a cheap indexed query over pending / retry-ready / expired-lease rows,
  so a lost wake or a dead worker self-heals on the recovery timer.

NARRATE-9 ships the machine and takes the provider ``generate`` callback by injection; it does
not itself publish visible narration or call an LLM (NARRATE-11/12 own generation, NARRATE-14
owns the primary-path cutover). In shadow mode the caller passes a non-publishing ``generate``.
"""
from __future__ import annotations

import random
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import narration_outbox
from db.core import _retry_on_locked

DEFAULT_LEASE_SECONDS = 120.0
DEFAULT_MAX_ATTEMPTS = 5
RETRY_BASE_SECONDS = 30.0
RETRY_CAP_SECONDS = 1800.0

# Actionable = a pending row, a retry whose backoff has elapsed, or a claimed row whose lease
# has expired (its worker died). Ordered oldest-intent first for cross-entity fairness, then by
# revision so per-entity coalescing sees the whole revision chain together.
_ACTIONABLE_SQL = (
    "SELECT * FROM narration_outbox WHERE "
    "attempt_state = 'pending' "
    "OR (attempt_state = 'retry_wait' AND available_at <= ?) "
    "OR (attempt_state = 'claimed' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?) "
    "ORDER BY requested_at, entity_type, entity_id, source_revision"
)


def _now(now: Optional[float]) -> float:
    return time.time() if now is None else now


def _entity_current(c, entity_type: str, entity_id: str) -> Tuple[int, Optional[str]]:
    """Current (revision, source_hash) for the entity the request targets."""
    table = "tasks" if entity_type == "task" else "deliverables"
    key_col = "task_id" if entity_type == "task" else "id"
    row = c.execute(
        f"SELECT narration_source_revision AS rev, narration_source_hash AS hash "
        f"FROM {table} WHERE {key_col}=?",
        (entity_id,),
    ).fetchone()
    if row is None:
        return (0, None)
    return (row["rev"] or 0, row["hash"])


def _classify(row_rev: int, row_hash: str, cur_rev: int, cur_hash: Optional[str]) -> str:
    """ready | current | stale — mirrors narration_events.request_disposition without a full
    envelope re-validation (the row is already a committed, validated envelope)."""
    if row_rev < cur_rev:
        return "stale"
    if row_rev == cur_rev:
        return "current" if row_hash == cur_hash else "stale"
    return "ready"


# Re-assert that a row is still actionable at write time, so a claim/supersede that lost the
# race to a concurrent drainer or the recovery timer matches zero rows instead of stealing an
# in-flight claim. Never touch a row whose lease is still live (owned by another worker).
_STILL_ACTIONABLE = (
    "(attempt_state='pending' "
    "OR (attempt_state='retry_wait' AND available_at<=?) "
    "OR (attempt_state='claimed' AND lease_expires_at IS NOT NULL AND lease_expires_at<=?))"
)


def _supersede(c, event_id: str, now: float) -> None:
    c.execute(
        "UPDATE narration_outbox SET attempt_state='superseded', claimed_by=NULL, "
        f"lease_expires_at=NULL, updated_at=? WHERE event_id=? AND {_STILL_ACTIONABLE}",
        (now, event_id, now, now),
    )


def claim_next_narration(project: str, *, worker_id: str, now: Optional[float] = None,
                         lease_seconds: float = DEFAULT_LEASE_SECONDS
                         ) -> Optional[Dict[str, Any]]:
    """Atomically claim the freshest actionable request for one entity, superseding its stale
    revisions in the same transaction. Returns the claimed event, or None if nothing is
    actionable. Any superseding it does still commits even when it returns None (progress).

    Wrapped in ``_retry_on_locked`` and guarded by conditional writes so two concurrent
    drainers (e.g. a wake-driven drain overlapping the recovery timer) can never both win the
    same request: the loser's conditional UPDATE matches zero rows or retries on a fresh
    snapshot. SQLite serializes the single writer; the guard closes the read-then-write gap."""
    return _retry_on_locked(
        lambda: _claim_next_impl(project, worker_id=worker_id, now=_now(now),
                                 lease_seconds=lease_seconds))


def _claim_next_impl(project: str, *, worker_id: str, now: float,
                     lease_seconds: float) -> Optional[Dict[str, Any]]:
    with narration_outbox._conn(project) as c:
        rows = c.execute(_ACTIONABLE_SQL, (now, now)).fetchall()
        if not rows:
            return None
        # Group actionable rows by entity, preserving oldest-intent encounter order.
        order: List[Tuple[str, str]] = []
        by_entity: Dict[Tuple[str, str], List[Any]] = {}
        for r in rows:
            key = (r["entity_type"], r["entity_id"])
            if key not in by_entity:
                by_entity[key] = []
                order.append(key)
            by_entity[key].append(r)
        for key in order:
            cur_rev, cur_hash = _entity_current(c, key[0], key[1])
            target = None
            for r in by_entity[key]:
                if _classify(r["source_revision"], r["source_hash"], cur_rev, cur_hash) == "stale":
                    _supersede(c, r["event_id"], now)  # coalesce / stale-suppress, no provider call
                else:
                    target = r  # the current/ready revision for this entity
            if target is not None:
                # Anchor the lease to max(now, available_at) so it always strictly follows
                # available_at (a contract invariant) even under clock skew; in production now
                # is monotonic and >= available_at, so this is just now + lease_seconds.
                lease_end = max(now, target["available_at"]) + lease_seconds
                cur = c.execute(
                    "UPDATE narration_outbox SET attempt_state='claimed', claimed_by=?, "
                    "lease_expires_at=?, attempt_count=attempt_count+1, updated_at=? "
                    f"WHERE event_id=? AND {_STILL_ACTIONABLE}",
                    (worker_id, lease_end, now, target["event_id"], now, now),
                )
                if cur.rowcount == 0:
                    continue  # a concurrent drainer claimed it first; try the next entity
                fresh = c.execute("SELECT * FROM narration_outbox WHERE event_id=?",
                                  (target["event_id"],)).fetchone()
                return narration_outbox._row_to_event(fresh)
        # Every actionable entity coalesced to nothing (all revisions stale); supersessions
        # above are committed. The caller's drain loop stops on this None.
        return None


# A settle only takes effect if the caller still holds the exact lease it was granted
# (claimed_by + lease_expires_at as a fencing token). A zombie worker whose lease expired and
# whose request was reclaimed by another worker therefore cannot clobber the new owner's live
# lease — its late result is dropped. Mirrors the guarded claim path.
_OWNS_LEASE = "attempt_state='claimed' AND claimed_by=? AND lease_expires_at=?"


def _settle(project: str, event_id: str, *, state: str, now: float, worker_id: str,
            lease: float, available_at: Optional[float] = None,
            last_error: Optional[str] = None) -> bool:
    """Move an owned claimed row to a terminal/retry state, always clearing the lease (the
    contract forbids a non-claimed row from carrying an active lease). Returns False (no-op)
    if the caller no longer owns the lease."""
    with narration_outbox._conn(project) as c:
        cur = c.execute(
            "UPDATE narration_outbox SET attempt_state=?, claimed_by=NULL, lease_expires_at=NULL, "
            f"available_at=COALESCE(?, available_at), last_error=?, updated_at=? "
            f"WHERE event_id=? AND {_OWNS_LEASE}",
            (state, available_at, last_error, now, event_id, worker_id, lease),
        )
        return cur.rowcount > 0


def mark_delivered(project: str, event_id: str, *, worker_id: str, lease: float,
                   now: Optional[float] = None) -> bool:
    return _settle(project, event_id, state="delivered", now=_now(now),
                   worker_id=worker_id, lease=lease, last_error=None)


def mark_superseded(project: str, event_id: str, *, worker_id: str, lease: float,
                    now: Optional[float] = None) -> bool:
    return _settle(project, event_id, state="superseded", now=_now(now),
                   worker_id=worker_id, lease=lease, last_error=None)


def mark_dead_letter(project: str, event_id: str, *, worker_id: str, lease: float, error: str,
                     now: Optional[float] = None) -> bool:
    return _settle(project, event_id, state="dead_letter", now=_now(now),
                   worker_id=worker_id, lease=lease, last_error=error or "dead_letter")


def _retry_delay(attempt_count: int, base: float, cap: float, jitter: Optional[float]) -> float:
    exp = min(cap, base * (2 ** max(0, attempt_count - 1)))
    j = random.random() if jitter is None else jitter
    return exp + j * base


def mark_retry(project: str, event_id: str, *, worker_id: str, lease: float, error: str,
               now: Optional[float] = None, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
               base: float = RETRY_BASE_SECONDS, cap: float = RETRY_CAP_SECONDS,
               jitter: Optional[float] = None) -> str:
    """Bounded exponential backoff with jitter; escalate to dead_letter at the attempt ceiling.
    Retains the original error. No-op ('not_owned') if the caller's lease was reclaimed. Returns
    the resulting state ('retry_wait', 'dead_letter', 'not_owned', or 'missing')."""
    now = _now(now)
    with narration_outbox._conn(project) as c:
        row = c.execute(
            f"SELECT attempt_count, requested_at FROM narration_outbox "
            f"WHERE event_id=? AND {_OWNS_LEASE}",
            (event_id, worker_id, lease),
        ).fetchone()
        if row is None:
            # Either the row is gone or this worker no longer owns the lease (reclaimed).
            return "missing" if get_outbox_state(project, event_id) is None else "not_owned"
        count = row["attempt_count"] or 0
        if count >= max_attempts:
            c.execute(
                "UPDATE narration_outbox SET attempt_state='dead_letter', claimed_by=NULL, "
                f"lease_expires_at=NULL, last_error=?, updated_at=? WHERE event_id=? AND {_OWNS_LEASE}",
                (error or "dead_letter", now, event_id, worker_id, lease),
            )
            return "dead_letter"
        # available_at must never precede requested_at (contract invariant).
        available_at = max(now + _retry_delay(count, base, cap, jitter), row["requested_at"])
        c.execute(
            "UPDATE narration_outbox SET attempt_state='retry_wait', claimed_by=NULL, "
            f"lease_expires_at=NULL, available_at=?, last_error=?, updated_at=? "
            f"WHERE event_id=? AND {_OWNS_LEASE}",
            (available_at, error or "retry", now, event_id, worker_id, lease),
        )
        return "retry_wait"


def get_outbox_state(project: str, event_id: str) -> Optional[str]:
    with narration_outbox._conn(project) as c:
        r = c.execute("SELECT attempt_state FROM narration_outbox WHERE event_id=?",
                      (event_id,)).fetchone()
        return r["attempt_state"] if r else None


def list_actionable(project: str, *, now: Optional[float] = None,
                    limit: int = 200) -> List[Dict[str, Any]]:
    """Recovery-sweep view: pending, retry-ready, or expired-lease rows (indexed query)."""
    now = _now(now)
    with narration_outbox._conn(project) as c:
        rows = c.execute(_ACTIONABLE_SQL + " LIMIT ?", (now, now, limit)).fetchall()
        return [narration_outbox._row_to_event(r) for r in rows]


def count_actionable(project: str, *, now: Optional[float] = None) -> int:
    now = _now(now)
    with narration_outbox._conn(project) as c:
        return c.execute(
            "SELECT COUNT(*) FROM narration_outbox WHERE "
            "attempt_state = 'pending' "
            "OR (attempt_state = 'retry_wait' AND available_at <= ?) "
            "OR (attempt_state = 'claimed' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)",
            (now, now),
        ).fetchone()[0]


def drain(project: str, *, worker_id: str, generate: Callable[[Dict[str, Any]], Any],
          now_fn: Callable[[], float] = time.time, max_items: int = 100,
          lease_seconds: float = DEFAULT_LEASE_SECONDS,
          max_attempts: int = DEFAULT_MAX_ATTEMPTS,
          jitter: Optional[float] = None) -> List[Tuple[str, str]]:
    """Drain actionable work: claim → generate (outside the transaction) → settle.

    ``generate(event)`` performs provider work and returns a receipt on success; it is injected,
    so NARRATE-9 stays provider-agnostic and shadow mode passes a non-publishing callback. A
    raised exception drops the claim into bounded retry / dead_letter. Returns a list of
    ``(event_id, outcome)`` where outcome is delivered | retry_wait | dead_letter.
    """
    results: List[Tuple[str, str]] = []
    for _ in range(max_items):
        event = claim_next_narration(project, worker_id=worker_id, now=now_fn(),
                                     lease_seconds=lease_seconds)
        if event is None:
            break
        event_id = event["event_id"]
        # Fencing token for the settle: the exact lease this claim was granted. If the lease
        # expires mid-generate and another worker reclaims, our settle no-ops instead of
        # clobbering the new owner.
        wid = event["attempt"]["claimed_by"]
        lease = event["attempt"]["lease_expires_at"]
        try:
            generate(event)
        except Exception as exc:  # provider failure — settle into retry/dead_letter, never lose it
            outcome = mark_retry(project, event_id, worker_id=wid, lease=lease, error=repr(exc),
                                 now=now_fn(), max_attempts=max_attempts, jitter=jitter)
            results.append((event_id, outcome))
            continue
        delivered = mark_delivered(project, event_id, worker_id=wid, lease=lease, now=now_fn())
        results.append((event_id, "delivered" if delivered else "lease_lost"))
    return results
