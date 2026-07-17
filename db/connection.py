"""connection.py — project resolution + sqlite connection factory (Layer 1). Extracted verbatim from store.py (ARCH-5)."""
import json
import time
import os
import sqlite3
import hashlib
import threading
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
from switchboard.domain.projects.lifecycle import (
    ProjectLifecycleWriteBlocked,
    assert_project_write_allowed,
)

__all__ = [
    "_dynamic_projects",
    "_project_map",
    "_resolve",
    "_conn",
    "_control_plane_timeout_s",
    "_control_plane_conn",
    "_control_plane_unavailable",
    "_write_through",
    "_sqlite_write_queue_stats",
    "bust_project_cache",
    "project_lifecycle_status",
    "ProjectLifecycleWriteBlocked",
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


# The project map changes only when a project is created, but _resolve() consults it on
# EVERY store operation (per project). Reading it uncached meant re-running the registry
# schema-init (init_project_registry) AND opening a fresh registry connection on every
# resolve — which, on the live box, pegged the web worker's core in _open_sqlite /
# _registry_conn / init_project_registry and made unrelated requests queue (7s p99).
# Cache the built map behind a short TTL; create_project busts it in-process for
# read-your-write, and the TTL bounds cross-process staleness (a project created by the
# MCP process appears to the web process within the TTL).
_PROJECT_MAP_TTL_S = float(os.environ.get("PM_PROJECT_MAP_TTL_S", "10") or 10)
_project_map_cache: Dict[str, Any] = {"at": 0.0, "data": None, "signature": None}
_project_map_lock = threading.Lock()


def bust_project_cache() -> None:
    """Invalidate the cached project map (call after writing the project registry)."""
    _project_map_cache["data"] = None
    _project_map_cache["signature"] = None


def _registry_cache_signature() -> tuple:
    """Cheap cross-process invalidation signal, including SQLite's WAL sidecar."""
    values = []
    for path in (PROJECT_REGISTRY_DB_PATH, PROJECT_REGISTRY_DB_PATH + "-wal"):
        try:
            stat = os.stat(path)
            values.append((path, stat.st_mtime_ns, stat.st_size))
        except OSError:
            values.append((path, 0, 0))
    return tuple(values)


def _load_dynamic_projects() -> Dict[str, Dict[str, str]]:
    """Load the single registry projection used for all project routing.

    The historical function name is retained for adapter/test compatibility.
    ACCESS-22 backfills configured system projects into this same table, so this
    now returns both protected system records and operator-created projects.
    """
    init_project_registry()
    with _registry_conn() as c:
        rows = c.execute("SELECT * FROM projects ORDER BY id").fetchall()
    return {
        r["id"]: {
            "db": r["db_path"],
            "seed": r["seed_path"],
            "label": r["label"],
            "pretitle": r["pretitle"] or "",
            "lifecycle_status": (r["lifecycle_status"] or "active").strip().lower(),
        }
        for r in rows
    }


def _dynamic_projects() -> Dict[str, Dict[str, str]]:
    signature = _registry_cache_signature()
    data = _project_map_cache["data"]
    if (data is not None and _project_map_cache.get("signature") == signature and
            (time.monotonic() - _project_map_cache["at"]) < _PROJECT_MAP_TTL_S):
        return data
    with _project_map_lock:
        signature = _registry_cache_signature()
        data = _project_map_cache["data"]
        if (data is not None and _project_map_cache.get("signature") == signature and
                (time.monotonic() - _project_map_cache["at"]) < _PROJECT_MAP_TTL_S):
            return data
        data = _load_dynamic_projects()
        _project_map_cache["data"] = data
        _project_map_cache["at"] = time.monotonic()
        _project_map_cache["signature"] = _registry_cache_signature()
        return data


def _project_map() -> Dict[str, Dict[str, str]]:
    return dict(_dynamic_projects())


def _resolve(project: Optional[str]) -> Dict[str, str]:
    """Map a project id -> its config. Fail CLOSED on an unknown id — never silently fall back
    to Maxwell (which could leak a write across projects)."""
    p = _project_map().get(project or DEFAULT_PROJECT)
    if not p:
        raise ValueError(f"unknown project: {project!r}")
    return p


def project_lifecycle_status(project: str) -> str:
    """Current lifecycle state used by the central read/write connection boundary."""
    return str(_resolve(project).get("lifecycle_status") or "active").strip().lower()


# journal_mode=WAL is PERSISTENT — it lives in the database header, so once a db is WAL
# every new connection opens in WAL automatically without re-running the PRAGMA. Re-issuing
# `PRAGMA journal_mode=WAL` on every connection is therefore redundant, and it was the single
# hottest line on the live web worker (~45% of its CPU — the statement briefly locks the db /
# touches the WAL). Set it once per db-path per process; skip it on every subsequent open.
# (The other PRAGMAs below are per-connection state and MUST be re-applied on each open.)
_wal_confirmed_paths: set = set()


def _open_sqlite(db_path: str, timeout_s: float) -> sqlite3.Connection:
    c = sqlite3.connect(db_path, timeout=timeout_s)
    c.row_factory = sqlite3.Row
    try:
        c.execute(f"PRAGMA busy_timeout={int(timeout_s * 1000)}")
        if db_path not in _wal_confirmed_paths:
            c.execute("PRAGMA journal_mode=WAL")
            _wal_confirmed_paths.add(db_path)
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


class _LifecycleGuardedCursor:
    """Keep cursor-based and chained execution inside the lifecycle boundary."""

    def __init__(self, connection, cursor):
        self._connection = connection
        self._cursor = cursor

    @property
    def connection(self):
        return self._connection

    def execute(self, sql, parameters=()):
        self._connection._guard_sql(sql)
        self._cursor.execute(sql, parameters)
        return self

    def executemany(self, sql, seq_of_parameters):
        self._connection._guard_sql(sql)
        self._cursor.executemany(sql, seq_of_parameters)
        return self

    def executescript(self, sql_script):
        self._connection._guard_script(sql_script)
        self._cursor.executescript(sql_script)
        return self

    def __iter__(self):
        return iter(self._cursor)

    def __next__(self):
        return next(self._cursor)

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)


