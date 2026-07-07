"""Webhook provenance writers must survive a transient sqlite 'database is locked'.

Regression for the dropped-PR-open-webhook bug: under board write-contention the busy_timeout
can still expire and raise 'database is locked'; with no retry the pr_opened/merge event is
silently dropped and the task's status/provenance is stranded (which then blocks the claim
gate). db.core._retry_on_locked wraps mark_task_pr_opened / mark_task_merged so a transient
lock self-recovers within the request. Non-busy errors must still propagate immediately.
"""
import os
import sqlite3
import tempfile

os.environ["PM_DB_PATH"] = tempfile.mktemp(suffix=".webhookretry.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.environ["PM_DB_PATH"] + ".reg"

import store
from db.core import _retry_on_locked, _sqlite_busy

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


# --- _retry_on_locked unit behavior ------------------------------------------------------
calls = {"n": 0}


def flaky_then_ok():
    calls["n"] += 1
    if calls["n"] <= 2:
        raise sqlite3.OperationalError("database is locked")
    return 42


calls["n"] = 0
result = _retry_on_locked(flaky_then_ok, base_delay=0.001)
check("retries transient 'database is locked' then succeeds", result == 42)
check("retried exactly until success (3 calls: 2 busy + 1 ok)", calls["n"] == 3)

# non-busy OperationalError propagates immediately, no retry
calls["n"] = 0


def hard_error():
    calls["n"] += 1
    raise sqlite3.OperationalError("no such table: nope")


try:
    _retry_on_locked(hard_error, base_delay=0.001)
    check("non-busy OperationalError propagates", False)
except sqlite3.OperationalError as e:
    check("non-busy OperationalError propagates", "no such table" in str(e))
check("non-busy error is not retried (called once)", calls["n"] == 1)

# persistent lock: exhausts attempts then raises
calls["n"] = 0


def always_locked():
    calls["n"] += 1
    raise sqlite3.OperationalError("database is locked")


try:
    _retry_on_locked(always_locked, attempts=4, base_delay=0.001)
    check("persistent lock eventually raises", False)
except sqlite3.OperationalError:
    check("persistent lock eventually raises", True)
check("persistent lock tried exactly `attempts` times", calls["n"] == 4)

check("_sqlite_busy recognizes 'database is locked'",
      _sqlite_busy(sqlite3.OperationalError("database is locked")))

# --- integration: mark_task_pr_opened retries the real write path ------------------------
store.init_db("maxwell")
now = 0.0
with store._conn("maxwell") as c:
    c.execute(
        "INSERT INTO tasks(task_id, workstream_id, title, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("WR-1", "WR", "retry probe", "Not Started", 1.0, 1.0),
    )

# make the underlying write raise 'database is locked' once, then run for real
real_impl = store._mark_task_pr_opened_impl
state = {"first": True}


def flaky_impl(*a, **k):
    if state["first"]:
        state["first"] = False
        raise sqlite3.OperationalError("database is locked")
    return real_impl(*a, **k)


store._mark_task_pr_opened_impl = flaky_impl
try:
    res = store.mark_task_pr_opened("WR-1", 999, "https://example/pr/999",
                                    "claude/WR-1-x", "deadbeef", "test", "maxwell")
finally:
    store._mark_task_pr_opened_impl = real_impl

check("mark_task_pr_opened recovers after one transient lock", res.get("status") == "In Review")
check("transient lock was actually exercised (not first-try)", state["first"] is False)
with store._conn("maxwell") as c:
    row = c.execute("SELECT status FROM tasks WHERE task_id='WR-1'").fetchone()
check("task advanced to In Review despite the dropped first write", row[0] == "In Review")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
