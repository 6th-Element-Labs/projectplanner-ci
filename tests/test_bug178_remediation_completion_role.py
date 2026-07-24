#!/usr/bin/env python3
"""BUG-178: remediation completion preserves exact execution-role fencing."""
from __future__ import annotations

import json
import sqlite3
import time

from path_setup import ROOT  # noqa: F401
from switchboard.storage.repositories.claims import _stage_managed_completion_stop_in


RUNNER = "run-bug178-remediation"
TASK = "BUG-178-REPRO"


def database(
    *, claim_role: str = "remediation", runner_role: str = "remediation"
) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE task_claims (
            id TEXT, task_id TEXT, runner_session_id TEXT,
            execution_generation INTEGER, execution_role TEXT, lease_epoch INTEGER
        );
        CREATE TABLE runner_sessions (
            runner_session_id TEXT, status TEXT, metadata_json TEXT
        );
        CREATE TABLE resource_leases (
            id TEXT, resource_type TEXT, released_at REAL, task_id TEXT,
            execution_role TEXT, execution_generation INTEGER, fence_epoch INTEGER
        );
        """
    )
    metadata = {
        "execution_id": "execlease-bug178",
        "execution_generation": 3,
        "execution_role": runner_role,
        "lease_epoch": 7,
    }
    connection.execute(
        "INSERT INTO task_claims VALUES (?,?,?,?,?,?)",
        ("claim-bug178", TASK, RUNNER, 3, claim_role, 7),
    )
    connection.execute(
        "INSERT INTO runner_sessions VALUES (?,?,?)",
        (RUNNER, "running", json.dumps(metadata)),
    )
    connection.execute(
        "INSERT INTO resource_leases VALUES (?,?,?,?,?,?,?)",
        ("execlease-bug178", "execution", None, TASK, claim_role, 3, 7),
    )
    return connection


def stage(connection: sqlite3.Connection):
    claim = connection.execute("SELECT * FROM task_claims").fetchone()
    return _stage_managed_completion_stop_in(
        connection,
        claim,
        {},
        {},
        "",
        "bug178-test",
        time.time(),
    )


accepted = stage(database())
assert accepted["reason"] == "completion_identity_incomplete", accepted

mismatched = stage(database(runner_role="implementation"))
assert mismatched["reason"] == "implementation_execution_binding_mismatch", mismatched
assert mismatched["failure_class"] == "unbound_identity", mismatched

unsupported = stage(database(claim_role="review", runner_role="review"))
assert unsupported["reason"] == "implementation_execution_binding_invalid", unsupported

print("BUG-178 remediation completion role: passed")
