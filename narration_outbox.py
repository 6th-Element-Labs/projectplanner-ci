"""Transactional narration outbox — atomic emit + backfill (NARRATE-8, ADR-0008).

This module owns the *producer* half of the event-driven narrator: the canonical
source projection, the monotonic per-entity revision bump, and the atomic insert of a
strict ``switchboard.narration_requested.v1`` envelope (validated by ``narration_events``)
into ``narration_outbox``. All three happen on the *same open connection* as the domain
mutation, so commit means both the mutation and the intent exist and rollback means
neither does — closing the crash gap left by the post-commit ``pending_narrations`` marker.

NARRATE-8 is deliberately producer-only. It does not claim, call a provider, publish, or
retire the legacy queue; NARRATE-9+ own delivery. The legacy marker keeps being written
post-commit during shadowing, so this module never removes it.

Emitters take an already-open ``sqlite3.Connection`` and perform no provider/network work,
honouring the ADR rule that no provider call occurs while holding a SQLite transaction.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Dict, List, Mapping, Optional

import narration_events

# The canonical source snapshot is a *versioned projection*, not raw row JSON: only inputs
# that can materially change the narration. Bump this when the projection shape changes so a
# reprojection legitimately produces a new hash (and thus a new revision) for the same row.
TASK_PROJECTION_VERSION = 1
DELIVERABLE_PROJECTION_VERSION = 1

_UNSAFE = re.compile(r"[^A-Za-z0-9._:/-]+")


def emit_enabled() -> bool:
    """Operator kill switch for the atomic emit (ADR rollback lever).

    Default on: the outbox is additive and nothing consumes it yet, so shadow emit is safe
    and gives real coverage. Set ``PM_NARRATION_OUTBOX=0`` to fall back to legacy-only.
    """
    raw = (os.environ.get("PM_NARRATION_OUTBOX") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _safe_id(value: Any, *, prefix: str, fallback: str) -> str:
    """Coerce an arbitrary actor/label into the contract's safe-id charset.

    Actors can be emails, display names, or the shared env token; the envelope requires
    ``^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$``. We sanitize rather than reject so a producer
    never fails a domain write over a cosmetic id — identity authority is NARRATE-9's job.
    """
    text = _UNSAFE.sub("-", str(value or "").strip()).strip("-")
    if not text:
        text = fallback
    candidate = f"{prefix}{text}"[:128]
    candidate = candidate.strip("-") or f"{prefix}{fallback}"
    return candidate[:128]


def build_task_source_projection(c, task_id: str,
                                 row: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Versioned projection of the task inputs that can materially change its narration.

    Read-only. Deliberately excludes cosmetic fields (sort order, timestamps, assignee
    churn) so a non-material edit produces the same hash and therefore no revision bump.
    """
    if row is None:
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        return None
    row = dict(row)
    try:
        depends_on = json.loads(row.get("depends_on") or "[]")
    except (TypeError, ValueError):
        depends_on = []
    provenance_type = _provenance_type(c, task_id)
    return {
        "v": TASK_PROJECTION_VERSION,
        "entity": "task",
        "title": row.get("title") or "",
        "description": row.get("description") or "",
        "deliverable": row.get("deliverable") or "",
        "exit_criteria": row.get("exit_criteria") or "",
        "status": row.get("status") or "",
        "depends_on": sorted(str(d) for d in depends_on),
        "is_blocking": bool(row.get("is_blocking")),
        "provenance_type": provenance_type,
    }


def build_deliverable_source_projection(row: Mapping[str, Any],
                                        linked: Optional[List[Mapping[str, Any]]] = None
                                        ) -> Dict[str, Any]:
    """Versioned projection for a deliverable header narration.

    Kept dependency-free (operates on a row + optional linked-task snapshot) so a caller
    can build it inside the mutation transaction without recomputing full mission status.
    Deliverable *emit wiring* follows in a later slice; the projection is centralized here
    now so producers and NARRATE-9 share one definition.
    """
    row = dict(row)
    links = []
    for link in linked or []:
        links.append({
            "task_id": str(link.get("task_id") or ""),
            "status": link.get("status") or "",
            "blocks": bool(link.get("blocks_deliverable") or link.get("blocks")),
        })
    return {
        "v": DELIVERABLE_PROJECTION_VERSION,
        "entity": "deliverable",
        "title": row.get("title") or "",
        "status": row.get("status") or "",
        "end_state": row.get("end_state") or "",
        "why_it_matters": row.get("why_it_matters") or "",
        "acceptance_criteria": row.get("acceptance_criteria_json") or "[]",
        "linked_tasks": sorted(links, key=lambda x: x["task_id"]),
    }


