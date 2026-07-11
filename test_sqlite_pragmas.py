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


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
