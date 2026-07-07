"""digests_store.py — activity digests (leaf store). Extracted verbatim from store.py (ARCH-5)."""
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
    "_digest_row",
    "add_digest",
    "last_digest",
    "list_digests",
]


def _digest_row(r):
    return {"id": r["id"], "created_at": r["created_at"], "since_ts": r["since_ts"],
            "content": r["content"], "meta": json.loads(r["meta"] or "{}")}


def add_digest(since_ts: float, content: str, meta: Optional[Dict[str, Any]] = None) -> int:
    with _conn() as c:
        cur = c.execute("INSERT INTO digests(created_at, since_ts, content, meta) VALUES (?,?,?,?)",
                        (time.time(), since_ts, content, json.dumps(meta or {})))
        return cur.lastrowid


def last_digest() -> Optional[Dict[str, Any]]:
    with _conn() as c:
        r = c.execute("SELECT * FROM digests ORDER BY id DESC LIMIT 1").fetchone()
        return _digest_row(r) if r else None


def list_digests(limit: int = 20) -> List[Dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM digests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_digest_row(r) for r in rows]
