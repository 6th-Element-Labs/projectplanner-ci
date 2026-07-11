"""connection.py — project resolution + sqlite connection factory (Layer 1). Extracted verbatim from store.py (ARCH-5)."""
import json
import time
import os
import sqlite3
import hashlib
import uuid
import copy
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

from constants import *  # noqa: F401,F403
from db.core import *     # noqa: F401,F403
from db.schema import *   # noqa: F401,F403
from db.write_queue import (
    all_queue_stats,
    in_write_worker,
    single_writer_enabled,
    sql_mutates,
    write_through,
)

__all__ = [
    "_dynamic_projects",
    "_project_map",
    "_resolve",
    "_conn",
    "_write_through",
    "_sqlite_write_queue_stats",
]


# Connection-local defaults for the WAL-backed board databases.  A negative
# cache_size is measured in KiB (rather than pages), so it stays stable if the
# SQLite page size changes.  The larger autocheckpoint threshold amortizes EBS
# checkpoint I/O while keeping the WAL bounded to roughly 16 MiB at 4 KiB/page.
_SQLITE_CACHE_KIB = 32 * 1024
_SQLITE_MMAP_BYTES = 256 * 1024 * 1024
_SQLITE_WAL_AUTOCHECKPOINT_PAGES = 4_000


def _sqlite_mmap_bytes() -> int:
    raw = (os.environ.get("PM_SQLITE_MMAP_BYTES") or "").strip()
    if not raw:
        return _SQLITE_MMAP_BYTES
    value = int(raw)
    if value < 0:
        raise ValueError("PM_SQLITE_MMAP_BYTES must be >= 0")
    return value


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


def _open_sqlite(db_path: str, timeout_s: float) -> sqlite3.Connection:
    c = sqlite3.connect(db_path, timeout=timeout_s)
    c.row_factory = sqlite3.Row
    try:
        c.execute(f"PRAGMA busy_timeout={int(timeout_s * 1000)}")
        c.execute("PRAGMA journal_mode=WAL")
        # NORMAL avoids a second fsync for each WAL transaction.  SQLite still
        # syncs the WAL at checkpoints, preserving database consistency while
        # substantially shortening the writer lock window.  A power or OS crash
        # can roll back a recently committed transaction, which is the documented
        # WAL+NORMAL durability tradeoff accepted for this control-plane store.
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute(f"PRAGMA cache_size={-_SQLITE_CACHE_KIB}")
        c.execute(f"PRAGMA mmap_size={_sqlite_mmap_bytes()}")
        c.execute(f"PRAGMA wal_autocheckpoint={_SQLITE_WAL_AUTOCHECKPOINT_PAGES}")
        return c
    except Exception:
        c.close()
        raise


class _SerializedWriteProxy:
    """Routes mutating SQL on non-writer threads through the per-db write queue."""

    def __init__(self, db_path: str, timeout_s: float, conn: sqlite3.Connection):
        self._db_path = db_path
        self._timeout_s = timeout_s
        self._conn = conn

    def _run_write(self, fn):
        def thunk():
            with _open_sqlite(self._db_path, self._timeout_s) as writer:
                return fn(writer)
        return write_through(self._db_path, thunk)

    def execute(self, sql, parameters=()):
        if single_writer_enabled() and sql_mutates(sql) and not in_write_worker():
            return self._run_write(lambda writer: writer.execute(sql, parameters))
        return self._conn.execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        if single_writer_enabled() and sql_mutates(sql) and not in_write_worker():
            return self._run_write(lambda writer: writer.executemany(sql, seq_of_parameters))
        return self._conn.executemany(sql, seq_of_parameters)

    def executescript(self, sql_script):
        if single_writer_enabled() and not in_write_worker():
            return self._run_write(lambda writer: writer.executescript(sql_script))
        return self._conn.executescript(sql_script)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._conn.__exit__(exc_type, exc, tb)


@contextmanager
def _conn(project: str = DEFAULT_PROJECT, timeout_s: Optional[float] = None):
    timeout = _sqlite_timeout_s("PM_SQLITE_TIMEOUT_S", 5.0) if timeout_s is None else timeout_s
    db_path = _resolve(project)["db"]
    c = _open_sqlite(db_path, timeout)
    try:
        with c:
            if single_writer_enabled() and not in_write_worker():
                yield _SerializedWriteProxy(db_path, timeout, c)
            else:
                yield c
    finally:
        if c:
            c.close()


def _write_through(project: str, thunk, timeout_s: Optional[float] = None):
    """Serialize a multi-statement write transaction on the project's writer thread."""
    return write_through(_resolve(project)["db"], thunk, timeout_s=timeout_s)


def _sqlite_write_queue_stats() -> Dict[str, Any]:
    return all_queue_stats()
