#!/usr/bin/env python3
"""BUG-263: exact terminal generations cannot leave active Work Sessions."""
from __future__ import annotations

import json
import sqlite3

from path_setup import ROOT  # noqa: F401
from switchboard.storage.repositories import runner


def database() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE task_claims (
            id TEXT PRIMARY KEY, task_id TEXT, agent_id TEXT, status TEXT,
            abandon_reason TEXT, runner_session_id TEXT
        );
        CREATE TABLE resource_leases (
            resource_type TEXT, task_id TEXT, agent_id TEXT, released_at REAL
        );
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, status TEXT, assignee TEXT,
            deliverable TEXT, agent_state TEXT, updated_at REAL
        );
        CREATE TABLE work_sessions (
            work_session_id TEXT PRIMARY KEY, task_id TEXT, claim_id TEXT,
            agent_id TEXT, principal_id TEXT, runner_session_id TEXT,
            repo TEXT, branch TEXT, head_sha TEXT, worktree_path TEXT,
            clone_path TEXT, status TEXT, created_at REAL,
            completed_at REAL, updated_at REAL, updated_by TEXT
        );
        CREATE TABLE runner_sessions (
            runner_session_id TEXT PRIMARY KEY, task_id TEXT, claim_id TEXT,
            agent_id TEXT, status TEXT, heartbeat_at REAL,
            heartbeat_ttl_s INTEGER, principal_id TEXT, metadata_json TEXT
        );
        CREATE TABLE task_git_state (
            task_id TEXT PRIMARY KEY, branch TEXT, head_sha TEXT,
            pr_number INTEGER, pr_url TEXT
        );
        CREATE TABLE activity (
            task_id TEXT, actor TEXT, kind TEXT, payload TEXT, created_at REAL
        );
    """)
    c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)", (
        "BUG-263", "In Review", "agent/current", None, "{}", 0,
    ))
    c.execute("INSERT INTO task_git_state VALUES (?,?,?,?,?)", (
        "BUG-263", "codex/BUG-263", "a" * 40, 1204,
        "https://github.example/pr/1204",
    ))
    return c


def add_attempt(c: sqlite3.Connection, suffix: str, *, claim_status: str,
                runner_status: str, terminalized_by: str,
                exact: bool = True, role: str = "remediation") -> None:
    claim_id = f"claim-{suffix}"
    runner_id = f"run-{suffix}"
    ws_runner_id = runner_id if exact else "run-someone-else"
    agent_id = f"agent/{suffix}"
    c.execute("INSERT INTO task_claims VALUES (?,?,?,?,?,?)", (
        claim_id, "BUG-263", agent_id, claim_status, None, runner_id,
    ))
    c.execute(
        "INSERT INTO work_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            f"ws-{suffix}", "BUG-263", claim_id, agent_id, f"principal/{suffix}",
            ws_runner_id, "6th-Element-Labs/projectplanner", "codex/BUG-263",
            "a" * 40, f"/tmp/{suffix}", "", "active", 1, None, 1, "",
        ))
    metadata = {
        "execution_role": role,
        "terminalized_by": terminalized_by,
        "work_session_id": f"ws-{suffix}",
        "lease_surrender": {"claim_id": claim_id},
    }
    c.execute("INSERT INTO runner_sessions VALUES (?,?,?,?,?,?,?,?,?)", (
        runner_id, "BUG-263", claim_id, agent_id, runner_status, 1, 60,
        f"principal/{suffix}", json.dumps(metadata),
    ))


assert runner._terminalizes_fenced_generation({
    "terminalized_by": "terminal_lease_surrendered",
})

# Historical rows reconcile only from exact terminal runner+claim authority.
c = database()
add_attempt(c, "completed", claim_status="completed", runner_status="exited",
            terminalized_by="host_supervisor")
add_attempt(c, "expired", claim_status="abandoned", runner_status="expired",
            terminalized_by="runner_lease_expiry")
add_attempt(c, "completed-expired", claim_status="completed",
            runner_status="expired", terminalized_by="runner_lease_expiry")
add_attempt(c, "abandoned", claim_status="abandoned", runner_status="exited",
            terminalized_by="host_supervisor")
add_attempt(c, "historical-binding", claim_status="abandoned",
            runner_status="exited", terminalized_by="host_supervisor")
c.execute("UPDATE runner_sessions SET claim_id=NULL,metadata_json=? "
          "WHERE runner_session_id='run-historical-binding'", (
              json.dumps({"terminalized_by": "host_supervisor"}),
          ))
add_attempt(c, "unbound", claim_status="abandoned", runner_status="exited",
            terminalized_by="host_supervisor")
c.execute("UPDATE task_claims SET runner_session_id=NULL WHERE id='claim-unbound'")
c.execute("UPDATE runner_sessions SET claim_id=NULL,metadata_json=? "
          "WHERE runner_session_id='run-unbound'", (
              json.dumps({"terminalized_by": "host_supervisor"}),
          ))
add_attempt(c, "active", claim_status="active", runner_status="exited",
            terminalized_by="host_supervisor")
add_attempt(c, "mismatch", claim_status="completed", runner_status="exited",
            terminalized_by="host_supervisor", exact=False)
reconciled = runner._reconcile_terminal_bound_work_sessions_in(
    c, "BUG-263", "host/test", 100.0)
assert len(reconciled) == 5
statuses = dict(c.execute(
    "SELECT work_session_id,status FROM work_sessions").fetchall())
assert statuses["ws-completed"] == "completed"
assert statuses["ws-expired"] == "archived"
assert statuses["ws-completed-expired"] == "archived"
assert statuses["ws-abandoned"] == "expired"
assert statuses["ws-historical-binding"] == "expired"
assert statuses["ws-unbound"] == "active"
assert statuses["ws-active"] == "active"
assert statuses["ws-mismatch"] == "active"
assert c.execute(
    "SELECT count(*) FROM activity "
    "WHERE kind='work_session.reconciled_by_terminal_runner'",
).fetchone()[0] == 5
assert runner._reconcile_terminal_bound_work_sessions_in(
    c, "BUG-263", "host/test", 101.0) == []

# A terminal remediation generation in In Review releases only itself. It
# never advances the task or marks remediation completion successful.
c = database()
add_attempt(c, "current", claim_status="active", runner_status="exited",
            terminalized_by="host_supervisor")
c.execute("INSERT INTO resource_leases VALUES (?,?,?,NULL)", (
    "task", "BUG-263", "agent/current",
))
record = {
    "runner_session_id": "run-current", "task_id": "BUG-263",
    "claim_id": "claim-current", "agent_id": "agent/current",
    "status": "exited",
}
metadata = {
    "work_session_id": "ws-current", "execution_role": "remediation",
    "terminalized_by": "host_supervisor",
}
handoff = runner._release_terminal_runner_ownership_in(
    c, record, metadata, "run-current", "host/test", 100.0)
assert handoff and handoff["role"] == "remediation"
assert c.execute(
    "SELECT status FROM task_claims WHERE id='claim-current'",
).fetchone()[0] == "abandoned"
assert c.execute(
    "SELECT status FROM work_sessions WHERE work_session_id='ws-current'",
).fetchone()[0] == "expired"
assert c.execute(
    "SELECT status FROM tasks WHERE task_id='BUG-263'",
).fetchone()[0] == "In Review"

print("BUG-263 terminal Work Session reconciliation: passed")
