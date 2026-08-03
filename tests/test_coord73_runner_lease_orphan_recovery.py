#!/usr/bin/env python3
"""COORD-73: lease death cannot leave an orphan claim covering a tip."""
from __future__ import annotations

import json
import sqlite3

from path_setup import ROOT  # noqa: F401
from switchboard.storage.repositories import deliverables, runner


def database(*, live_replacement: bool = False) -> sqlite3.Connection:
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
            agent_id TEXT, principal_id TEXT, repo TEXT, branch TEXT,
            head_sha TEXT, worktree_path TEXT, clone_path TEXT, status TEXT,
            completed_at REAL, updated_at REAL, updated_by TEXT,
            runner_session_id TEXT
        );
        CREATE TABLE task_git_state (
            task_id TEXT PRIMARY KEY, branch TEXT, head_sha TEXT,
            pr_number INTEGER, pr_url TEXT
        );
        CREATE TABLE runner_sessions (
            runner_session_id TEXT PRIMARY KEY, task_id TEXT, status TEXT,
            heartbeat_at REAL, heartbeat_ttl_s INTEGER, principal_id TEXT,
            metadata_json TEXT
        );
        CREATE TABLE activity (
            task_id TEXT, actor TEXT, kind TEXT, payload TEXT, created_at REAL
        );
    """)
    c.execute("INSERT INTO task_claims VALUES (?,?,?,?,?,?)",
              ("claim-73", "COORD-73", "agent/old", "active", None,
               "run-dead"))
    c.execute("INSERT INTO resource_leases VALUES (?,?,?,NULL)",
              ("task", "COORD-73", "agent/old"))
    c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
              ("COORD-73", "In Progress", "agent/old", None, "{}", 0))
    c.execute("INSERT INTO work_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        "ws-73", "COORD-73", "claim-73", "agent/old", "principal/old",
        "6th-Element-Labs/projectplanner", "codex/COORD-73", "a" * 40,
        "/tmp/coord73", "", "active", None, 0, "", "run-dead",
    ))
    c.execute("INSERT INTO task_git_state VALUES (?,?,?,?,?)",
              ("COORD-73", "codex/COORD-73", "a" * 40, None, None))
    c.execute("INSERT INTO runner_sessions VALUES (?,?,?,?,?,?,?)", (
        "run-dead", "COORD-73", "expired", 1, 60, "principal/old", "{}",
    ))
    if live_replacement:
        c.execute("INSERT INTO runner_sessions VALUES (?,?,?,?,?,?,?)", (
            "run-live", "COORD-73", "running", 100, 180,
            "principal/new", json.dumps({"execution_generation": 2}),
        ))
    return c


record = {
    "runner_session_id": "run-dead", "task_id": "COORD-73",
    "claim_id": "claim-73", "agent_id": "agent/old", "status": "expired",
}
metadata = {
    "terminalized_by": "runner_lease_expiry",
    "failure_reason": "runner heartbeat lease expired",
    "execution_generation": 1,
    "work_session_id": "ws-73",
}

# C3 may resolve a late terminal receipt only through the exact claim+runner
# binding; no task/agent fallback is allowed.
c = database()
handoff = runner._release_terminal_runner_ownership_in(
    c, record, metadata, "run-dead", "host/test", 100.0)
assert handoff and handoff["previous_work_session_id"] == "ws-73"
assert handoff["reason_code"] == "orphan_claim_after_runner_lease_expiry"
assert c.execute("SELECT status FROM task_claims").fetchone()[0] == "abandoned"
assert c.execute("SELECT status FROM work_sessions").fetchone()[0] == "archived"
assert c.execute("SELECT status FROM tasks").fetchone()[0] == "Not Started"

# A live replacement generation makes ownership ambiguous and fails closed.
c = database(live_replacement=True)
assert runner._release_terminal_runner_ownership_in(
    c, record, metadata, "run-dead", "host/test", 100.0) is None
assert c.execute("SELECT status FROM task_claims").fetchone()[0] == "active"
blocked = c.execute(
    "SELECT payload FROM activity "
    "WHERE kind='task.runner_lease_orphan_recovery_blocked'").fetchone()
assert blocked and json.loads(blocked[0])["needs_you"] is True

# Mission planning ignores claim-as-liveness, but honors an in-flight wake.
deliverable = {"milestones": []}
link = {
    "project_id": "switchboard", "task_id": "COORD-73",
    "role": "implementation", "blocks_deliverable": True,
    "task_detail": {
        "task_id": "COORD-73", "title": "lease orphan",
        "status": "In Progress", "workstream": "COORD",
        "active_claims": [{"claim_id": "claim-73"}],
        "execution_coverage": {"covered": False},
        "dependency_state": {"ready": True},
    },
}
actions = deliverables._mission_next_actions(deliverable, [link], None)
assert actions[0]["action"] == "resume_or_claim"
assert actions[0]["reason_code"] == "orphan_claim_after_runner_lease_expiry"
link["task_detail"]["execution_coverage"] = {
    "covered": True, "start_in_flight": True,
}
assert deliverables._mission_next_actions(deliverable, [link], None) == []

print("COORD-73 runner lease orphan recovery: passed")
