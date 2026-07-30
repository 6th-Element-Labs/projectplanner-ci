"""BUG-246 — SQLite connection lifetime is deterministic, never GC-dependent.

Origin: the BUG-245 incident response measured the live web app holding 210
SQLite FDs on 23 threads (49 to switchboard.db alone) and the wedged reconcile
process holding 164. The old pool kept one connection per (thread, db_path) in
``threading.local`` with no eviction and no close-on-thread-death: a dead
thread's connections sat in reference cycles until a gen-2 GC pass — which
starves exactly when memory pressure throttles the process. Each connection
also pinned a 32 MiB private page cache, so worst-case memory scaled as
threads-ever x databases x 32 MiB.

Invariants pinned here:

1. After any burst of concurrent use, idle connections per database are capped
   and the excess is CLOSED at return — not parked, not left for GC.
2. Thread death strands nothing: with the cyclic GC disabled, connections used
   by short-lived threads are still closed when the work completes.
3. Nested ``_conn`` calls on one thread get independent handles and both work
   (the old per-thread pool needed a special re-entrancy branch for this).
4. The private page cache defaults small and is env-tunable via
   PM_SQLITE_CACHE_KIB — the OS page cache, shared across connections, does
   the bulk caching instead of N private copies.

Measurement note: liveness is asserted on the CONNECTION (a closed handle
raises ProgrammingError), not on raw /proc fd counts. SQLite's POSIX VFS
deliberately defers the OS close of a file descriptor while sibling
connections to the same file exist in-process (closing it would drop the
process's fcntl locks for everyone), so fd counts legitimately exceed live
connections mid-flight. The fd-level guarantee that IS ours to make — a full
pool drain releases every descriptor — is pinned explicitly.
"""
from __future__ import annotations

import gc
import os
import sqlite3
import threading
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from db import connection


def _db_fd_count(db_path: str) -> int:
    """Count this process's open FDs that point at db_path (main file only)."""
    target = os.path.realpath(db_path)
    count = 0
    # BUG-246 asserts that a pool drain releases the test process's OWN file
    # descriptors — /proc/self is state about this very process, not about the
    # host, and there is no fixture that can stand in for the kernel's fd table.
    fd_dir = "/proc/self/fd"  # ci-hermetic: allow -- asserting release of this process's own descriptors is the point of BUG-246
    for fd in os.listdir(fd_dir):
        try:
            if os.path.realpath(os.path.join(fd_dir, fd)) == target:
                count += 1
        except OSError:
            continue
    return count


class Bug246Base(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "bug246.db")
        seed = sqlite3.connect(self.db_path)
        seed.execute("CREATE TABLE t (x INTEGER)")
        seed.execute("INSERT INTO t VALUES (1)")
        seed.commit()
        seed.close()
        self.created: list = []
        real_open = connection._open_sqlite

        def tracking_open(db_path, timeout_s):
            c = real_open(db_path, timeout_s)
            self.created.append(c)
            return c

        self.patches = [
            patch.object(
                connection, "_resolve",
                return_value={"db": self.db_path, "lifecycle_status": "active"}),
            patch.object(connection, "_open_sqlite", side_effect=tracking_open),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        connection._close_pooled_conns()
        self.tmp.cleanup()

    def _live_conns(self) -> int:
        live = 0
        for c in self.created:
            try:
                c.execute("SELECT 1")
                live += 1
            except sqlite3.ProgrammingError:
                pass  # closed — exactly what deterministic release looks like
        return live

    def _use_once(self):
        with connection._conn("switchboard") as c:
            row = c.execute("SELECT x FROM t").fetchone()
            assert row["x"] == 1


class DeterministicPoolTest(Bug246Base):
    def test_concurrent_burst_leaves_at_most_idle_cap_fds(self):
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            for _ in range(5):
                self._use_once()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(
            self._live_conns(), connection._pool_idle_cap(),
            "excess connections must be closed at check-in, not retained")

    def test_thread_death_strands_no_connections_without_gc(self):
        gc.disable()
        try:
            threads = [
                threading.Thread(target=self._use_once) for _ in range(12)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertLessEqual(
                self._live_conns(), connection._pool_idle_cap(),
                "connections owned by dead threads must be closed "
                "deterministically, never by the garbage collector (BUG-246)")
        finally:
            gc.enable()

    def test_pool_drain_releases_every_descriptor(self):
        barrier = threading.Barrier(6)

        def worker():
            barrier.wait()
            self._use_once()

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        connection._close_pooled_conns()
        self.assertEqual(
            self._live_conns(), 0,
            "draining the pool must close every connection")
        self.assertEqual(
            _db_fd_count(self.db_path), 0,
            "a full drain must release every OS descriptor, including the "
            "fds SQLite's POSIX VFS defers while sibling connections exist")

    def test_nested_conn_uses_independent_handles(self):
        with connection._conn("switchboard") as outer:
            self.assertEqual(outer.execute(
                "SELECT x FROM t").fetchone()["x"], 1)
            with connection._conn("switchboard") as inner:
                self.assertEqual(inner.execute(
                    "SELECT x FROM t").fetchone()["x"], 1)
                self.assertGreaterEqual(_db_fd_count(self.db_path), 2)
            # outer must still be usable after the inner checkout returned
            self.assertEqual(outer.execute(
                "SELECT x FROM t").fetchone()["x"], 1)

    def test_pool_reuses_a_checked_in_connection(self):
        self._use_once()
        after_first = _db_fd_count(self.db_path)
        self._use_once()
        self.assertEqual(
            _db_fd_count(self.db_path), after_first,
            "sequential use on one thread should reuse the pooled handle")


class CacheSizingTest(Bug246Base):
    def test_default_private_cache_is_small(self):
        self.assertLessEqual(
            connection._sqlite_cache_kib(), 8 * 1024,
            "private per-connection page cache must default to a few MiB; "
            "the OS page cache does the shared caching (BUG-246)")
        with connection._conn("switchboard") as c:
            cache = c.execute("PRAGMA cache_size").fetchone()[0]
        self.assertEqual(cache, -connection._sqlite_cache_kib())

    def test_cache_env_override(self):
        connection._close_pooled_conns()
        with patch.dict(os.environ, {"PM_SQLITE_CACHE_KIB": "1024"}):
            self.assertEqual(connection._sqlite_cache_kib(), 1024)
            with connection._conn("switchboard") as c:
                cache = c.execute("PRAGMA cache_size").fetchone()[0]
            self.assertEqual(cache, -1024)
        connection._close_pooled_conns()

    def test_cache_env_rejects_nonpositive(self):
        with patch.dict(os.environ, {"PM_SQLITE_CACHE_KIB": "0"}):
            with self.assertRaises(ValueError):
                connection._sqlite_cache_kib()


if __name__ == "__main__":
    unittest.main()
