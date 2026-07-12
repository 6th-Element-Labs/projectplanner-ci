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


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
