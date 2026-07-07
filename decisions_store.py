"""decisions_store.py — decision log (leaf store). Extracted verbatim from store.py (ARCH-5)."""
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
    "record_decision",
    "list_decisions",
    "get_decision",
]


def record_decision(task_id: Optional[str], author: str, title: str,
                    context: str, decision: str, rationale: str,
                    supersedes: Optional[int] = None,
                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Append an architectural decision record (ADR-lite) to the decisions log.
    Immutable once written — to reverse, record a new decision with status='superseded'
    and reference the old id in supersedes. Returns the full record."""
    now = time.time()
    with _conn(project) as c:
        cur = c.execute(
            "INSERT INTO decisions(task_id, author, title, context, decision, rationale, "
            "supersedes, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (task_id, author, title, context, decision, rationale, supersedes, now),
        )
        dec_id = cur.lastrowid
        if supersedes:
            c.execute("UPDATE decisions SET status='superseded' WHERE id=?", (supersedes,))
    return {"id": dec_id, "task_id": task_id, "author": author, "title": title,
            "context": context, "decision": decision, "rationale": rationale,
            "status": "accepted", "supersedes": supersedes, "created_at": now}


def list_decisions(task_id: Optional[str] = None, status: str = "",
                   project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """List decisions, optionally filtered by task_id and/or status ('accepted',
    'superseded', 'proposed'). Returns newest-first."""
    q = "SELECT * FROM decisions WHERE 1=1"
    p: List[Any] = []
    if task_id:
        q += " AND task_id=?"; p.append(task_id)
    if status:
        q += " AND status=?"; p.append(status)
    q += " ORDER BY id DESC"
    with _conn(project) as c:
        rows = c.execute(q, p).fetchall()
    return [dict(r) for r in rows]


def get_decision(decision_id: int, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        r = c.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    return dict(r) if r else None