def _provenance_type(c, task_id: str) -> Optional[str]:
    """Terminal-provenance signal for the projection, resolved lazily via store.

    Imported inside the call to avoid an import cycle (store imports this module).
    """
    try:
        import store
        return store._provenance_summary(store._load_git_state(c, task_id)).get("type")
    except Exception:
        return None


def _emit(c, *, project: str, entity_type: str, entity_id: str, table: str,
          projection: Mapping[str, Any], cause_kind: str, actor: str,
          priority: str, now: float) -> Optional[Dict[str, Any]]:
    """Shared atomic emitter. Bumps the entity revision and inserts one outbox row on the
    open connection ``c`` iff the projection materially changed. Returns the event or None.
    """
    source_hash = narration_events.canonical_source_hash(projection)
    ent = c.execute(
        f"SELECT narration_source_revision AS rev, narration_source_hash AS hash "
        f"FROM {table} WHERE {'task_id' if table == 'tasks' else 'id'}=?",
        (entity_id,),
    ).fetchone()
    if ent is None:
        return None
    current_rev = ent["rev"] or 0
    if current_rev >= 1 and ent["hash"] == source_hash:
        return None  # cosmetic / no material change — no revision bump, no emit
    new_rev = current_rev + 1
    key_col = "task_id" if table == "tasks" else "id"
    c.execute(
        f"UPDATE {table} SET narration_source_revision=?, narration_source_hash=? "
        f"WHERE {key_col}=?",
        (new_rev, source_hash, entity_id),
    )
    # Deterministic causal id per (entity, revision) makes a retried domain write collapse to
    # the same dedupe_key, so INSERT OR IGNORE emits the revision at most once.
    causal_event_id = f"{entity_type}-{entity_id}-r{new_rev}"[:128]
    principal = _safe_id(actor, prefix="principal-", fallback="system")
    event = narration_events.build_narration_requested(
        event_id="nrq-evt-" + uuid.uuid4().hex,
        project=project,
        entity_type=entity_type,
        entity_id=entity_id,
        source_revision=new_rev,
        source_hash=source_hash,
        causal_event={
            "event_id": _safe_id(causal_event_id, prefix="", fallback=f"{entity_id}-r{new_rev}"),
            "kind": cause_kind,
            "occurred_at": now,
            "actor_id": principal,
        },
        requested_at=now,
        authorization={
            "principal_id": principal,
            "decision_id": _safe_id("authz-" + uuid.uuid4().hex, prefix="", fallback="authz"),
            "scope": narration_events.NARRATION_REQUEST_SCOPE,
            "project": project,
        },
        trace_id=_safe_id("trace-" + uuid.uuid4().hex, prefix="", fallback="trace"),
        priority=priority,
    )
    _insert_outbox_row(c, event, now)
    return event


def emit_task_narration_request(c, task_id: str, *, project: str,
                                cause_kind: str = "task.updated", actor: str = "user",
                                priority: str = "normal", now: Optional[float] = None,
                                row: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Emit a narration request for a task on the caller's open transaction.

    No-op (returns None) when the emit kill switch is off, the task is gone, or the
    projection is unchanged since the last emitted revision.
    """
    if not emit_enabled():
        return None
    now = _now(now)
    projection = build_task_source_projection(c, task_id, row=row)
    if projection is None:
        return None
    return _emit(c, project=project, entity_type="task", entity_id=task_id, table="tasks",
                 projection=projection, cause_kind=cause_kind, actor=actor,
                 priority=priority, now=now)


def _insert_outbox_row(c, event: Mapping[str, Any], now: float) -> None:
    attempt = event["attempt"]
    c.execute(
        """INSERT OR IGNORE INTO narration_outbox
            (event_id, schema_version, event_type, project, entity_type, entity_id,
             source_revision, source_hash, causal_event, priority, requested_at,
             dedupe_key, supersedes, attempt_state, attempt_count, available_at,
             claimed_by, lease_expires_at, last_error, authorization, trace_id,
             created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event["event_id"], event["schema"], event["event_type"], event["project"],
            event["entity_type"], event["entity_id"], event["source_revision"],
            event["source_hash"], json.dumps(event["causal_event"], sort_keys=True),
            event["priority"], event["requested_at"], event["dedupe_key"],
            json.dumps(event["supersedes"], sort_keys=True) if event.get("supersedes") else None,
            attempt["state"], attempt["count"], attempt["available_at"],
            attempt.get("claimed_by"), attempt.get("lease_expires_at"),
            attempt.get("last_error"),
            json.dumps(event["authorization"], sort_keys=True), event["trace_id"],
            now, now,
        ),
    )


