#!/usr/bin/env python3
"""Regression tests for bounded Switchboard control-plane calls.

The host/wake APIs are operator-facing control surfaces. Under SQLite write
contention they must report an explicit failure quickly instead of making an
agent, cockpit, or coordinator wait indefinitely.
"""
import os
import shutil
import sqlite3
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="switchboard-control-plane-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev"
os.environ["PM_CONTROL_PLANE_SQLITE_TIMEOUT_S"] = "0.05"

import store  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def lock_db():
    con = sqlite3.connect(store._resolve(P)["db"], timeout=5)
    con.execute("BEGIN EXCLUSIVE")
    con.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                (None, "test/locker", "lock.probe", "{}", time.time()))
    return con


try:
    store.init_db(P)

    lock = lock_db()
    try:
        started = time.time()
        result = store.register_host(
            {
                "host_id": "host/locked",
                "runtimes": [{"runtime": "claude-code", "lanes": ["BUG"]}],
                "limits": {"max_sessions": 1},
            },
            project=P,
        )
        elapsed = time.time() - started
        ok(elapsed < 1.0, "register_host returns before the operator path can hang")
        ok(result.get("error") == "control_plane_unavailable",
           "register_host reports an explicit control-plane error")
        ok(result.get("reason") == "sqlite_busy",
           "register_host preserves the concrete SQLite busy reason")
        ok(result.get("timeout_ms") <= 100,
           "register_host reports the bounded timeout used")
    finally:
        lock.rollback()
        lock.close()

    host = store.register_host(
        {
            "host_id": "host/ok",
            "runtimes": [{"runtime": "claude-code", "lanes": ["BUG"],
                          "capabilities": ["wake"]}],
            "limits": {"max_sessions": 1},
        },
        project=P,
    )
    ok(host.get("host_id") == "host/ok", "register_host still succeeds after contention clears")

    wake = store.request_wake(
        selector={"runtime": "claude-code", "lane": "BUG", "capabilities": ["wake"]},
        reason="fail-fast proof",
        source="test",
        policy={"deadline_seconds": 30},
        project=P,
    )
    ok(wake.get("status") == "pending", "request_wake still succeeds after contention clears")

    lock = lock_db()
    try:
        started = time.time()
        claim = store.claim_wake("host/ok", wake["wake_id"], project=P)
        elapsed = time.time() - started
        ok(elapsed < 1.0, "claim_wake returns before the coordinator path can hang")
        ok(claim.get("claimed") is False and claim.get("error") == "control_plane_unavailable",
           "claim_wake reports unavailable instead of claiming under lock contention")
    finally:
        lock.rollback()
        lock.close()

    unlocked_claim = store.claim_wake("host/ok", wake["wake_id"], project=P)
    ok(unlocked_claim.get("claimed") is True,
       "claim_wake works normally once the lock is released")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
