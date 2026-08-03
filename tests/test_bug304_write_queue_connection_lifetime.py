"""BUG-304 — write-queue checkpoint connections close deterministically."""
from __future__ import annotations

import sqlite3
import threading
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401
from db.write_queue import SqliteWriteQueue


class _CheckpointConnection:
    """Expose sqlite's transaction context behavior without GC cleanup."""

    def __init__(self, error: sqlite3.Error | None = None):
        self.error = error
        self.closed = False
        self.executed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str):
        self.executed.append(sql)
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


def _checkpoint_queue() -> SqliteWriteQueue:
    queue = object.__new__(SqliteWriteQueue)
    queue.db_path = "/tmp/bug304-checkpoint.db"
    queue.checkpoint_idle_s = 1.0
    queue._last_checkpoint_at = 0.0
    queue._metrics_lock = threading.Lock()
    queue._metrics = {"checkpoints": 0}
    return queue


class WriteQueueConnectionLifetimeTest(unittest.TestCase):
    def test_successful_checkpoint_closes_connection(self):
        connection = _CheckpointConnection()
        queue = _checkpoint_queue()

        with patch("db.write_queue.sqlite3.connect", return_value=connection):
            queue._maybe_checkpoint()

        self.assertEqual(connection.executed, ["PRAGMA wal_checkpoint(PASSIVE)"])
        self.assertTrue(connection.closed)
        self.assertEqual(queue._metrics["checkpoints"], 1)

    def test_failed_checkpoint_still_closes_connection(self):
        connection = _CheckpointConnection(sqlite3.OperationalError("checkpoint failed"))
        queue = _checkpoint_queue()

        with patch("db.write_queue.sqlite3.connect", return_value=connection):
            queue._maybe_checkpoint()

        self.assertTrue(connection.closed)
        self.assertEqual(queue._metrics["checkpoints"], 0)


if __name__ == "__main__":
    unittest.main()
