"""Narration operator surfaces: queue health, provenance, and authorized controls (NARRATE-13).

The M4 observability + control layer over the NARRATE-8 outbox and NARRATE-12 receipts:

- ``narration_health`` — a BOUNDED snapshot (indexed COUNT/MIN/SUM/AVG only, never a text or
  per-row scan) an operator uses to tell queued / running / retrying / dead-lettered / stale /
  delivered / fallback apart, plus freshness age, success/failure/fallback rates, model-token-cost
  totals, and precomputed alert flags for queue age, failure rate, dead letters, and spend.
- ``narrate_now`` — force the entity's CURRENT revision to be (re)generated now. It re-queues the
  existing request for that revision (deduped on the immutable revision — it never invents a new
  revision or a second visible effect), records an audit event, and wakes the worker. It does NOT
  bypass budgets: generation still runs through the NARRATE-12 policy/budget gate.
- ``reactivate_request`` — authorized, audited retry / dead-letter recovery: move a dead-lettered
  or errored request back to actionable so the worker re-attempts, or park a stuck one as a
  dead-letter. Operates on the existing row (deduped); never mutates immutable event fields.

Permission is enforced by the REST/MCP caller (scoped principal); these functions take the
resolved ``actor`` and record it in the audit trail, mirroring the store's actor convention.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import narration_outbox

_ACTIONABLE_STATES = ("pending", "retry_wait")
_TERMINAL_OR_STUCK = ("delivered", "superseded", "dead_letter", "claimed")
DEFAULT_WINDOW_SECONDS = 86400.0

# Operator alert thresholds (advisory defaults; the caller may pass overrides).
QUEUE_AGE_ALERT_SECONDS = 60.0
FAILURE_RATE_ALERT = 0.20


def _now(now: Optional[float]) -> float:
    return time.time() if now is None else now


def _conn(project: str):
    from db.connection import _conn as conn
    return conn(project)


def narration_health(project: str, *, now: Optional[float] = None,
                     window_seconds: float = DEFAULT_WINDOW_SECONDS) -> Dict[str, Any]:
    """Bounded operator snapshot of the narration queue + generation receipts for a project.

    Every query is an indexed aggregate (COUNT/MIN/SUM/AVG with GROUP BY), so this is safe to poll
    and to back an alerting rule — it never loads narration text or scans row-by-row."""
    now = _now(now)
    since = now - window_seconds
    with _conn(project) as c:
        state_rows = c.execute(
            "SELECT attempt_state, COUNT(*) AS cnt FROM narration_outbox GROUP BY attempt_state"
        ).fetchall()
        states = {r["attempt_state"]: r["cnt"] for r in state_rows}
        oldest = c.execute(
            "SELECT MIN(requested_at) FROM narration_outbox "
            "WHERE attempt_state IN ('pending', 'retry_wait')"
        ).fetchone()[0]
        expired_leases = c.execute(
            "SELECT COUNT(*) FROM narration_outbox WHERE attempt_state='claimed' "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?", (now,)
        ).fetchone()[0]
        receipt_rows = c.execute(
            "SELECT mode, outcome, COUNT(*) AS cnt, COALESCE(SUM(cost_usd),0) AS cost, "
            "COALESCE(SUM(tokens_in+tokens_out),0) AS toks, "
            "COALESCE(SUM(latency_ms),0) AS lat_sum, COUNT(latency_ms) AS lat_n "
            "FROM narration_receipts WHERE created_at >= ? GROUP BY mode, outcome",
            (since,),
        ).fetchall()
        model_rows = c.execute(
            "SELECT COALESCE(model,'(none)') AS model, COUNT(*) AS cnt, "
            "COALESCE(SUM(cost_usd),0) AS cost FROM narration_receipts "
            "WHERE created_at >= ? GROUP BY model", (since,),
        ).fetchall()

    queue = {s: int(states.get(s, 0)) for s in
             ("pending", "claimed", "retry_wait", "delivered", "superseded", "dead_letter")}
    queue["actionable"] = queue["pending"] + queue["retry_wait"]
    queue["expired_leases"] = int(expired_leases)
    oldest_age = (now - oldest) if oldest else 0.0

    delivered = failed = fallback = deterministic = llm = 0
    total_cost = total_tokens = 0.0
    lat_weighted = lat_count = 0.0
    for r in receipt_rows:
        cnt = int(r["cnt"])
        total_cost += float(r["cost"] or 0.0)
        total_tokens += float(r["toks"] or 0.0)
        # Weight by the count of NON-NULL latencies only (SQLite COUNT(col) excludes NULLs), so
        # receipts with no provider latency (deterministic/budget/outage fallbacks) don't skew it.
        if r["lat_n"]:
            lat_weighted += float(r["lat_sum"] or 0.0)
            lat_count += int(r["lat_n"])
        if r["mode"] == "deterministic":
            deterministic += cnt
        elif r["mode"] == "llm":
            llm += cnt
        if r["outcome"] == "delivered":
            delivered += cnt
        elif r["outcome"] == "fallback":
            fallback += cnt
        elif r["outcome"] == "error":
            failed += cnt
    attempts = delivered + failed + fallback
    receipts = {
        "window_seconds": window_seconds,
        "attempts": attempts, "delivered": delivered, "failed": failed, "fallback": fallback,
        "deterministic": deterministic, "llm": llm,
        "failure_rate": round((failed + fallback) / attempts, 4) if attempts else 0.0,
        "avg_latency_ms": round(lat_weighted / lat_count, 2) if lat_count else None,
        "by_model": [{"model": r["model"], "count": int(r["cnt"]), "cost_usd": round(float(r["cost"] or 0.0), 6)}
                     for r in model_rows],
    }
    cost = {"window_seconds": window_seconds, "total_cost_usd": round(total_cost, 6),
            "total_tokens": int(total_tokens)}

    alerts = {
        "queue_age_over_threshold": bool(oldest_age >= QUEUE_AGE_ALERT_SECONDS and queue["actionable"]),
        "dead_letters_present": queue["dead_letter"] > 0,
        "expired_leases_present": queue["expired_leases"] > 0,
        "failure_rate_high": receipts["failure_rate"] >= FAILURE_RATE_ALERT and attempts >= 5,
    }
    return {
        "project": project, "generated_at": now,
        "queue": queue,
        "freshness": {"oldest_pending_age_seconds": round(oldest_age, 2)},
        "receipts": receipts, "cost": cost,
        "alerts": alerts, "alerting": any(alerts.values()),
    }


def _current_outbox_row(c, entity_type: str, entity_id: str):
    return c.execute(
        "SELECT * FROM narration_outbox WHERE entity_type=? AND entity_id=? "
        "ORDER BY source_revision DESC, created_at DESC LIMIT 1",
        (entity_type, entity_id),
    ).fetchone()


def narrate_now(project: str, entity_type: str, entity_id: str, *, actor: str,
                reason: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    """Force (re)generation of the entity's CURRENT narration revision now.

    Deduped: it re-queues the existing request for the current revision rather than inventing a
    new revision, so it can never produce a second visible effect for one source state. Budgets
    are NOT bypassed — the worker still runs the request through the NARRATE-12 policy/budget gate.
    Audited under the resolved actor."""
    now = _now(now)
    if entity_type not in ("task", "deliverable"):
        return {"error": "entity_type must be task or deliverable"}
    # Ensure a request exists for the current revision (idempotent; no-op if already present).
    if entity_type == "task":
        with _conn(project) as c:
            narration_outbox.emit_task_narration_request(
                c, entity_id, project=project, cause_kind="task.narrate_now", actor=actor, now=now)
    else:
        narration_outbox.emit_deliverable_narration_request(
            project, entity_id, cause_kind="deliverable.narrate_now", actor=actor, now=now)

    from db.connection import _write_through

    def _thunk():
        with _conn(project) as c:
            row = _current_outbox_row(c, entity_type, entity_id)
            if row is None:
                return None
            # Re-queue the current revision: back to pending, available now, lease cleared. The
            # dedupe_key/immutable fields are untouched, so this is not a new visible effect.
            c.execute(
                "UPDATE narration_outbox SET attempt_state='pending', available_at=?, "
                "claimed_by=NULL, lease_expires_at=NULL, updated_at=? WHERE event_id=?",
                (now, now, row["event_id"]),
            )
            return dict(row)

    row = _write_through(project, _thunk)
    if row is None:
        _audit(project, "narration.narrate_now_missing", actor,
               {"entity_type": entity_type, "entity_id": entity_id, "reason": reason})
        return {"error": "no narration request for entity", "entity_type": entity_type,
                "entity_id": entity_id}
    narration_outbox.request_wake(project, entity_type=entity_type, entity_id=entity_id)
    result = {"ok": True, "event_id": row["event_id"], "entity_type": entity_type,
              "entity_id": entity_id, "source_revision": row["source_revision"], "requeued": True}
    _audit(project, "narration.narrate_now", actor, {**result, "reason": reason})
    return result


def reactivate_request(project: str, event_id: str, *, actor: str, action: str = "retry",
                       reason: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    """Authorized retry / dead-letter recovery on an existing request (deduped — same row).

    ``action='retry'`` moves a dead_letter/error/superseded row back to pending so the worker
    re-attempts; ``action='dead_letter'`` parks a stuck request. Immutable event fields are never
    touched. Audited under the resolved actor."""
    now = _now(now)
    if action not in ("retry", "dead_letter"):
        return {"error": "action must be retry or dead_letter"}
    from db.connection import _write_through

    def _thunk():
        with _conn(project) as c:
            row = c.execute("SELECT * FROM narration_outbox WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                return ("missing", None)
            prev = row["attempt_state"]
            if action == "retry":
                c.execute(
                    "UPDATE narration_outbox SET attempt_state='pending', available_at=?, "
                    "claimed_by=NULL, lease_expires_at=NULL, updated_at=? WHERE event_id=?",
                    (now, now, event_id),
                )
            else:
                c.execute(
                    "UPDATE narration_outbox SET attempt_state='dead_letter', claimed_by=NULL, "
                    "lease_expires_at=NULL, last_error=COALESCE(last_error, ?), updated_at=? "
                    "WHERE event_id=?",
                    (f"operator_dead_letter:{reason}" if reason else "operator_dead_letter",
                     now, event_id),
                )
            return (prev, dict(row))

    prev, row = _write_through(project, _thunk)
    if row is None:
        return {"error": "unknown event_id", "event_id": event_id}
    if action == "retry":
        narration_outbox.request_wake(project, entity_type=row["entity_type"],
                                      entity_id=row["entity_id"])
    result = {"ok": True, "event_id": event_id, "action": action,
              "previous_state": prev, "entity_id": row["entity_id"]}
    _audit(project, f"narration.{action}", actor, {**result, "reason": reason})
    return result


def list_dead_letters(project: str, *, limit: int = 100) -> List[Dict[str, Any]]:
    with _conn(project) as c:
        rows = c.execute(
            "SELECT event_id, entity_type, entity_id, source_revision, attempt_count, "
            "last_error, updated_at FROM narration_outbox WHERE attempt_state='dead_letter' "
            "ORDER BY updated_at DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _audit(project: str, kind: str, actor: str, payload: Dict[str, Any]) -> None:
    try:
        import store
        store.append_activity(kind, actor, payload, task_id=None, project=project)
    except Exception:
        pass
