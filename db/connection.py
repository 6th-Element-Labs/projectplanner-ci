"""connection.py — project resolution + sqlite connection factory (Layer 1). Extracted verbatim from store.py (ARCH-5)."""
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

__all__ = [
    "_dynamic_projects",
    "_project_map",
    "_resolve",
    "_conn",
]


def _dynamic_projects() -> Dict[str, Dict[str, str]]:
    init_project_registry()
    with _registry_conn() as c:
        rows = c.execute("SELECT * FROM projects ORDER BY id").fetchall()
    return {
        r["id"]: {
            "db": r["db_path"],
            "seed": r["seed_path"],
            "label": r["label"],
            "pretitle": r["pretitle"] or "",
        }
        for r in rows
    }


def _project_map() -> Dict[str, Dict[str, str]]:
    return {**_dynamic_projects(), **BUILTIN_PROJECTS}


def _resolve(project: Optional[str]) -> Dict[str, str]:
    """Map a project id -> its config. Fail CLOSED on an unknown id — never silently fall back
    to Maxwell (which could leak a write across projects)."""
    p = _project_map().get(project or DEFAULT_PROJECT)
    if not p:
        raise ValueError(f"unknown project: {project!r}")
    return p


def _conn(project: str = DEFAULT_PROJECT, timeout_s: Optional[float] = None):
    timeout = _sqlite_timeout_s("PM_SQLITE_TIMEOUT_S", 5.0) if timeout_s is None else timeout_s
    c = sqlite3.connect(_resolve(project)["db"], timeout=timeout)
    c.row_factory = sqlite3.Row
    c.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    c.execute("PRAGMA journal_mode=WAL")
    return c
