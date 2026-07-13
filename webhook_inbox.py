"""Durable webhook inbox — accept-and-ack, never drop (PERF-1).

The GitHub webhook handler used to apply provenance *synchronously on the request
path*: ``github_webhook -> handle_pr`` fanned out into several SQLite writes (task
status, canonical main SHA, leaseholder messages, branch retire). Under a lock
storm those writes exhausted the retry budget and GitHub got 500/504 — the delivery
was then DROPPED, stranding board provenance (this is what stranded BUG-32 #242 and
UI-7 #243).

This module makes that failure class *structurally impossible*. The handler now does
exactly one thing on the request path: append the raw event (headers + payload +
delivery guid) to the append-only ``webhook_inbox`` table and return 2xx in O(1). No
fan-out, no cross-row locks, so it cannot lock-timeout. A separate drain worker
(:func:`drain`) applies the provenance idempotently *off* the request path, deduped
on the GitHub delivery guid, with bounded retries. Event-sourcing / write-ahead
intent.

Idempotency is enforced at two layers:
  * enqueue: ``INSERT OR IGNORE`` on a UNIQUE delivery guid — GitHub redelivery of
    the same guid can never double-enqueue.
  * drain:  once a row is ``applied`` it is never re-selected; and the underlying
    ``github_sync.handle_push``/``handle_pr`` are themselves idempotent, so even a
    forced replay converges.

Kept out of store.py deliberately (ADR-0006 / ARCH-20 size ratchet): this is a leaf
module that composes existing store primitives.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, Mapping, Optional, Set, Union

import github_sync
import store

SCHEMA = "switchboard.webhook_inbox_depth.v1"

# Terminal drain states: a row that reached one of these is never re-applied.
_TERMINAL = ("applied", "dead", "ignored")

# Bounded retry budget before a row is parked as ``dead`` (still durable + visible,
# never silently dropped). Small-box friendly: a poison event can't spin forever.
DEFAULT_MAX_ATTEMPTS = 8

# Canonical DDL. Mirrored in db/schema.apply_schema so a fresh DB has the table
# before any webhook arrives; re-run cheaply here to self-heal pre-existing DBs.
_DDL = """
CREATE TABLE IF NOT EXISTS webhook_inbox (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_guid      TEXT NOT NULL,
    event              TEXT NOT NULL,
    project            TEXT NOT NULL,
    requested_project  TEXT,
    signature_verified INTEGER NOT NULL DEFAULT 0,
    headers_json       TEXT NOT NULL DEFAULT '{}',
    payload            TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending',
    attempts           INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT,
    result_json        TEXT NOT NULL DEFAULT '{}',
    received_at        REAL NOT NULL,
    updated_at         REAL NOT NULL,
    applied_at         REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_webhook_inbox_guid ON webhook_inbox(delivery_guid);
CREATE INDEX IF NOT EXISTS ix_webhook_inbox_status ON webhook_inbox(status, id);
"""

_ENSURED: Set[str] = set()


def _ensure(project: str) -> None:
    """Idempotent, amortized-once-per-process schema guard for the inbox table."""
    if project in _ENSURED:
        return
    if store.project_lifecycle_status(project) == "archived":
        # Archive happens only after a complete impact snapshot, so the board schema
        # already exists. Historical inbox reads must not attempt DDL on a locked board.
        _ENSURED.add(project)
        return
    with store._conn(project) as c:
        c.executescript(_DDL)
    _ENSURED.add(project)


def _delivery_guid(delivery_guid: str, event: str, payload_text: str) -> str:
    """GitHub's X-GitHub-Delivery is the dedupe key. Synthesize a stable one from the
    body when a sender omits it (synthetic bursts, replays) so dedupe still holds."""
    guid = (delivery_guid or "").strip()
    if guid:
        return guid
    digest = hashlib.sha256(f"{event}:{payload_text}".encode("utf-8")).hexdigest()
    return f"synthetic-{digest}"


def enqueue_event(
    project: str,
    *,
    delivery_guid: str,
    event: str,
    payload_bytes: Union[bytes, str],
    headers: Optional[Mapping[str, str]] = None,
    signature_verified: bool = True,
    requested_project: str = "",
) -> Dict[str, Any]:
    """Durably append one raw webhook delivery. O(1), single INSERT, no fan-out.

    This is the *commit point* for never-drop: once this returns ``enqueued`` (or
    ``duplicate``), the delivery survives process death and will be applied by the
    drain worker. Returns fast enough to ack GitHub well inside its timeout budget.
    """
    _ensure(project)
    if isinstance(payload_bytes, bytes):
        payload_text = payload_bytes.decode("utf-8", "replace")
    else:
        payload_text = payload_bytes
    guid = _delivery_guid(delivery_guid, event, payload_text)
    now = time.time()
    headers_json = json.dumps(dict(headers or {}), separators=(",", ":"))
    with store._conn(project) as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO webhook_inbox
               (delivery_guid, event, project, requested_project, signature_verified,
                headers_json, payload, status, attempts, result_json,
                received_at, updated_at)
               VALUES (?,?,?,?,?,?,?, 'pending', 0, '{}', ?, ?)""",
            (guid, event or "", project, requested_project or "",
             1 if signature_verified else 0, headers_json, payload_text, now, now),
        )
        if cur.rowcount:
            return {"enqueued": True, "duplicate": False, "id": cur.lastrowid,
                    "delivery_guid": guid, "status": "pending"}
        row = c.execute(
            "SELECT id, status FROM webhook_inbox WHERE delivery_guid=?", (guid,)
        ).fetchone()
    return {"enqueued": False, "duplicate": True,
            "id": row["id"] if row else None, "delivery_guid": guid,
            "status": row["status"] if row else "pending"}


