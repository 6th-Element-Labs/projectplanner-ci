#!/usr/bin/env python3
"""BUG-232: stale execution authority fails the wake and releases its lease."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="bug232-stale-wake-")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_RUNNER_DIR"] = os.path.join(TMP, "runner")

import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from execution_readiness_fixture import configure_ready_project  # noqa: E402
from switchboard.application.commands import execution_context, task_execution  # noqa: E402

P = "switchboard"


def start(task_id: str, attempt: int) -> dict:
    return task_execution.start_task(
        task_id,
        project=P,
        actor="bug232-test",
        role="implementation",
        decision_attempt=attempt,
        state_version=attempt,
    )


try:
    store.init_db(P)
    configure_ready_project(P, actor="bug232-test")
    task = store.create_task(
        {"workstream_id": "BUG", "title": "BUG-232 regression", "ui_impact": "no"},
        actor="bug232-test",
        project=P,
    )
    task_id = task["task_id"]
    first = start(task_id, 1)
    assert first.get("started") is True, first
    wake_id = first["wake_id"]

    stale = execution_context.ExecutionContextError(
        "stale_execution_context",
        "canonical base changed",
        expected_digest="sha256:old",
        current_digest="sha256:new",
    )
    with patch.object(execution_context, "require_current", side_effect=stale):
        refused = store.claim_wake(
            "host/bug232",
            wake_id,
            actor="bug232-host",
            project=P,
        )

    assert refused["claimed"] is False, refused
    assert refused["reason"] == "stale_execution_context", refused
    assert refused["wake"]["status"] == "failed", refused
    assert refused["wake"]["result"]["reason"] == "stale_execution_context", refused
    assert refused["execution_leases_released"] == 1, refused

    with _conn(P) as conn:
        wake = conn.execute(
            "SELECT status,completed_at,result_json FROM wake_intents WHERE wake_id=?",
            (wake_id,),
        ).fetchone()
        lease = conn.execute(
            "SELECT released_at,lease_state,fence_epoch FROM resource_leases "
            "WHERE wake_id=?",
            (wake_id,),
        ).fetchone()
        event = conn.execute(
            "SELECT actor,payload FROM activity WHERE task_id=? "
            "AND kind='wake.failed'",
            (task_id,),
        ).fetchone()

    assert wake["status"] == "failed" and wake["completed_at"], dict(wake)
    assert lease["released_at"] is not None, dict(lease)
    assert lease["lease_state"] == "expired", dict(lease)
    assert int(lease["fence_epoch"]) == 2, dict(lease)
    assert event["actor"] == "switchboard/wake", dict(event)
    assert json.loads(event["payload"])["reason"] == "stale_execution_context"

    replacement = start(task_id, 2)
    assert replacement.get("started") is True, replacement
    assert replacement["wake_id"] != wake_id, replacement
    with _conn(P) as conn:
        generations = conn.execute(
            "SELECT execution_generation,execution_role FROM resource_leases "
            "WHERE task_id=? ORDER BY execution_generation",
            (task_id,),
        ).fetchall()
    assert [int(row["execution_generation"]) for row in generations] == [1, 2]
    assert [row["execution_role"] for row in generations] == [
        "implementation",
        "implementation",
    ]
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("BUG-232 stale wake fast fail: passed")
