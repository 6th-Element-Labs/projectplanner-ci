"""PERF-2 — single-writer SQLite serialization (LMAX write queue).

All mutating database work for a given db path is funneled through one in-process
writer thread with a bounded queue.  Readers keep independent WAL connections.
"""
from __future__ import annotations

import os
import queue
import sqlite3
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Dict, Optional

_MUTATING_SQL = frozenset(
    word.upper()
    for word in (
        "INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER",
        "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "REINDEX",
        "VACUUM", "ATTACH", "DETACH",
    )
)

_writer_ctx = threading.local()


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _positive_float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def single_writer_enabled() -> bool:
    raw = os.environ.get("PM_SQLITE_SINGLE_WRITER", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def sql_mutates(statement: str) -> bool:
    """True when *statement* is a write or transaction-control SQL command."""
    if not statement:
        return False
    token = statement.lstrip().split(None, 1)[0].upper()
    return token in _MUTATING_SQL


def in_write_worker() -> bool:
    return bool(getattr(_writer_ctx, "active", False))


class SqliteWriteQueueFull(queue.Full):
    """Raised when the bounded write queue cannot accept another item in time."""


class SqliteWriteQueue:
    """Per-database-path bounded queue drained by one daemon writer thread."""

    def __init__(self, db_path: str, maxsize: Optional[int] = None,
                 put_timeout_s: Optional[float] = None,
                 checkpoint_idle_s: Optional[float] = None):
        self.db_path = db_path
        self.maxsize = maxsize or _positive_int_env("PM_SQLITE_WRITE_QUEUE_SIZE", 256)
        self.put_timeout_s = (put_timeout_s if put_timeout_s is not None else
                              _positive_float_env("PM_SQLITE_WRITE_QUEUE_TIMEOUT_S", 30.0))
        self.checkpoint_idle_s = (checkpoint_idle_s if checkpoint_idle_s is not None else
                                  _positive_float_env("PM_SQLITE_CHECKPOINT_IDLE_S", 60.0))
        self._queue: queue.Queue = queue.Queue(maxsize=self.maxsize)
        self._metrics_lock = threading.Lock()
        self._metrics = {
            "submitted": 0,
            "completed": 0,
            "rejected": 0,
            "errors": 0,
            "write_ms_total": 0.0,
            "write_ms_max": 0.0,
            "checkpoints": 0,
        }
        self._last_checkpoint_at = time.time()
        self._thread = threading.Thread(
            target=self._worker,
            name=f"sqlite-writer:{os.path.basename(db_path)}",
            daemon=True,
        )
        self._thread.start()

    def _record_write(self, elapsed_ms: float, error: bool) -> None:
        with self._metrics_lock:
            self._metrics["completed"] += 1
            if error:
                self._metrics["errors"] += 1
            else:
                self._metrics["write_ms_total"] += elapsed_ms
                if elapsed_ms > self._metrics["write_ms_max"]:
                    self._metrics["write_ms_max"] = elapsed_ms

    def _maybe_checkpoint(self) -> None:
        if self.checkpoint_idle_s <= 0:
            return
        now = time.time()
        if now - self._last_checkpoint_at < self.checkpoint_idle_s:
            return
        self._last_checkpoint_at = now
        try:
            with sqlite3.connect(self.db_path, timeout=1.0) as conn:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            with self._metrics_lock:
                self._metrics["checkpoints"] += 1
        except sqlite3.Error:
            pass

    def _worker(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=min(5.0, max(0.5, self.checkpoint_idle_s / 4.0)))
            except queue.Empty:
                self._maybe_checkpoint()
                continue
            if item is None:
                break
            future, thunk = item
            started = time.perf_counter()
            error = False
            try:
                _writer_ctx.active = True
                future.set_result(thunk())
            except BaseException as exc:
                error = True
                future.set_exception(exc)
            finally:
                _writer_ctx.active = False
                self._record_write((time.perf_counter() - started) * 1000.0, error)
                self._queue.task_done()

    def submit(self, thunk: Callable[[], Any],
               timeout_s: Optional[float] = None) -> Any:
        if not single_writer_enabled():
            return thunk()
        if in_write_worker():
            return thunk()
        timeout = self.put_timeout_s if timeout_s is None else timeout_s
        future: Future = Future()
        with self._metrics_lock:
            self._metrics["submitted"] += 1
        try:
            self._queue.put((future, thunk), timeout=timeout)
        except queue.Full:
            with self._metrics_lock:
                self._metrics["rejected"] += 1
            raise SqliteWriteQueueFull(
                f"sqlite write queue full for {self.db_path} (maxsize={self.maxsize})"
            )
        return future.result(timeout=timeout)

    def stats(self) -> Dict[str, Any]:
        with self._metrics_lock:
            completed = self._metrics["completed"]
            avg_ms = (self._metrics["write_ms_total"] / completed) if completed else None
            return {
                "db_path": self.db_path,
                "maxsize": self.maxsize,
                "depth": self._queue.qsize(),
                "submitted": self._metrics["submitted"],
                "completed": completed,
                "rejected": self._metrics["rejected"],
                "errors": self._metrics["errors"],
                "write_avg_ms": round(avg_ms, 3) if avg_ms is not None else None,
                "write_max_ms": round(self._metrics["write_ms_max"], 3),
                "checkpoints": self._metrics["checkpoints"],
            }


_queues: Dict[str, SqliteWriteQueue] = {}
_queues_lock = threading.Lock()


def _queue_for(db_path: str) -> SqliteWriteQueue:
    with _queues_lock:
        existing = _queues.get(db_path)
        if existing is None:
            existing = SqliteWriteQueue(db_path)
            _queues[db_path] = existing
        return existing


def write_through(db_path: str, thunk: Callable[[], Any],
                  timeout_s: Optional[float] = None) -> Any:
    """Run *thunk* on the per-db writer thread; block until it finishes."""
    return _queue_for(db_path).submit(thunk, timeout_s=timeout_s)


def all_queue_stats() -> Dict[str, Any]:
    with _queues_lock:
        queues = list(_queues.values())
    return {
        "schema": "switchboard.sqlite_write_queue.v1",
        "enabled": single_writer_enabled(),
        "queues": [q.stats() for q in queues],
    }
