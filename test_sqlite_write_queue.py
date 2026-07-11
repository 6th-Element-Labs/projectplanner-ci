"""PERF-2 — single-writer SQLite serialization tests."""
import concurrent.futures
import os
import sqlite3
import tempfile
import threading

os.environ["PM_DB_PATH"] = tempfile.mktemp(suffix=".write-queue.db")
os.environ["PM_HELM_DB_PATH"] = os.environ["PM_DB_PATH"] + ".helm"
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.environ["PM_DB_PATH"] + ".switchboard"
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.environ["PM_DB_PATH"] + ".reg"
os.environ["PM_SQLITE_SINGLE_WRITER"] = "1"
os.environ["PM_SQLITE_WRITE_QUEUE_SIZE"] = "32"
os.environ["PM_SQLITE_WRITE_QUEUE_TIMEOUT_S"] = "10"

import store
from db.write_queue import SqliteWriteQueue, single_writer_enabled, sql_mutates


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


check("single writer enabled by default", single_writer_enabled())
check("INSERT is mutating", sql_mutates("INSERT INTO t VALUES (1)"))
check("UPDATE is mutating", sql_mutates("  update tasks set status=?"))
check("SELECT is not mutating", not sql_mutates("SELECT 1"))
check("PRAGMA is not mutating", not sql_mutates("PRAGMA journal_mode"))


store.init_db("switchboard")
created = store.create_task(
    {"workstream_id": "PERF", "title": "Write queue fixture"},
    actor="perf-2/test",
    project="switchboard",
)
task_id = created["task_id"] if isinstance(created, dict) else created
check("create_task succeeds through write queue", bool(task_id))


errors = []
barrier = threading.Barrier(8)


def concurrent_comment(index: int):
    try:
        barrier.wait(timeout=5)
        row = store.add_comment(
            task_id,
            actor=f"perf-2/agent-{index}",
            text=f"burst {index}",
            project="switchboard",
            hydrate_task=False,
        )
        if not row:
            errors.append(f"missing row from agent {index}")
    except Exception as exc:
        errors.append(str(exc))


with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    list(pool.map(concurrent_comment, range(8)))

check("concurrent writes complete without lock errors", not errors)
stats = store._sqlite_write_queue_stats()
check("queue stats schema present", stats.get("schema") == "switchboard.sqlite_write_queue.v1")
queue = stats["queues"][0] if stats.get("queues") else {}
check("writes were serialized", queue.get("completed", 0) >= 9)


db_path = tempfile.mktemp(suffix=".queue-unit.db")
q = SqliteWriteQueue(db_path, maxsize=4, put_timeout_s=2.0, checkpoint_idle_s=0)
order = []


def writer_job(label: str):
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS seq (v TEXT)")
        conn.execute("INSERT INTO seq (v) VALUES (?)", (label,))
    order.append(label)
    return label


with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    futures = [pool.submit(q.submit, lambda label=str(i): writer_job(label)) for i in range(3)]
    results = [future.result() for future in futures]
check("queue jobs complete", results == ["0", "1", "2"])
check("queue preserves submission order", order == ["0", "1", "2"])


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
