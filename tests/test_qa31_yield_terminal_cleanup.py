#!/usr/bin/env python3
"""QA-31: a yielded generation closes only after its exact host stop receipt."""
from __future__ import annotations

import sqlite3

from path_setup import ROOT  # noqa: F401
from switchboard.storage.repositories import runner


TASK_ID = "QA-31"
RUNNER_ID = "run-qa31-review"
CLAIM_ID = "claim-qa31-review"
EXECUTION_ID = "exec-qa31-review"
GENERATION = 2
AGENT_ID = "agent/codex/qa-31"


def database() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE task_claims (
            id TEXT PRIMARY KEY, task_id TEXT, agent_id TEXT, status TEXT,
            runner_session_id TEXT, claimed_at REAL, completed_at REAL
        );
        CREATE TABLE resource_leases (
            id TEXT PRIMARY KEY, resource_type TEXT, task_id TEXT,
            agent_id TEXT, released_at REAL
        );
        CREATE TABLE work_sessions (
            work_session_id TEXT PRIMARY KEY, task_id TEXT, claim_id TEXT,
            runner_session_id TEXT, status TEXT, created_at REAL,
            updated_at REAL, completed_at REAL, updated_by TEXT
        );
        CREATE TABLE activity (
            task_id TEXT, actor TEXT, kind TEXT, payload TEXT, created_at REAL
        );
    """)
    c.execute("INSERT INTO task_claims VALUES (?,?,?,?,?,?,NULL)", (
        CLAIM_ID, TASK_ID, AGENT_ID, "active", RUNNER_ID, 10.0,
    ))
    c.execute("INSERT INTO resource_leases VALUES (?,?,?,?,NULL)", (
        "lease-qa31-review", "task", TASK_ID, AGENT_ID,
    ))
    c.execute("INSERT INTO work_sessions VALUES (?,?,?,?,?,?,?,NULL,?)", (
        "ws-qa31-review", TASK_ID, CLAIM_ID, RUNNER_ID, "active",
        10.0, 10.0, "",
    ))
    return c


def metadata() -> dict:
    return {
        "execution_id": EXECUTION_ID,
        "execution_generation": GENERATION,
        "terminalized_by": "terminal_lease_surrendered",
        "lease_surrender": {
            "authority": "completion_owner",
            "execution_id": EXECUTION_ID,
            "generation": GENERATION,
            "coordination_receipt": {
                "schema": "switchboard.mission_yield_receipt.v1",
                "event_type": "agent_yielded",
                "event_id": "event-qa31-yield",
                "task_id": TASK_ID,
                "execution_id": EXECUTION_ID,
                "generation": GENERATION,
            },
        },
    }


record = {"task_id": TASK_ID, "agent_id": AGENT_ID}

# Capacity cannot invent a Coordination outcome: without the immutable yield
# receipt on the surrender, the exact terminal receipt changes nothing.
c = database()
missing_receipt = metadata()
missing_receipt["lease_surrender"].pop("coordination_receipt")
assert runner._complete_yielded_runner_cleanup_in(
    c, record, missing_receipt, RUNNER_ID, "host/qa31", 20.0,
) is None
assert c.execute(
    "SELECT status FROM task_claims WHERE id=?", (CLAIM_ID,),
).fetchone()[0] == "active"

# Once the exact generation's receipt exists, the host acknowledgement completes
# only that claim and Work Session and releases its task lease.
result = runner._complete_yielded_runner_cleanup_in(
    c, record, metadata(), RUNNER_ID, "host/qa31", 21.0,
)
assert result == {
    "completed": True,
    "idempotent": False,
    "claim_id": CLAIM_ID,
    "work_session_id": "ws-qa31-review",
}
assert tuple(c.execute(
    "SELECT status,completed_at FROM task_claims WHERE id=?", (CLAIM_ID,),
).fetchone()) == ("completed", 21.0)
assert tuple(c.execute(
    "SELECT status,completed_at FROM work_sessions WHERE work_session_id=?",
    ("ws-qa31-review",),
).fetchone()) == ("completed", 21.0)
assert c.execute(
    "SELECT released_at FROM resource_leases WHERE id='lease-qa31-review'",
).fetchone()[0] == 21.0
assert c.execute(
    "SELECT count(*) FROM activity "
    "WHERE kind='task.claim.completed_by_yield_terminal_receipt'",
).fetchone()[0] == 1
assert runner._complete_yielded_runner_cleanup_in(
    c, record, metadata(), RUNNER_ID, "host/qa31", 22.0,
)["idempotent"] is True

# Fail closed if another generation already owns the task; a late Capacity
# receipt may not release a newer Coordination lease.
c = database()
c.execute("INSERT INTO task_claims VALUES (?,?,?,?,?,?,NULL)", (
    "claim-newer", TASK_ID, AGENT_ID, "active", "run-newer", 11.0,
))
assert runner._complete_yielded_runner_cleanup_in(
    c, record, metadata(), RUNNER_ID, "host/qa31", 23.0,
) is None
assert c.execute(
    "SELECT status FROM task_claims WHERE id=?", (CLAIM_ID,),
).fetchone()[0] == "active"

print("QA-31 yielded terminal cleanup tests passed")
