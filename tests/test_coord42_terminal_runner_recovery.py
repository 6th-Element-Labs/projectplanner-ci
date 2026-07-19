#!/usr/bin/env python3
"""COORD-42 permanent runner recovery contract."""
from __future__ import annotations

import json
import sqlite3
from path_setup import ROOT  # noqa: F401

from adapters.codex.pty_stream import INJECT_KINDS, format_inject_payload
from switchboard.storage.repositories import runner


def database(status: str = "In Progress") -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE task_claims (
            id TEXT PRIMARY KEY, task_id TEXT, agent_id TEXT, status TEXT,
            abandon_reason TEXT
        );
        CREATE TABLE resource_leases (
            resource_type TEXT, task_id TEXT, agent_id TEXT, released_at REAL
        );
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, status TEXT, assignee TEXT,
            deliverable TEXT, agent_state TEXT, updated_at REAL
        );
        CREATE TABLE work_sessions (
            work_session_id TEXT PRIMARY KEY, repo TEXT, branch TEXT,
            head_sha TEXT, worktree_path TEXT, clone_path TEXT, status TEXT
        );
        CREATE TABLE task_git_state (
            task_id TEXT PRIMARY KEY, branch TEXT, head_sha TEXT,
            pr_number INTEGER, pr_url TEXT
        );
        CREATE TABLE activity (
            task_id TEXT, actor TEXT, kind TEXT, payload TEXT, created_at REAL
        );
    """)
    c.execute("INSERT INTO task_claims VALUES (?,?,?,?,?)",
              ("claim-1", "COORD-42", "codex/COORD-42", "active", None))
    c.execute("INSERT INTO resource_leases VALUES (?,?,?,NULL)",
              ("task", "COORD-42", "codex/COORD-42"))
    c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
              ("COORD-42", status, "codex/COORD-42", "autopilot", "{}", 0))
    c.execute("INSERT INTO work_sessions VALUES (?,?,?,?,?,?,?)", (
        "ws-1", "6th-Element-Labs/projectplanner", "codex/COORD-42-existing",
        "a" * 40, "/tmp/coord42", "", "expired"))
    c.execute("INSERT INTO task_git_state VALUES (?,?,?,?,?)", (
        "COORD-42", "codex/COORD-42-existing", "a" * 40, 700,
        "https://github.example/pr/700"))
    return c


def terminal_record() -> tuple[dict, dict]:
    return ({
        "runner_session_id": "run-dead", "task_id": "COORD-42",
        "claim_id": "claim-1", "agent_id": "codex/COORD-42",
        "status": "failed", "cwd": "/tmp/coord42",
    }, {
        "work_session_id": "ws-1", "role": "implementation",
        "failure_reason": "executed_tests_failed", "log_path": "/tmp/stdout.log",
    })


def test_terminal_release(status: str, expected: str) -> None:
    c = database(status)
    record, metadata = terminal_record()
    handoff = runner._release_terminal_runner_ownership_in(
        c, record, metadata, "run-dead", "test", 100.0)
    assert handoff and handoff["previous_runner_session_id"] == "run-dead"
    assert handoff["branch"] == "codex/COORD-42-existing"
    assert handoff["head_sha"] == "a" * 40
    assert c.execute("SELECT status FROM task_claims").fetchone()[0] == "abandoned"
    assert c.execute("SELECT released_at FROM resource_leases").fetchone()[0] == 100.0
    task = c.execute("SELECT status, assignee, agent_state FROM tasks").fetchone()
    assert task[0] == expected
    assert task[1] is None
    assert json.loads(task[2])["switchboard/recovery_handoff"]["attempt"] == 1
    assert runner._release_terminal_runner_ownership_in(
        c, record, metadata, "run-dead", "test", 101.0) is None
    assert c.execute("SELECT count(*) FROM activity").fetchone()[0] == 1


test_terminal_release("In Progress", "Not Started")
test_terminal_release("In Review", "In Review")
test_terminal_release("Blocked", "Blocked")

successful, successful_metadata = terminal_record()
successful["status"] = "completed"
successful_db = database("In Progress")
assert runner._release_terminal_runner_ownership_in(
    successful_db, successful, successful_metadata,
    "run-dead", "test", 100.0) is None
assert successful_db.execute("SELECT status FROM task_claims").fetchone()[0] == "active"

personal, personal_metadata = terminal_record()
personal_metadata["execution_connection_id"] = "exec-personal"
personal_db = database("In Progress")
assert runner._release_terminal_runner_ownership_in(
    personal_db, personal, personal_metadata,
    "run-dead", "test", 100.0) is None
assert personal_db.execute("SELECT status FROM task_claims").fetchone()[0] == "active"

session = {
    "runner_session_id": "run-live", "task_id": "COORD-42", "claim_id": "claim-2",
    "host_id": "host/mac", "status": "running", "stale": False,
    "control": {"runner_open": True},
    "metadata": {"native_host_execution": True, "wake_id": "wake-2",
                 "work_session_id": "ws-2"},
}
assert runner.assert_runner_watchable(session)["error_code"] == "runner_stream_not_ready"
session["metadata"].update({"pty": True, "stream_bind": "127.0.0.1", "stream_port": 5000})
assert runner.assert_runner_watchable(session)["watchable"] is True

assert "session_chat" in INJECT_KINDS
assert format_inject_payload("hello", kind="session_chat") == b"hello\n"

print("COORD-42 terminal runner recovery: passed")
