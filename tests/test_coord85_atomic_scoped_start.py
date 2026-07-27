#!/usr/bin/env python3
"""COORD-85: scoped Start and capacity admission share one generation lifetime."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from path_setup import ROOT, SRC  # noqa: F401

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="coord85-")
DB = str(Path(TMP) / "switchboard.db")
os.environ["PM_DB_PATH"] = DB
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = DB
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from db.connection import _conn  # noqa: E402
from db.schema import apply_schema  # noqa: E402
from switchboard.storage.repositories import coordination  # noqa: E402

PROJECT = "switchboard"
TASK = "COORD-85"


with sqlite3.connect(DB) as raw:
    raw.row_factory = sqlite3.Row
    apply_schema(raw)


def start(idem_key: str) -> dict:
    return coordination.request_wake(
        selector={"runtime": "codex", "agent_id": "agent/codex/coord-85"},
        reason=f"Connect assignment {TASK}",
        source="connect",
        task_id=TASK,
        actor="operator-test",
        project=PROJECT,
        idem_key=idem_key,
        policy={
            "deadline_seconds": 30,
            "no_eligible_host": "fail",
            "assignment": {
                "schema": "switchboard.connect.assignment.v1",
                "assignment_id": f"assignment-{idem_key}",
            },
            "lifecycle": {
                "schema": "switchboard.execution_lifecycle.v1",
                "role": "implementation",
                "head_sha": "",
                "ttl_seconds": 7200,
            },
            "coordination_scope": {
                "schema": "switchboard.scoped_start_request.v1",
                "scope_type": "task",
                "task_project": PROJECT,
                "task_id": TASK,
                "runtime": "codex",
                "started_by": "operator-test",
            },
        },
    )


first = start("coord85:first")
assert first["status"] == "failed", first
assert first["scope"]["scope_id"], first

with _conn(PROJECT) as conn:
    wake = conn.execute(
        "SELECT deadline,requested_at FROM wake_intents WHERE wake_id=?",
        (first["wake_id"],),
    ).fetchone()
    lease = conn.execute(
        "SELECT * FROM resource_leases WHERE wake_id=?", (first["wake_id"],),
    ).fetchone()
    scope = conn.execute(
        "SELECT * FROM autopilot_scopes WHERE task_id=?", (TASK,),
    ).fetchone()
    assert wake and lease and scope
    assert int(wake["deadline"] - wake["requested_at"]) == int(lease["ttl_seconds"]) == 30
    assert lease["lease_state"] == "expired" and lease["released_at"] is not None
    assert int(lease["fence_epoch"]) == 2

second = start("coord85:second")
assert second["status"] == "failed", second
assert second["wake_id"] != first["wake_id"]
assert second["scope"]["scope_id"] == first["scope"]["scope_id"]

with _conn(PROJECT) as conn:
    leases = conn.execute(
        "SELECT execution_generation,wake_id,lease_state,fence_epoch "
        "FROM resource_leases WHERE task_id=? ORDER BY execution_generation",
        (TASK,),
    ).fetchall()
    assert [row["execution_generation"] for row in leases] == [1, 2]
    assert all(row["lease_state"] == "expired" for row in leases)
    assert all(int(row["fence_epoch"]) == 2 for row in leases)
    scope = conn.execute(
        "SELECT last_result_json,started_by,started_at FROM autopilot_scopes "
        "WHERE task_id=?", (TASK,),
    ).fetchone()
    assert scope["started_by"] == "operator-test" and scope["started_at"]
    assert second["wake_id"] in scope["last_result_json"]

print("coord85 atomic scoped Start: passed")
