"""Webhook provenance writers serialize through the single-writer queue (PERF-2)."""
import os
import sqlite3
import tempfile

os.environ["PM_DB_PATH"] = tempfile.mktemp(suffix=".webhookretry.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.environ["PM_DB_PATH"] + ".reg"

import store
from db.core import _sqlite_busy

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}")


check("_sqlite_busy recognizes 'database is locked'",
      _sqlite_busy(sqlite3.OperationalError("database is locked")))

store.init_db("maxwell")
with store._conn("maxwell") as c:
    c.execute(
        "INSERT INTO tasks(task_id, workstream_id, title, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("WR-1", "WR", "retry probe", "Not Started", 1.0, 1.0),
    )

real_impl = store._mark_task_pr_opened_impl
state = {"calls": 0}


def counted_impl(*a, **k):
    state["calls"] += 1
    return real_impl(*a, **k)


store._mark_task_pr_opened_impl = counted_impl
try:
    res = store.mark_task_pr_opened("WR-1", 999, "https://example/pr/999",
                                    "claude/WR-1-x", "deadbeef", "test", "maxwell")
finally:
    store._mark_task_pr_opened_impl = real_impl

check("mark_task_pr_opened succeeds through write queue", res.get("status") == "In Review")
check("impl invoked exactly once", state["calls"] == 1)
with store._conn("maxwell") as c:
    row = c.execute("SELECT status FROM tasks WHERE task_id='WR-1'").fetchone()
check("task advanced to In Review", row[0] == "In Review")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
