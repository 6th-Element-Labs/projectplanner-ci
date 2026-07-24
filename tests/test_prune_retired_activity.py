#!/usr/bin/env python3
"""prune_retired_activity must be dry-run by default and refuse live kinds."""
from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


spec = importlib.util.spec_from_file_location(
    "prune_retired_activity", Path(ROOT) / "scripts" / "prune_retired_activity.py")
prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune)

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def build(path: Path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE activity(id INTEGER PRIMARY KEY, task_id TEXT, actor TEXT,"
                 " kind TEXT, payload TEXT, created_at REAL)")
    now = time.time()
    rows = []
    # A retired kind, silent for days.
    for i in range(50):
        rows.append((None, "steward", "coordinator.review_steward.tick",
                     "x" * 2000, now - 5 * 86400))
    # A kind that is still being written RIGHT NOW.
    for i in range(20):
        rows.append((None, "daemon", "coordinator.daemon.tick", "y" * 100, now - 30))
    # Business history that must never be touched by this tool.
    for i in range(10):
        rows.append((None, "gate", "merge.gate", "z" * 500, now - 5 * 86400))
    conn.executemany(
        "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
        rows)
    conn.commit()
    conn.close()


def count(path: Path, kind: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM activity WHERE kind=?", (kind,)).fetchone()[0]
    finally:
        conn.close()


with tempfile.TemporaryDirectory(prefix="prune-test-") as tmp:
    db = Path(tmp) / "t.db"
    build(db)

    # 1. Default run must not write anything.
    rc = prune.main(["--db", str(db)])
    ok(rc == 0, "dry run exits 0")
    ok(count(db, "coordinator.review_steward.tick") == 50,
       "DRY RUN deletes nothing (this is the default, so a bare run is always safe)")

    # 2. A kind written seconds ago is live, not retired — must be refused even when
    #    named explicitly. This guard is what makes 'retired' a fact, not an assumption.
    rc = prune.main(["--db", str(db), "--kind", "coordinator.daemon.tick", "--apply"])
    ok(rc == 0, "explicit live kind exits 0")
    ok(count(db, "coordinator.daemon.tick") == 20,
       "REFUSES to prune a kind that is still being written")

    # 3. Apply on the genuinely retired kind.
    rc = prune.main(["--db", str(db), "--apply"])
    ok(rc == 0, "apply exits 0")
    ok(count(db, "coordinator.review_steward.tick") == 0,
       "retired steward rows are deleted on --apply")
    ok(count(db, "merge.gate") == 10,
       "business history (merge.gate) is never touched")
    ok(count(db, "coordinator.daemon.tick") == 20,
       "the live coordinator loop's rows survive the apply")

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
