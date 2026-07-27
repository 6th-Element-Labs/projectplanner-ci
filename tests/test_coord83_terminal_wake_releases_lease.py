#!/usr/bin/env python3
"""COORD-83: a terminal wake finishes its execution lease.

2026-07-27. COORD-79 and COORD-80 held pending Connect wakes that placement
refused (`session_policy_not_supported`, fixed separately by COORD-81). Each
wake hit its 900s deadline and went terminal:

    wake-f2a3ff02641143ee  failed  reason=deadline_expired

The autopilot then retried ~19 times per task with genuinely fresh coordinator
idempotency keys. Every single retry came back with the SAME refusal string —
`no_eligible_persistent_capacity` — the placement verdict computed at 04:11,
fifty minutes before the fix that removed that refusal even existed. Zero new
wake rows were created in 68 minutes. Meanwhile a hand-run placement against
the same policy and the same live hosts returned `assign_persistent`.

The cause is a lifetime mismatch, not a placement bug:

  * an execution lease runs ``ttl_seconds=7200``;
  * a Connect wake deadline is ``deadline_seconds=900``.

``_acquire_execution_lease_in`` decided a lease was still live from its own TTL
and ``lease_state`` alone. So for the ~1h45m after its wake died, the lease was
still "live", and ``request_wake`` coalesced every new dispatch onto that
lease's ``wake_id`` — handing back the corpse with ``coalesced: True``.
``connect_dispatch`` then read the dead wake's stored placement blob and
reported it as this attempt's result.

The effect is worse than a stall: the loop keeps producing confident receipts
while re-serving a cached answer, so repairing the underlying cause changes
nothing and the logs look like the fix did not work.

A wake that will never run again finishes its lease, whatever the TTL says.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from path_setup import ROOT, SRC  # noqa: F401

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="coord83-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from db.schema import apply_schema  # noqa: E402
from switchboard.storage.repositories.coordination import (  # noqa: E402
    _acquire_execution_lease_in,
    _lease_wake_is_terminal,
)

TASK = "COORD-79"
ROLE = "implementation"
#: The real numbers from the incident: the lease outlives the wake 8x.
LEASE_TTL = 7200
WAKE_DEADLINE = 900

passed = 0
failed = 0


def ok(condition: bool, label: str) -> None:
    global passed, failed
    if condition:
        passed += 1
        print("PASS  %s" % label)
    else:
        failed += 1
        print("FAIL  %s" % label)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    return conn


def acquire(conn, *, now: float):
    return _acquire_execution_lease_in(
        conn, task_id=TASK, role=ROLE, head_sha="", ttl_seconds=LEASE_TTL,
        agent_id="agent/codex/coord-79", principal_id="user-test", now=now)


def write_wake(conn, wake_id: str, status: str, *, requested_at: float) -> None:
    conn.execute(
        "INSERT INTO wake_intents(wake_id, source, reason, selector_json, "
        "policy_json, status, requested_at, deadline, task_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (wake_id, "connect", f"Connect assignment {TASK}", "{}", "{}",
         status, requested_at, requested_at + WAKE_DEADLINE, TASK),
    )


# ---------------------------------------------------------------------------
# 1. The incident, reproduced on the clock it actually happened on.
# ---------------------------------------------------------------------------

conn = db()
t0 = 1_785_125_492.0                       # 04:11 — the real dispatch
first = acquire(conn, now=t0)
ok(first.get("_acquired_new") is True, "first dispatch mints a fresh lease")
first_wake = str(first["wake_id"])
write_wake(conn, first_wake, "pending", requested_at=t0)

# The wake dies at its 900s deadline, exactly as the sweep recorded it.
t_dead = t0 + WAKE_DEADLINE + 5
conn.execute("UPDATE wake_intents SET status='failed' WHERE wake_id=?", (first_wake,))

# ~26 minutes later the coordinator retries. The lease TTL still has ~1h19m to
# run, which is precisely the window the bug lived in.
t_retry = t_dead + 26 * 60
ok(t_retry < t0 + LEASE_TTL,
   "the retry lands while the lease TTL is still live (the bug's window)")

second = acquire(conn, now=t_retry)
ok(second.get("_acquired_new") is True,
   "a retry after the wake died mints a NEW lease, not the corpse")
ok(str(second["wake_id"]) != first_wake,
   "the new lease reserves a different wake id, so request_wake cannot coalesce "
   "onto the dead wake and replay its stale placement verdict")
ok(int(second["execution_generation"]) > int(first["execution_generation"]),
   "the execution generation advances")

released = conn.execute(
    "SELECT lease_state, released_at, fence_epoch FROM resource_leases WHERE id=?",
    (first["id"],)).fetchone()
ok(released["released_at"] is not None and released["lease_state"] == "expired",
   "the finished lease is released, not left dangling")
ok(int(released["fence_epoch"]) > int(first["fence_epoch"] or 0),
   "the fence epoch bumps so a late runner from the dead generation is fenced")


# ---------------------------------------------------------------------------
# 2. The invariant this must NOT break: a live launch still coalesces.
#
# Without this, the fix would trade a stalled task for a double-booted runner —
# strictly worse. A lease whose wake is still in flight must be reused.
# ---------------------------------------------------------------------------

for live_status in ("pending", "claimed"):
    conn = db()
    base = acquire(conn, now=t0)
    write_wake(conn, str(base["wake_id"]), live_status, requested_at=t0)
    again = acquire(conn, now=t0 + 30)
    ok(again.get("_acquired_new") is not True
       and str(again["wake_id"]) == str(base["wake_id"]),
       f"a wake still {live_status} keeps its lease — no double boot")

# A lease reserves its wake id BEFORE the row exists. That gap must read as
# "starting", never "finished", or a concurrent start races in mid-launch.
conn = db()
base = acquire(conn, now=t0)
ok(_lease_wake_is_terminal(conn, base) is False,
   "a reserved wake id with no row yet is not terminal (mid-launch is not done)")
again = acquire(conn, now=t0 + 5)
ok(again.get("_acquired_new") is not True,
   "the mid-launch gap still coalesces onto the in-flight lease")

conn = db()
base = acquire(conn, now=t0)
base_row = dict(base)
base_row["wake_id"] = ""
ok(_lease_wake_is_terminal(conn, base_row) is False,
   "a lease with no wake binding at all is never treated as terminal")


# ---------------------------------------------------------------------------
# 3. Every terminal status releases, not just the one the incident hit.
# ---------------------------------------------------------------------------

for status in ("failed", "cancelled", "completed", "expired"):
    conn = db()
    base = acquire(conn, now=t0)
    write_wake(conn, str(base["wake_id"]), status, requested_at=t0)
    ok(_lease_wake_is_terminal(conn, base) is True,
       f"wake status {status!r} finishes the lease")
    nxt = acquire(conn, now=t0 + 60)
    ok(nxt.get("_acquired_new") is True,
       f"wake status {status!r} lets the next dispatch mint a fresh lease")

print(f"\ncoord83 terminal wake releases lease: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
