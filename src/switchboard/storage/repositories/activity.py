"""Activity stream + meta KV repository (ARCH-MS-55).

Owns append_activity / get_activity_delta / activity_since / _activity_cursor,
get_meta / set_meta, and contacts helpers previously living in
``repositories/shell.py``. Cross-cutting store helpers (init_db) are reached via
``_store_facade()`` during the strangler when needed. ``store.py`` / ``shell.py``
re-export these symbols; root ``activity_store.py`` is a compatibility shim.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from constants import *  # noqa: F401,F403
from db.connection import _conn, _control_plane_conn, _control_plane_unavailable
from db.core import *  # noqa: F401,F403
from switchboard.storage.repositories.provenance import _load_git_state  # noqa: F401


def _store_facade():
    """Resolve transitional store helpers after store.py is initialized."""
    import store
    return store


def _activity_cursor(project: str = DEFAULT_PROJECT) -> int:
    with _control_plane_conn(project) as c:
        return int(c.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0] or 0)


def get_activity_delta(since_cursor: int = 0, lane: str = "",
                       project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Return activity newer than since_cursor (activity.id rowid — monotonic, clock-skew-safe).
    lane filters to one workstream (e.g. 'ENGINE'). Returns
    {cursor, updates: [{task_id, status, title, workstream_id, kinds}]}.
    Use this for polling instead of list_tasks/board_summary — empty updates = zero tokens wasted."""
    lane_upper = lane.strip().upper() if lane else ""
    with _conn(project) as c:
        if lane_upper:
            rows = c.execute(
                """SELECT a.id, a.task_id, a.kind, a.actor,
                          t.status, t.title, t.workstream_id
                   FROM activity a
                   JOIN tasks t ON t.task_id = a.task_id
                   WHERE a.id > ? AND t.workstream_id = ?
                   ORDER BY a.id""",
                (since_cursor, lane_upper),
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT a.id, a.task_id, a.kind, a.actor,
                          t.status, t.title, t.workstream_id
                   FROM activity a
                   JOIN tasks t ON t.task_id = a.task_id
                   WHERE a.id > ?
                   ORDER BY a.id""",
                (since_cursor,),
            ).fetchall()
        git_states = {r["task_id"]: _load_git_state(c, r["task_id"]) for r in rows}
    if not rows:
        return {"cursor": since_cursor, "updates": []}
    new_cursor = rows[-1]["id"]
    by_task: Dict[str, Any] = {}
    for row in rows:
        tid = row["task_id"]
        if tid not in by_task:
            by_task[tid] = {"task_id": tid, "status": row["status"],
                            "title": row["title"], "workstream_id": row["workstream_id"],
                            "kinds": [], "git_state": git_states.get(tid, {})}
        by_task[tid]["status"] = row["status"]
        if row["kind"] not in by_task[tid]["kinds"]:
            by_task[tid]["kinds"].append(row["kind"])
    return {"cursor": new_cursor, "updates": list(by_task.values())}


def append_activity(kind: str, actor: str, payload: Optional[Dict[str, Any]] = None,
                    task_id: Optional[str] = None,
                    project: str = DEFAULT_PROJECT) -> int:
    with _conn(project) as c:
        cur = c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (task_id, actor, kind, json.dumps(payload or {}, sort_keys=True), time.time()))
        return cur.lastrowid


def get_meta(key: str, default=None, project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default


def set_meta(key: str, value, project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, json.dumps(value)))


# Seeded with the known TEEP participants so the email agent can resolve "Sahir",
# "Darko", "Steve" -> the right address; auto-learned from every inbound From/To/Cc.
_SEED_CONTACTS = {
    "steve@taikunai.com": "Steve Ridder",
    "sahir.shah@totalenergies.com": "Sahir Shah",
    "darko.jankovic@totalenergies.com": "Darko Jankovic",
}


def get_contacts() -> Dict[str, str]:
    c = get_meta("contacts")
    if not c:
        c = dict(_SEED_CONTACTS)
        set_meta("contacts", c)
    return c


def upsert_contact(email: str, name: Optional[str] = None):
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return
    c = get_contacts()
    name = (name or "").strip()
    if email not in c or (name and not c.get(email)):
        c[email] = name or c.get(email) or email
        set_meta("contacts", c)


def activity_since(ts: float) -> List[Dict[str, Any]]:
    """Every activity event across all tasks since `ts` — the delta substrate."""
    with _conn() as c:
        rows = c.execute(
            "SELECT task_id, actor, kind, payload, created_at FROM activity WHERE created_at > ? ORDER BY created_at",
            (ts,)).fetchall()
    return [{"task_id": r["task_id"], "actor": r["actor"], "kind": r["kind"],
             "payload": json.loads(r["payload"] or "{}"), "created_at": r["created_at"]} for r in rows]


class StoreActivityRepository:
    """Thin repository wrapper over module-level activity/meta helpers."""

    def append_activity(self, *args, **kwargs):
        return append_activity(*args, **kwargs)

    def get_activity_delta(self, *args, **kwargs):
        return get_activity_delta(*args, **kwargs)

    def activity_since(self, *args, **kwargs):
        return activity_since(*args, **kwargs)

    def get_meta(self, *args, **kwargs):
        return get_meta(*args, **kwargs)

    def set_meta(self, *args, **kwargs):
        return set_meta(*args, **kwargs)

    def get_contacts(self, *args, **kwargs):
        return get_contacts(*args, **kwargs)

    def upsert_contact(self, *args, **kwargs):
        return upsert_contact(*args, **kwargs)


def default_activity_repository() -> StoreActivityRepository:
    return StoreActivityRepository()


__all__ = [
    "StoreActivityRepository",
    "default_activity_repository",
    "_SEED_CONTACTS",
    "_activity_cursor",
    "append_activity",
    "get_activity_delta",
    "activity_since",
    "get_meta",
    "set_meta",
    "get_contacts",
    "upsert_contact",
]
