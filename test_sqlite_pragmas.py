"""HARDEN-47 — every board connection applies the production WAL tuning."""
import os
import tempfile


os.environ["PM_DB_PATH"] = tempfile.mktemp(suffix=".sqlite-pragmas.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.environ["PM_DB_PATH"] + ".reg"

import store


passed = 0
failed = 0


def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}")


with store._conn("maxwell", timeout_s=1.25) as conn:
    check("WAL journal mode is active",
          conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")
    check("synchronous=NORMAL shortens the WAL writer lock window",
          conn.execute("PRAGMA synchronous").fetchone()[0] == 1)
    check("busy timeout remains bound to the requested connection timeout",
          conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1_250)
    check("cache is a stable 32 MiB rather than a page count",
          conn.execute("PRAGMA cache_size").fetchone()[0] == -(32 * 1024))
    check("256 MiB memory mapping is enabled",
          conn.execute("PRAGMA mmap_size").fetchone()[0] == 256 * 1024 * 1024)
    check("WAL checkpoints are amortized across 4,000 pages",
          conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 4_000)

os.environ["PM_SQLITE_MMAP_BYTES"] = str(32 * 1024 * 1024)
with store._conn("maxwell") as conn:
    check("background jobs can opt into a bounded SQLite memory map",
          conn.execute("PRAGMA mmap_size").fetchone()[0] == 32 * 1024 * 1024)
os.environ.pop("PM_SQLITE_MMAP_BYTES", None)

# --- redundant journal_mode PRAGMA is skipped after the first open (perf: was ~45% of the
# web worker's CPU), yet connections still open in WAL because it is persistent ---
import db.connection as _dbc    # noqa: E402

# 1) First open of an UNconfirmed db runs the PRAGMA (WAL active) and records the path.
_wal_db = tempfile.mktemp(suffix=".wal-once.db")
_dbc._wal_confirmed_paths.discard(_wal_db)
check("fresh db path starts un-confirmed", _wal_db not in _dbc._wal_confirmed_paths)
_modes = []
for _ in range(5):
    _conn = _dbc._open_sqlite(_wal_db, 5.0)
    _modes.append(_conn.execute("PRAGMA journal_mode").fetchone()[0].lower())
    _conn.close()
check("first open confirms WAL for the db path (then the PRAGMA is skipped)",
      _wal_db in _dbc._wal_confirmed_paths)
check("every connection opens in WAL (persistent) across 5 opens", _modes == ["wal"] * 5)

# 2) When a path is ALREADY confirmed, _open_sqlite must SKIP the PRAGMA — proven by a fresh
# db (default journal) pre-marked confirmed staying non-WAL because the PRAGMA never ran.
_skip_db = tempfile.mktemp(suffix=".wal-skip.db")
_dbc._wal_confirmed_paths.add(_skip_db)
_sc = _dbc._open_sqlite(_skip_db, 5.0)
_skip_mode = _sc.execute("PRAGMA journal_mode").fetchone()[0].lower()
_sc.close()
check("a WAL-confirmed path skips the journal_mode PRAGMA (fresh db stays non-WAL — proves the skip)",
      _skip_mode != "wal")


# --- per-thread connection REUSE skips the ~1.2ms lazy DB-open; re-entrancy stays fresh ---
os.environ["PM_SQLITE_CONN_REUSE"] = "1"
_dbc._close_pooled_conns()
_pm = _dbc._resolve("maxwell")["db"]

with _dbc._conn("maxwell"):
    pass
_c_first = _dbc._conn_pool_state()["cache"].get(_pm)
with _dbc._conn("maxwell"):
    pass
_c_second = _dbc._conn_pool_state()["cache"].get(_pm)
check("connection is cached and reused across non-nested _conn calls",
      _c_first is not None and _c_first is _c_second)

with _dbc._conn("maxwell"):
    _active_during = _pm in _dbc._conn_pool_state()["active"]
    with _dbc._conn("maxwell"):   # nested on same thread+db: must open fresh, not collide
        pass
check("nested _conn is re-entrancy-safe (path is marked active while in use)", _active_during)
check("active set is released after the block", _pm not in _dbc._conn_pool_state()["active"])

store.init_db("maxwell")  # this test file never seeded the db; needed for a real write+read
store.set_meta("reuse_probe", "v1", project="maxwell")
check("write-then-read is correct over reused connections",
      store.get_meta("reuse_probe", project="maxwell") == "v1")

store.set_meta("snapshot_probe", "before", project="maxwell")
with _dbc._conn("maxwell", read_snapshot=True) as _snapshot:
    _snapshot_before = _snapshot.execute(
        "SELECT value FROM meta WHERE key='snapshot_probe'").fetchone()[0]
    store.set_meta("snapshot_probe", "after", project="maxwell")
    _snapshot_after = _snapshot.execute(
        "SELECT value FROM meta WHERE key='snapshot_probe'").fetchone()[0]
check("read_snapshot holds one WAL view across concurrent committed writes",
      _snapshot_before == '"before"' and _snapshot_after == '"before"'
      and store.get_meta("snapshot_probe", project="maxwell") == "after")

# honoring per-call timeout on a reused connection
with store._conn("maxwell", timeout_s=2.5) as _tc:
    check("reused connection honors the per-call busy_timeout",
          _tc.execute("PRAGMA busy_timeout").fetchone()[0] == 2_500)

os.environ["PM_SQLITE_CONN_REUSE"] = "0"
_dbc._close_pooled_conns()
with _dbc._conn("maxwell"):
    pass
check("PM_SQLITE_CONN_REUSE=0 kill switch disables caching (fresh conn per op)",
      not _dbc._conn_pool_state()["cache"])
os.environ["PM_SQLITE_CONN_REUSE"] = "1"
_dbc._close_pooled_conns()

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