def _now(now: Optional[float]) -> float:
    if now is not None:
        return now
    import time
    return time.time()


# ---------------------------------------------------------------------------
# Backfill + read accessors (project-aware; open their own connection).
# ---------------------------------------------------------------------------

def _conn(project: str):
    from db.connection import _conn as conn
    return conn(project)


def backfill_narration_source_revisions(project: str, *, batch: int = 500) -> Dict[str, int]:
    """Set revision 1 + current source hash for tasks never projected, without emitting.

    Idempotent: only touches ``narration_source_revision = 0`` rows, so re-running is a
    no-op. This establishes a baseline so the first real mutation bumps to revision 2 and
    the outbox never replays historical provider work for already-current entities.
    """
    updated = 0
    with _conn(project) as c:
        while True:
            rows = c.execute(
                "SELECT task_id FROM tasks WHERE COALESCE(narration_source_revision, 0) = 0 "
                "LIMIT ?",
                (batch,),
            ).fetchall()
            if not rows:
                break
            for r in rows:
                projection = build_task_source_projection(c, r["task_id"])
                if projection is None:
                    continue
                source_hash = narration_events.canonical_source_hash(projection)
                c.execute(
                    "UPDATE tasks SET narration_source_revision=1, narration_source_hash=? "
                    "WHERE task_id=? AND COALESCE(narration_source_revision, 0) = 0",
                    (source_hash, r["task_id"]),
                )
                updated += 1
            if len(rows) < batch:
                break
    return {"tasks": updated}


def _row_to_event(r: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "event_id": r["event_id"],
        "schema": r["schema_version"],
        "event_type": r["event_type"],
        "project": r["project"],
        "entity_type": r["entity_type"],
        "entity_id": r["entity_id"],
        "source_revision": r["source_revision"],
        "source_hash": r["source_hash"],
        "causal_event": json.loads(r["causal_event"]),
        "priority": r["priority"],
        "requested_at": r["requested_at"],
        "dedupe_key": r["dedupe_key"],
        "supersedes": json.loads(r["supersedes"]) if r["supersedes"] else None,
        "attempt": {
            "state": r["attempt_state"],
            "count": r["attempt_count"],
            "available_at": r["available_at"],
            "claimed_by": r["claimed_by"],
            "lease_expires_at": r["lease_expires_at"],
            "last_error": r["last_error"],
        },
        "authorization": json.loads(r["authorization"]),
        "trace_id": r["trace_id"],
    }


def list_narration_outbox(project: str, *, states: Optional[List[str]] = None,
                          limit: int = 200) -> List[Dict[str, Any]]:
    """Read outbox rows in per-entity revision order (recovery/observability helper)."""
    sql = ("SELECT * FROM narration_outbox")
    params: List[Any] = []
    if states:
        placeholders = ",".join("?" * len(states))
        sql += f" WHERE attempt_state IN ({placeholders})"
        params.extend(states)
    sql += " ORDER BY entity_type, entity_id, source_revision LIMIT ?"
    params.append(limit)
    with _conn(project) as c:
        return [_row_to_event(r) for r in c.execute(sql, params).fetchall()]


def get_narration_outbox_event(project: str, event_id: str) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        r = c.execute("SELECT * FROM narration_outbox WHERE event_id=?", (event_id,)).fetchone()
        return _row_to_event(r) if r else None
