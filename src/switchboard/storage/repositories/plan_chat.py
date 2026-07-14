"""Plan-wide chat persistence repository (ARCH-MS-57).

Owns the global Ask Taikun ``chat`` table helpers previously living in
``repositories/shell.py``. The plan_chat REST router imports this module
directly; ``store.py`` / ``shell.py`` re-export for strangler compatibility.
Root ``plan_chat_store.py`` is a shim.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from constants import *  # noqa: F401,F403
from db.connection import _conn


def add_chat(session: str, role: str, content: str, payload: Optional[Dict[str, Any]] = None,
             project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        c.execute("INSERT INTO chat(session, role, content, payload, created_at) VALUES (?,?,?,?,?)",
                  (session, role, content, json.dumps(payload or {}), time.time()))


def clear_chat(session: str, project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        c.execute("DELETE FROM chat WHERE session=?", (session,))


def recent_chat(session: str, limit: int = 20, project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    with _conn(project) as c:
        rows = c.execute(
            "SELECT role, content, payload, created_at FROM chat WHERE session=? ORDER BY id DESC LIMIT ?",
            (session, limit)).fetchall()
    out = [{"role": r["role"], "content": r["content"],
            "payload": json.loads(r["payload"] or "{}"), "created_at": r["created_at"]} for r in rows]
    out.reverse()
    return out


class StorePlanChatRepository:
    """Thin repository wrapper over module-level plan-chat helpers."""

    def add_chat(self, *args, **kwargs):
        return add_chat(*args, **kwargs)

    def clear_chat(self, *args, **kwargs):
        return clear_chat(*args, **kwargs)

    def recent_chat(self, *args, **kwargs):
        return recent_chat(*args, **kwargs)


def default_plan_chat_repository() -> StorePlanChatRepository:
    return StorePlanChatRepository()


__all__ = [
    "StorePlanChatRepository",
    "default_plan_chat_repository",
    "add_chat",
    "clear_chat",
    "recent_chat",
]
