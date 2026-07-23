#!/usr/bin/env python3
"""SIMPLIFY-18: every execution surface shares one presence projection."""
from __future__ import annotations

import os
import shutil
import tempfile

TMP = tempfile.mkdtemp(prefix="simplify-18-")
os.environ["PM_DB_PATH"] = os.path.join(TMP, "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_SQLITE_SINGLE_WRITER"] = "0"

from path_setup import ROOT  # noqa: E402
import store  # noqa: E402
from switchboard.application.commands import task_execution  # noqa: E402
from switchboard.application.queries.execution_presence import get_execution_presence  # noqa: E402
from switchboard.storage.repositories import claims, runner, work_sessions  # noqa: E402

P = "switchboard"


try:
    store.init_db(P)
    first = store.create_task({"workstream_id": "SIMPLIFY", "title": "leased"}, project=P)
    second = store.create_task({"workstream_id": "SIMPLIFY", "title": "free"}, project=P)
    task_id = first["task_id"]
    session = work_sessions.create_work_session({
        "schema": "switchboard.work_session.v1",
        "project_id": P,
        "task_id": task_id,
        "agent_id": "cursor/operator-desktop",
        "runtime": "cursor",
        "repo_role": "canonical",
        "storage_mode": "external",
        "status": "active",
        "dirty_status": "clean",
    }, project=P)["work_session"]

    rows = runner.list_runner_sessions(task_id=task_id, project=P)
    assert len(rows) == 1, rows
    advisory = rows[0]
    assert advisory["metadata"]["execution_tier"] == "advisory", advisory
    assert advisory["control"]["runner_kill"] is False, advisory
    assert "kill" not in advisory["available_actions"], advisory

    presence = get_execution_presence(task_id, project=P)
    assert presence["leased"] is True, presence
    assert {"runner_sessions", "work_sessions"}.issubset(presence["sources"]), presence
    started = task_execution.start_task(task_id, project=P)
    assert started["action"] == "already_leased", started
    assert started["presence"]["schema"] == "switchboard.execution_presence.v1", started

    claimed = claims.claim_task(
        task_id, "cursor/operator-desktop", project=P,
        work_session_id=session["work_session_id"])
    assert claimed["claimed"] is True, claimed
    rebound = runner.list_runner_sessions(task_id=task_id, project=P)
    assert len(rebound) == 1 and rebound[0]["claim_id"] == claimed["claim_id"], rebound

    scheduled = claims.claim_next("codex/scheduler", project=P)
    assert scheduled["claimed"] is True, scheduled
    assert scheduled["task"]["task_id"] == second["task_id"], scheduled

    source = (ROOT / "src/switchboard/storage/repositories/claims.py").read_text()
    assert "active_task_ids_in(c, now)" in source
    assert 'SELECT task_id FROM task_claims WHERE status=\'active\'' not in source
    fleet_js = (ROOT / "static/js/runner-session.js").read_text()
    assert "const advisory = (s.metadata || {}).execution_tier === 'advisory'" in fleet_js
    assert "${canKill ? btn('kill'" in fleet_js
    print("SIMPLIFY-18 execution presence: 16 passed, 0 failed")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
