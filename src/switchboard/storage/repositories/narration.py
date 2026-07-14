"""Narration persistence repository (ARCH-MS-56).

Owns task narration fingerprint/state/set/get and pending-narration
enqueue/list/clear helpers previously living in ``repositories/shell.py``.
Prefer this small module over growing ``tasks.py``. ``store.py`` / ``shell.py``
re-export these symbols; root ``narration_store.py`` is a compatibility shim.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Optional

from constants import *  # noqa: F401,F403
from db.connection import _conn
from db.core import *  # noqa: F401,F403


def _max_activity_cursor(task: Dict[str, Any]) -> int:
    return max((a.get("id", 0) for a in (task.get("activity") or [])), default=0)


def task_narration_fingerprint(task: Dict[str, Any]) -> str:
    """Stable stamp of the source state a narration was written from. Recomputed on read;
    a mismatch means the narration is stale (see _narration_state). Shared by the narrator
    (write) and get_task (read) so both sides agree."""
    prov = task.get("provenance") or {}
    parts = [
        str(task.get("status") or ""),
        str(prov.get("type") or ""),
        str(_max_activity_cursor(task)),
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def _narration_state(stored: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
    """Flag a narration stale when current task state has moved past the fingerprint it was
    written from. Discipline carried over from BUG-13/BUG-17/HARDEN-30: derived prose is never
    shown as current truth once it contradicts the fingerprint."""
    current_fp = task_narration_fingerprint(task)
    stored_fp = stored.get("source_fingerprint")
    stale = bool(stored_fp) and stored_fp != current_fp
    state = {
        "stale": stale,
        "source_fingerprint": current_fp,
        "stored_fingerprint": stored_fp,
        "message": (
            "CEO narration is regenerating; trust status, provenance, and progress."
        ) if stale else None,
    }
    if stale:
        state["failure_class"] = "missing_data"
        state["expected_signal"] = "Narration should be regenerated from current task state."
    return state


def set_task_narration(task_id: str, narration: str, activity_cursor: int,
                       source_fingerprint: str = "", model: str = "",
                       project: str = DEFAULT_PROJECT) -> None:
    """Upsert the CEO-voice narration for a task (separate store from task_summaries)."""
    with _conn(project) as c:
        c.execute(
            "INSERT OR REPLACE INTO task_narrations"
            "(task_id, narration, generated_at, activity_cursor, source_fingerprint, model) "
            "VALUES (?,?,?,?,?,?)",
            (task_id, narration, time.time(), activity_cursor, source_fingerprint, model),
        )


def get_task_narration(task_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        r = c.execute("SELECT * FROM task_narrations WHERE task_id=?", (task_id,)).fetchone()
        return dict(r) if r else None


def enqueue_narration(task_id: str, status: str = "", reason: str = "",
                      project: str = DEFAULT_PROJECT) -> None:
    """Mark a task for (re)narration after a meaningful transition. Idempotent per task —
    a burst of transitions collapses into one pending row. Called post-commit from the write
    path; never triggers a synchronous LLM call."""
    with _conn(project) as c:
        c.execute(
            "INSERT OR REPLACE INTO pending_narrations(task_id, status, reason, enqueued_at) "
            "VALUES (?,?,?,?)",
            (task_id, status or "", reason or "", time.time()),
        )


def list_pending_narrations(project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    with _conn(project) as c:
        rows = c.execute(
            "SELECT task_id, status, reason, enqueued_at FROM pending_narrations "
            "ORDER BY enqueued_at"
        ).fetchall()
        return [dict(r) for r in rows]


def clear_pending_narration(task_id: str, project: str = DEFAULT_PROJECT) -> None:
    with _conn(project) as c:
        c.execute("DELETE FROM pending_narrations WHERE task_id=?", (task_id,))


class StoreNarrationRepository:
    """Thin repository wrapper over module-level narration helpers."""

    def task_narration_fingerprint(self, *args, **kwargs):
        return task_narration_fingerprint(*args, **kwargs)

    def set_task_narration(self, *args, **kwargs):
        return set_task_narration(*args, **kwargs)

    def get_task_narration(self, *args, **kwargs):
        return get_task_narration(*args, **kwargs)

    def enqueue_narration(self, *args, **kwargs):
        return enqueue_narration(*args, **kwargs)

    def list_pending_narrations(self, *args, **kwargs):
        return list_pending_narrations(*args, **kwargs)

    def clear_pending_narration(self, *args, **kwargs):
        return clear_pending_narration(*args, **kwargs)


def default_narration_repository() -> StoreNarrationRepository:
    return StoreNarrationRepository()


__all__ = [
    "StoreNarrationRepository",
    "default_narration_repository",
    "_max_activity_cursor",
    "task_narration_fingerprint",
    "_narration_state",
    "set_task_narration",
    "get_task_narration",
    "enqueue_narration",
    "list_pending_narrations",
    "clear_pending_narration",
]
