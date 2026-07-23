"""One read model for the question: is anyone executing this task?

The underlying records keep their own lifecycle contracts.  Callers must not
reimplement subsets of this union when deciding whether work may be started.
"""
from __future__ import annotations

import json
import time
from typing import Any

from constants import DEFAULT_PROJECT
from db.connection import _conn


LIVE_RUNNER_STATUSES = frozenset({"starting", "ready", "running"})
LIVE_WORK_SESSION_STATUSES = frozenset({"proposed", "active", "blocked"})


def _execution_presence_in(c: Any, task_id: str, now: float) -> dict[str, Any]:
    task_id = str(task_id or "").strip()
    claims = [dict(row) for row in c.execute(
        "SELECT * FROM task_claims WHERE task_id=? AND status='active' AND expires_at>?",
        (task_id, now),
    ).fetchall()]
    runners = []
    for row in c.execute(
            "SELECT * FROM runner_sessions WHERE task_id=?", (task_id,)).fetchall():
        item = dict(row)
        expires_at = float(item.get("heartbeat_at") or 0) + int(
            item.get("heartbeat_ttl_s") or 60)
        if str(item.get("status") or "").lower() not in LIVE_RUNNER_STATUSES:
            continue
        if expires_at <= now:
            continue
        item["control"] = json.loads(item.pop("control_json", "{}") or "{}")
        item["metadata"] = json.loads(item.pop("metadata_json", "{}") or "{}")
        item["expires_at"] = expires_at
        runners.append(item)
    work_sessions = [dict(row) for row in c.execute(
        "SELECT * FROM work_sessions WHERE task_id=? AND status IN ('proposed','active','blocked') "
        "AND (expires_at IS NULL OR expires_at>?)",
        (task_id, now),
    ).fetchall()]
    agents = [dict(row) for row in c.execute(
        "SELECT * FROM agent_presence WHERE task_id=? AND heartbeat_at+ttl_s>?",
        (task_id, now),
    ).fetchall()]
    sources = []
    if runners:
        sources.append("runner_sessions")
    if claims:
        sources.append("active_claims")
    if work_sessions:
        sources.append("work_sessions")
    if agents:
        sources.append("live_agents")
    return {
        "schema": "switchboard.execution_presence.v1",
        "task_id": task_id,
        "leased": bool(sources),
        "sources": sources,
        "runner_sessions": runners,
        "active_claims": claims,
        "work_sessions": work_sessions,
        "live_agents": agents,
        "checked_at": now,
    }


def get_execution_presence(task_id: str, *, project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        return _execution_presence_in(c, task_id, now)


def active_task_ids_in(c: Any, now: float) -> set[str]:
    """Scheduler projection using the exact same union as point reads."""
    ids: set[str] = set()
    for table in ("task_claims", "runner_sessions", "work_sessions", "agent_presence"):
        rows = c.execute(f"SELECT DISTINCT task_id FROM {table} WHERE task_id IS NOT NULL").fetchall()
        for row in rows:
            task_id = str(row["task_id"] or "")
            if task_id and _execution_presence_in(c, task_id, now)["leased"]:
                ids.add(task_id)
    return ids