class _LifecycleGuardedConnection:
    """Allow reads on archived boards while rejecting every mutating SQL path."""

    def __init__(self, project: str, conn):
        self._project = project
        self._conn = conn

    def _guard(self, operation: str) -> None:
        # Re-read through the cross-process-coherent project map at execution time.
        # A connection opened while active must not stay writable after another
        # process archives the project.
        assert_project_write_allowed(
            self._project, project_lifecycle_status(self._project), operation)

    @staticmethod
    def _mutates(sql: str) -> bool:
        statement = re.sub(
            r"\A(?:\s|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*", "", str(sql or ""),
            flags=re.DOTALL,
        )
        if sql_mutates(statement):
            return True
        upper = statement.upper()
        if upper.startswith("WITH"):
            return bool(re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", upper))
        if upper.startswith("ANALYZE"):
            return True
        if upper.startswith("PRAGMA"):
            # Fail closed: bare pragmas such as optimize, wal_checkpoint, and
            # incremental_vacuum mutate state too. Only known query pragmas remain
            # available to historical diagnostics.
            if "=" in statement:
                return True
            match = re.match(r"^PRAGMA\s+(?:[\w\"]+\.)?([\w]+)", upper)
            pragma_name = match.group(1) if match else ""
            read_only = {
                "APPLICATION_ID", "COLLATION_LIST", "COMPILE_OPTIONS", "DATABASE_LIST",
                "DATA_VERSION", "ENCODING", "FOREIGN_KEYS", "FOREIGN_KEY_CHECK",
                "TABLE_INFO", "TABLE_XINFO", "INDEX_INFO", "INDEX_XINFO", "INDEX_LIST",
                "FOREIGN_KEY_LIST", "INTEGRITY_CHECK", "JOURNAL_MODE", "PAGE_COUNT",
                "FREELIST_COUNT", "PRAGMA_LIST", "QUICK_CHECK", "SCHEMA_VERSION",
                "TABLE_LIST", "THREADSAFE", "USER_VERSION",
            }
            return pragma_name not in read_only
        return False

    def _guard_sql(self, sql) -> None:
        if self._mutates(sql):
            operation = str(sql or "").lstrip().split(None, 1)[0].lower() or "write"
            self._guard(operation)

    def _guard_script(self, sql_script) -> None:
        statements = [part.strip() for part in str(sql_script or "").split(";") if part.strip()]
        if any(self._mutates(statement) for statement in statements):
            self._guard("executescript")

    def _wrap_cursor(self, cursor):
        return _LifecycleGuardedCursor(self, cursor)

    def execute(self, sql, parameters=()):
        self._guard_sql(sql)
        return self._wrap_cursor(self._conn.execute(sql, parameters))

    def executemany(self, sql, seq_of_parameters):
        self._guard_sql(sql)
        return self._wrap_cursor(self._conn.executemany(sql, seq_of_parameters))

    def executescript(self, sql_script):
        self._guard_script(sql_script)
        return self._wrap_cursor(self._conn.executescript(sql_script))

    def cursor(self, *args, **kwargs):
        return self._wrap_cursor(self._conn.cursor(*args, **kwargs))

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._conn.__exit__(exc_type, exc, tb)


_conn_pool = threading.local()


def _conn_reuse_enabled() -> bool:
    return (os.environ.get("PM_SQLITE_CONN_REUSE", "1") or "1").strip().lower() in {"1", "true", "on", "yes"}


def _conn_pool_state() -> Dict[str, Any]:
    state = getattr(_conn_pool, "state", None)
    if state is None:
        state = {"cache": {}, "active": set()}
        _conn_pool.state = state
    return state


def _close_pooled_conns() -> None:
    """Close and drop this thread's cached connections (lifecycle / tests)."""
    state = getattr(_conn_pool, "state", None)
    if not state:
        return
    for c in list(state["cache"].values()):
        try:
            c.close()
        except Exception:
            pass
    state["cache"].clear()
    state["active"].clear()


@contextmanager
def _conn(project: str = DEFAULT_PROJECT, timeout_s: Optional[float] = None,
          read_snapshot: bool = False):
    timeout = _sqlite_timeout_s("PM_SQLITE_TIMEOUT_S", 5.0) if timeout_s is None else timeout_s
    project_config = _resolve(project)
    db_path = project_config["db"]
    # Reuse a per-thread connection to skip the ~1.2ms lazy DB-open (WAL attach + shared lock)
    # every fresh connection pays. A re-entrant _conn on the same thread+db falls back to a
    # fresh, uncached connection so nested `with c:` transactions never collide on one
    # connection — preserving the exact pre-reuse behavior. PM_SQLITE_CONN_REUSE=0 disables it.
    state = _conn_pool_state() if _conn_reuse_enabled() else None
    reuse = state is not None and db_path not in state["active"]
    if reuse:
        c = state["cache"].get(db_path)
        if c is None:
            c = _open_sqlite(db_path, timeout)
            state["cache"][db_path] = c
        else:
            # Re-apply the settings that vary per call (busy_timeout) or per env
            # (mmap_size, e.g. a background job opting into a bounded map). These are pure
            # connection settings — no DB access, ~0.01ms total — while the ~1.2ms lazy open
            # is what reuse skips. The fixed PRAGMAs (synchronous/cache/wal) persist from open.
            c.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            c.execute(f"PRAGMA mmap_size={_sqlite_mmap_bytes()}")
        state["active"].add(db_path)
    else:
        c = _open_sqlite(db_path, timeout)
    try:
        with c:
            # sqlite3 does not begin a transaction for SELECT statements by default.
            # Start the transaction on the actual connection (before proxy wrapping)
            # when a multi-query classifier requires one stable WAL snapshot.
            if read_snapshot:
                c.execute("BEGIN DEFERRED")
            if single_writer_enabled() and not in_write_worker():
                exposed = _SerializedWriteProxy(db_path, timeout, c)
            else:
                exposed = c
            yield _LifecycleGuardedConnection(project, exposed)
    except sqlite3.OperationalError:
        # A locked/broken connection must not stay cached — drop it so the next use reopens.
        if reuse:
            state["cache"].pop(db_path, None)
            try:
                c.close()
            except Exception:
                pass
        raise
    finally:
        if reuse:
            state["active"].discard(db_path)
        else:
            try:
                c.close()
            except Exception:
                pass


def _write_through(project: str, thunk, timeout_s: Optional[float] = None):
    """Serialize a multi-statement write transaction on the project's writer thread."""
    config = _resolve(project)
    assert_project_write_allowed(
        project, str(config.get("lifecycle_status") or "active"), "write_through")
    return write_through(config["db"], thunk, timeout_s=timeout_s)


def _sqlite_write_queue_stats() -> Dict[str, Any]:
    return all_queue_stats()


def _control_plane_timeout_s() -> float:
    return _sqlite_timeout_s("PM_CONTROL_PLANE_SQLITE_TIMEOUT_S", 2.0)


def _control_plane_conn(project: str = DEFAULT_PROJECT):
    return _conn(project, timeout_s=_control_plane_timeout_s())


def _control_plane_unavailable(operation: str, project: str, started_at: float,
                               exc: Exception) -> Dict[str, Any]:
    return {
        "error": "control_plane_unavailable",
        "reason": "sqlite_busy",
        "operation": operation,
        "project": project,
        "elapsed_ms": int((time.time() - started_at) * 1000),
        "timeout_ms": int(_control_plane_timeout_s() * 1000),
        "message": str(exc),
    }
