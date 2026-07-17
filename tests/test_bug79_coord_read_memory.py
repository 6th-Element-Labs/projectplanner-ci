#!/usr/bin/env python3
"""BUG-79: Coord delta reads stay bounded by projected tasks, not activity history."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401

import scripts.switchboard_path  # noqa: F401


TMP = tempfile.mkdtemp(prefix="bug79-coord-memory-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(condition)
    failed += int(not condition)


try:
    import store
    from db.connection import _conn
    from switchboard.services.coord.router import _json_response
    from switchboard.storage.repositories import activity as activity_repo

    store.init_db("switchboard")
    arch = store.create_task({
        "title": "BUG-79 bounded delta",
        "status": "In Progress",
        "workstream_id": "ARCH-MS",
        "depends_on": [],
    }, actor="bug79/test", project="switchboard")
    other = store.create_task({
        "title": "BUG-79 lane control",
        "status": "Not Started",
        "workstream_id": "OTHER",
        "depends_on": [],
    }, actor="bug79/test", project="switchboard")
    baseline = activity_repo._activity_cursor(project="switchboard")

    now = time.time()
    arch_rows = [
        (arch["task_id"], "bug79.kind.%d" % (index % 3), "bug79/test", "{}", now + index)
        for index in range(5000)
    ]
    other_rows = [
        (other["task_id"], "bug79.other", "bug79/test", "{}", now + 6000 + index)
        for index in range(5000)
    ]
    with _conn("switchboard") as connection:
        connection.executemany(
            "INSERT INTO activity(task_id, kind, actor, payload, created_at) VALUES (?,?,?,?,?)",
            arch_rows + other_rows,
        )
        expected_cursor = connection.execute(
            "SELECT MAX(id) FROM activity WHERE task_id=?", (arch["task_id"],)
        ).fetchone()[0]

    original_load = activity_repo._load_git_state
    git_loads: list[str] = []

    def counted_load(connection, task_id):
        git_loads.append(task_id)
        return original_load(connection, task_id)

    activity_repo._load_git_state = counted_load
    try:
        delta = activity_repo.get_activity_delta(
            since_cursor=baseline, lane="ARCH-MS", project="switchboard"
        )
    finally:
        activity_repo._load_git_state = original_load

    ok(delta["cursor"] == expected_cursor,
       "lane delta cursor remains the latest matching activity id")
    ok(len(delta["updates"]) == 1 and delta["updates"][0]["task_id"] == arch["task_id"],
       "10,000 history rows project to only the matching changed task")
    ok(delta["updates"][0]["kinds"] == [
        "bug79.kind.0", "bug79.kind.1", "bug79.kind.2"
    ], "distinct kinds preserve first-seen order")
    ok(git_loads == [arch["task_id"]],
       "git provenance loads once per projected task, not once per activity row")

    payload = {"project": "switchboard", "items": [{"value": index} for index in range(1000)]}
    response = _json_response(payload)
    ok(response.media_type == "application/json" and json.loads(response.body) == payload,
       "Coord serializes repository-safe payloads directly to an immutable response body")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nBUG-79 Coord read memory: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
