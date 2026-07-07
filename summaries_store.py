"""summaries_store.py — task summaries (leaf store). Extracted verbatim from store.py (ARCH-5)."""
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
    "set_task_summary",
    "get_task_summary",
    "get_tasks_needing_summary",
]


def set_task_summary(task_id: str, rationale: str, activity_cursor: int,
                     project: str = DEFAULT_PROJECT) -> None:
    """Upsert the Haiku-generated rationale for a task."""
    with _conn(project) as c:
        c.execute(
            "INSERT OR REPLACE INTO task_summaries(task_id, rationale, generated_at, activity_cursor) "
            "VALUES (?,?,?,?)",
            (task_id, rationale, time.time(), activity_cursor),
        )


def get_task_summary(task_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        r = c.execute("SELECT * FROM task_summaries WHERE task_id=?", (task_id,)).fetchone()
        return dict(r) if r else None


def get_tasks_needing_summary(project: str = DEFAULT_PROJECT,
                              min_interval: int = 900) -> List[str]:
    """Task IDs that have activity AND either no summary yet or new activity since the last
    summary (and enough time has passed to re-run — min_interval seconds)."""
    now = time.time()
    cutoff = now - min_interval
    with _conn(project) as c:
        rows = c.execute(
            """SELECT t.task_id,
                      MAX(a.id) AS max_act,
                      s.activity_cursor,
                      s.generated_at
               FROM tasks t
               JOIN activity a ON a.task_id = t.task_id
               LEFT JOIN task_summaries s ON s.task_id = t.task_id
               GROUP BY t.task_id""",
        ).fetchall()
    result = []
    for row in rows:
        task_id, max_act, cursor, gen_at = row[0], row[1], row[2], row[3]
        no_summary = cursor is None
        new_activity = (not no_summary) and (max_act > cursor)
        interval_ok = gen_at is None or gen_at < cutoff
        if (no_summary or new_activity) and interval_ok:
            result.append(task_id)
    return result
