#!/usr/bin/env python3
"""WATCH-15: a reaper heartbeat writes exactly one durable audit event."""
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

tmp = Path(tempfile.mkdtemp(prefix="watch15-reaped-audit-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(tmp / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(tmp / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(tmp)

import store  # noqa: E402
from db.connection import _conn  # noqa: E402

try:
    store.init_db("switchboard")
    task = store.create_task({"workstream_id": "WATCH", "title": "reaper audit"},
                             actor="test", project="switchboard")
    record = {
        "runner_session_id": "run_watch15_audit",
        "host_id": "host/watch15",
        "agent_id": "agent/codex/watch-15",
        "runtime": "codex",
        "task_id": task["task_id"],
        "status": "completed",
        "metadata": {"terminalized_by": "session_reaper",
                     "reaped_reason": "claim_completed",
                     "reaped_at": 10_000.0, "last_output_at": 9_000.0},
    }
    for _ in range(2):
        result = store.upsert_runner_session(
            record, principal_id="principal/watch15-host",
            actor="host/watch15", project="switchboard")
        assert not result.get("error"), result
    with _conn("switchboard") as c:
        rows = c.execute(
            "SELECT payload FROM activity WHERE task_id=? AND kind='runner.reaped'",
            (task["task_id"],),
        ).fetchall()
    assert len(rows) == 1
    assert "claim_completed" in rows[0]["payload"]
    print("WATCH-15 runner.reaped audit test passed")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