def _apply_row(row: Mapping[str, Any], project: str) -> Dict[str, Any]:
    """Apply one inbox row's provenance via the existing idempotent sync handlers."""
    event = row["event"]
    payload = json.loads(row["payload"])
    if event == "push":
        return github_sync.handle_push(payload, project)
    if event == "pull_request":
        return github_sync.handle_pr(payload, project)
    return {"action": "ignored", "event": event}


def drain(project: str, *, limit: int = 200,
          max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> Dict[str, Any]:
    """Apply pending inbox rows idempotently, OFF the request path.

    Each row is applied in its own short transaction — the sync handlers open their
    own store connections, so the inbox connection is never held open across a
    fan-out write (which is exactly the lock-storm the accept-and-ack split removes).
    A transient failure leaves the row ``pending`` (retried next drain) until the
    attempt budget is spent, then parks it ``dead`` — durable and visible, never lost.
    """
    _ensure(project)
    with store._conn(project) as c:
        rows = c.execute(
            "SELECT * FROM webhook_inbox WHERE status='pending' ORDER BY id LIMIT ?",
            (int(limit),),
        ).fetchall()

    applied = failed = dead = ignored = 0
    for row in rows:
        now = time.time()
        try:
            result = _apply_row(row, project)
            status = "ignored" if result.get("action") == "ignored" else "applied"
            with store._conn(project) as c:
                c.execute(
                    "UPDATE webhook_inbox SET status=?, result_json=?, last_error=NULL, "
                    "applied_at=?, updated_at=? WHERE id=?",
                    (status, json.dumps(result, separators=(",", ":"),
                     default=str), now, now, row["id"]),
                )
            if status == "applied":
                applied += 1
            else:
                ignored += 1
        except Exception as exc:  # noqa: BLE001 — durability over correctness of one row
            attempts = int(row["attempts"] or 0) + 1
            status = "dead" if attempts >= max_attempts else "pending"
            with store._conn(project) as c:
                c.execute(
                    "UPDATE webhook_inbox SET status=?, attempts=?, last_error=?, "
                    "updated_at=? WHERE id=?",
                    (status, attempts, str(exc), now, row["id"]),
                )
            if status == "dead":
                dead += 1
            else:
                failed += 1

    depth = inbox_depth(project)
    return {"schema": "switchboard.webhook_inbox_drain.v1", "project": project,
            "scanned": len(rows), "applied": applied, "ignored": ignored,
            "retry_pending": failed, "dead": dead,
            "pending_after": depth["pending"], "depth": depth}


def inbox_depth(project: str) -> Dict[str, Any]:
    """Observable inbox state: counts by status + age of the oldest un-applied event.

    ``pending``/``dead`` > 0 or a growing ``oldest_pending_age_s`` is the signal that
    the drain worker is behind or wedged — the thing that used to be an invisible
    dropped delivery is now a visible, alertable queue depth.
    """
    _ensure(project)
    counts: Dict[str, int] = {}
    with store._conn(project) as c:
        for r in c.execute(
            "SELECT status, COUNT(*) AS n FROM webhook_inbox GROUP BY status"
        ).fetchall():
            counts[r["status"]] = int(r["n"])
        oldest = c.execute(
            "SELECT MIN(received_at) AS t FROM webhook_inbox WHERE status='pending'"
        ).fetchone()
    oldest_at = float(oldest["t"]) if oldest and oldest["t"] is not None else None
    total = sum(counts.values())
    return {
        "schema": SCHEMA,
        "project": project,
        "total": total,
        "pending": counts.get("pending", 0),
        "applied": counts.get("applied", 0),
        "ignored": counts.get("ignored", 0),
        "dead": counts.get("dead", 0),
        "by_status": counts,
        "oldest_pending_at": oldest_at,
        "oldest_pending_age_s": (time.time() - oldest_at) if oldest_at else 0.0,
    }
