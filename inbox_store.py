"""inbox_store.py — live inbox / triage (leaf store). Extracted verbatim from store.py (ARCH-5)."""
import json
import time
import os
import sqlite3
import hashlib
import uuid
import copy
from typing import Any, Dict, List, Optional, Tuple

from constants import *  # noqa: F401,F403
from db.core import *     # noqa: F401,F403
from db.schema import *   # noqa: F401,F403
from db.connection import *  # noqa: F401,F403

__all__ = [
    "_inbox_row",
    "inbox_exists",
    "add_inbox_item",
    "list_inbox",
    "get_inbox_item",
    "set_inbox_status",
    "update_inbox_triage",
    "inbox_pending_count",
]


def _inbox_row(r):
    return {"id": r["id"], "source": r["source"], "external_id": r["external_id"],
            "sender": r["sender"], "subject": r["subject"], "summary": r["summary"],
            "triage": json.loads(r["triage"] or "{}"), "status": r["status"],
            "received_at": r["received_at"], "created_at": r["created_at"]}


def inbox_exists(source: str, external_id: str, project: str = DEFAULT_PROJECT) -> bool:
    if not external_id:
        return False
    with _conn(project) as c:
        return bool(c.execute("SELECT 1 FROM inbox WHERE source=? AND external_id=?",
                              (source, external_id)).fetchone())


def add_inbox_item(source, external_id, sender, subject, summary, triage, received_at=None,
                   project: str = DEFAULT_PROJECT) -> int:
    with _conn(project) as c:
        cur = c.execute(
            "INSERT INTO inbox(source,external_id,sender,subject,summary,triage,status,received_at,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (source, external_id, sender, subject, summary, json.dumps(triage or {}), "pending",
             received_at or time.time(), time.time()))
        return cur.lastrowid


def list_inbox(status: Optional[str] = None, limit: int = 50,
               project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    with _conn(project) as c:
        if status:
            rows = c.execute("SELECT * FROM inbox WHERE status=? ORDER BY id DESC LIMIT ?",
                             (status, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM inbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_inbox_row(r) for r in rows]


def get_inbox_item(item_id: int, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        r = c.execute("SELECT * FROM inbox WHERE id=?", (item_id,)).fetchone()
        return _inbox_row(r) if r else None


def set_inbox_status(item_id: int, status: str, project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        c.execute("UPDATE inbox SET status=? WHERE id=?", (status, item_id))


def update_inbox_triage(item_id: int, triage: Dict[str, Any], project: str = DEFAULT_PROJECT):
    """Rewrite an item's stored triage JSON — used after a PARTIAL confirm so the proposals
    that were held back (e.g. status->Done awaiting evidence) stay in the queue."""
    with _conn(project) as c:
        c.execute("UPDATE inbox SET triage=? WHERE id=?", (json.dumps(triage or {}), item_id))


def inbox_pending_count(project: str = DEFAULT_PROJECT) -> int:
    with _conn(project) as c:
        return c.execute("SELECT COUNT(*) FROM inbox WHERE status='pending'").fetchone()[0]
