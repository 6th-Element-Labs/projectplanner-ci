#!/usr/bin/env python3
"""BUG-211: failed pre-registration wakes terminalize exactly once."""
from __future__ import annotations

import dataclasses
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="bug211-preregistration-")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_RUNNER_DIR"] = os.path.join(TMP, "runner")

if sys.version_info < (3, 10):
    _dataclass = dataclasses.dataclass

    def _compat_dataclass(*args, **kwargs):
        kwargs.pop("slots", None)
        return _dataclass(*args, **kwargs)

    dataclasses.dataclass = _compat_dataclass

import adapters.agent_host as agent_host  # noqa: E402
import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from execution_readiness_fixture import configure_ready_project  # noqa: E402
from switchboard.application.commands import task_execution  # noqa: E402

P = "switchboard"


def start(task_id: str, attempt: int) -> dict:
    return task_execution.start_task(
        task_id,
        project=P,
        actor="bug211-test",
        role="implementation",
        decision_attempt=attempt,
        state_version=attempt,
    )


try:
    store.init_db(P)
    configure_ready_project(P, actor="bug211-test")
    task = store.create_task(
        {"workstream_id": "BUG", "title": "BUG-211 regression", "ui_impact": "no"},
        actor="bug211-test",
        project=P,
    )
    task_id = task["task_id"]
    first = start(task_id, 1)
    assert first.get("started") is True, first
    wake_id = first["wake_id"]
    wake = store.list_wake_intents(
        wake_id=wake_id, project=P, limit=1)[0]
    with _conn(P) as conn:
        conn.execute(
            "UPDATE wake_intents SET status='claimed',claimed_by_host=?,"
            "claimed_at=requested_at WHERE wake_id=?",
            ("host/bug211", wake_id),
        )

    failure = {
        "started": False,
        "reason": "runtime_launch_exception",
        "failure_class": "failed_gate",
        "provider_error": (
            "connect execution assignment disagrees with persisted lease: "
            "execution_assignment_contract_mismatch"
        ),
        "task_id": task_id,
    }
    terminal = store.complete_wake(
        wake_id,
        runner_session_id="run_bug211_failed",
        agent_id=(wake.get("selector") or {}).get("agent_id") or "",
        result=failure,
        actor="bug211-test",
        project=P,
    )
    assert terminal.get("status") == "failed", terminal
    assert terminal.get("result") == failure, terminal

    replay = store.complete_wake(
        wake_id,
        runner_session_id="run_bug211_failed",
        agent_id=(wake.get("selector") or {}).get("agent_id") or "",
        result=failure,
        actor="bug211-test",
        project=P,
    )
    assert replay.get("note") == "idempotent terminal readback", replay

    zombie = store.complete_wake(
        wake_id,
        runner_session_id="run_bug211_zombie",
        agent_id=(wake.get("selector") or {}).get("agent_id") or "",
        result={"started": True, "reason": "started"},
        actor="bug211-test",
        project=P,
    )
    assert zombie.get("reason") == "exact_binding_denied", zombie
    assert zombie.get("reason_codes") == ["terminal_wake_rewrite_denied"], zombie

    with _conn(P) as conn:
        receipts = conn.execute(
            "SELECT COUNT(*) AS n FROM activity "
            "WHERE task_id=? AND kind='wake.failed'", (task_id,),
        ).fetchone()["n"]
        lease = conn.execute(
            "SELECT released_at,lease_state,fence_epoch "
            "FROM resource_leases WHERE wake_id=?", (wake_id,),
        ).fetchone()
    assert receipts == 1, receipts
    assert lease["released_at"] is not None, dict(lease)
    assert lease["lease_state"] == "failed", dict(lease)
    assert int(lease["fence_epoch"]) == 2, dict(lease)

    replacement = start(task_id, 2)
    assert replacement.get("started") is True, replacement
    assert replacement.get("wake_id") != wake_id, replacement

    callback = {
        "project": P,
        "wake_id": "wake-durable-bug211",
        "runner_session_id": "run-durable-bug211",
        "agent_id": "agent/codex/bug-211",
        "result": failure,
    }
    with patch.object(agent_host, "_try", return_value=None), \
            patch.object(agent_host.time, "sleep", return_value=None):
        exhausted = agent_host._complete_wake_with_retry(
            callback, attempts=3, delay_s=0)
    assert exhausted is None
    receipt_path = agent_host._pending_wake_receipt_path(
        P, callback["wake_id"])
    assert receipt_path.exists(), receipt_path

    with patch.object(
            agent_host, "_require",
            return_value={"status": "failed",
                          "note": "idempotent terminal readback"}):
        drained = agent_host._drain_pending_wake_receipts()
    assert drained == [{
        "wake_id": callback["wake_id"],
        "task_id": task_id,
        "reason": "pending_wake_completion_retry",
        "completed": True,
        "archived": False,
        "error": None,
    }], drained
    assert not receipt_path.exists(), Path(receipt_path)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("BUG-211 failed pre-registration wake: passed")
