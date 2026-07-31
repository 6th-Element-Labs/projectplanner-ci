#!/usr/bin/env python3
"""BUG-243: coordinator persistence never contains its previous full tick.

The production failure grew ``autopilot_scopes.last_result_json`` to tens of
megabytes by embedding the previous scope row in the next completion snapshot.
Scope scheduling needs metadata only, while the durable scope receipt needs
only the last task outcome/effect. Full decision evidence already has its own
append-only stores.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch


tmp = tempfile.mkdtemp(prefix="bug243-bounded-scope-")
for name in (
    "PM_DB_PATH",
    "PM_HELM_DB_PATH",
    "PM_SWITCHBOARD_DB_PATH",
    "PM_PROJECT_REGISTRY_DB_PATH",
):
    os.environ[name] = str(Path(tmp) / f"{name.lower()}.db")

from path_setup import ROOT  # noqa: E402,F401
import coordinator_daemon  # noqa: E402
import store  # noqa: E402
from switchboard.storage.repositories import autopilot_scopes  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


print("BUG-243 bounded Autopilot persistence")
project = "switchboard"

try:
    store.init_db(project)
    task = store.create_task(
        {"workstream_id": "BUG", "title": "bounded coordinator state"},
        actor="test/BUG-243", project=project)
    task_id = task["task_id"]
    scope = autopilot_scopes.start_autopilot_scope(
        project=project, scope_type="task", task_project=project,
        task_id=task_id, runtime="codex", actor="test/BUG-243")
    scope_id = scope["scope_id"]

    # Reproduce the old persisted shape: each generation contains the previous
    # result more than once, so a small leaf becomes a multi-megabyte JSON row.
    nested = {"status": "waiting", "padding": "x" * 20_000}
    for _ in range(5):
        nested = {
            "status": "running",
            "snapshot": {"autopilot_scope": {"last_result": nested}},
            "receipts": [nested, nested],
        }
    giant_json = json.dumps(nested, sort_keys=True)
    ok(len(giant_json.encode("utf-8")) > 1_000_000,
       "fixture reproduces a recursively amplified scope result")
    with store._conn(project) as connection:
        connection.execute(
            "UPDATE autopilot_scopes SET last_result_json=? WHERE scope_id=?",
            (giant_json, scope_id),
        )

    # Scheduler/hydrator callers can fetch the scope without decoding any
    # result at all.
    with patch.object(
        autopilot_scopes.json,
        "loads",
        side_effect=AssertionError("metadata read parsed last_result"),
    ):
        metadata = autopilot_scopes.list_autopilot_scopes(
            project=project, task_id=task_id, include_last_result=False)
    ok(len(metadata) == 1 and "last_result" not in metadata[0],
       "metadata-only scope listing never parses last_result")

    # Diagnostic/UI reads remain safe even before cleanup: SQLite returns a
    # tiny marker rather than materializing the oversized JSON in Python.
    safe = autopilot_scopes.get_autopilot_scope(scope_id, project=project)
    safe_result = safe.get("last_result") or {}
    ok(safe.get("last_result_compacted") is True
       and safe_result.get("result_compacted") is True
       and safe.get("last_result_bytes", 0) > 1_000_000,
       "legacy oversized rows read back as a bounded compaction marker")
    ok(len(json.dumps(safe_result)) < 1_000,
       "safe readback does not return the recursive payload")

    # The storage boundary is a final fail-safe even if a future caller tries
    # to persist another full result.
    written = autopilot_scopes.update_autopilot_scope(
        scope_id, project=project, last_result=nested)
    with store._conn(project) as connection:
        stored_json = connection.execute(
            "SELECT last_result_json FROM autopilot_scopes WHERE scope_id=?",
            (scope_id,),
        ).fetchone()[0]
    ok(len(stored_json.encode("utf-8"))
       <= autopilot_scopes.AUTOPILOT_SCOPE_RESULT_MAX_BYTES,
       "scope storage refuses to persist more than the bounded receipt limit")
    ok(written.get("last_result_compacted") is True
       and "snapshot" not in stored_json,
       "oversized writes retain only a compaction receipt")

    # Normal writes use the useful compact schema rather than the fallback
    # marker, preserving exactly the facts operators and the UI need.
    fat_tick = {
        "schema": "switchboard.completion_tick.v1",
        "task_id": task_id,
        "snapshot": {"autopilot_scope": {"last_result": nested}},
        "decision": {
            "mission_output": "wait",
            "reason_code": "ci_pending",
            "effect": "wait",
        },
        "observation": {
            "output": "wait",
            "reason_code": "ci_pending",
            "head_sha": "abc123",
        },
        "execution": {
            "receipt": {
                "effect": "wait",
                "verified": False,
                "pending": True,
            },
        },
    }
    summary = coordinator_daemon.summarize_scope_result({
        "scope_id": scope_id,
        "scope_type": "task",
        "task_id": task_id,
        "status": "completion_tick",
        "receipts": [fat_tick],
    })
    summary_json = json.dumps(summary, sort_keys=True)
    receipt = summary["receipts"][0]
    ok(len(summary_json) < 4_000
       and "snapshot" not in summary_json
       and "last_result" not in summary_json,
       "scope receipt drops snapshots, dossiers, and recursive history")
    ok(receipt.get("task_id") == task_id
       and receipt.get("reason_code") == "ci_pending"
       and receipt.get("head_sha") == "abc123"
       and receipt.get("effect") == "wait"
       and receipt.get("effect_pending") is True
       and receipt.get("effect_verified") is False,
       "bounded receipt preserves task, outcome, reason, head, and effect truth")

    many = coordinator_daemon.summarize_scope_result({
        "scope_id": scope_id,
        "status": "running",
        "receipts": [
            {
                **fat_tick,
                "task_id": f"BUG-{index}",
                "observation": {
                    "output": "x" * 100_000,
                    "reason_code": "y" * 100_000,
                    "head_sha": f"{index:040d}",
                },
            }
            for index in range(100)
        ],
    })
    many_json = json.dumps(many, sort_keys=True)
    ok(len(many_json.encode("utf-8"))
       < autopilot_scopes.AUTOPILOT_SCOPE_RESULT_MAX_BYTES
       and many.get("receipt_count") == 100
       and many.get("receipts_truncated") == 68
       and len(many.get("receipts") or []) == 32,
       "daemon metadata remains bounded under a 100-receipt adversarial tick")

    # Standalone task scopes have an empty deliverable_id.  The compactor
    # correctly omits that optional value, and the daemon's activity projection
    # must accept the compact receipt instead of crashing on a missing key.
    class ActivityStore:
        def __init__(self):
            self.activities = []

        @staticmethod
        def get_meta(_key, default=None, **_kwargs):
            return default

        @staticmethod
        def _activity_cursor(_project):
            return 0

        def append_activity(self, kind, actor, payload, **kwargs):
            self.activities.append((kind, actor, payload, kwargs))

    activity_store = ActivityStore()
    daemon = coordinator_daemon.CoordinatorDaemon(
        coordinator_daemon.DaemonConfig(projects=("switchboard",)),
        store_mod=activity_store,
        instance_id="bug243-compact-consumer",
        clock=lambda: 1.0,
    )
    daemon._admitted_projects = ("switchboard",)
    daemon._execution_policy = lambda _project: {"configured": False}
    daemon._register_or_heartbeat = lambda _project: {}
    daemon._state = lambda _project: {"sequence": 0}
    daemon._acquire_leadership = lambda _project, _state: {"leader": True}
    daemon._ordered_scopes = lambda _project, _state: [{
        "scope_id": scope_id,
        "scope_type": "task",
        "deliverable_id": "",
        "task_id": task_id,
    }]
    daemon._drive_scope = lambda _project, _scope: {
        "status": "completion_tick",
        "receipts": [],
    }
    daemon._save_state = lambda _project, _state: None
    daemon.tick_project("switchboard")
    tick_activity = next(
        payload for kind, _actor, payload, _kwargs in activity_store.activities
        if kind == "coordinator.daemon.tick"
    )
    ok(tick_activity.get("scope_ids") == [scope_id]
       and tick_activity.get("deliverable_ids") == [""],
       "compact standalone-task receipt remains valid daemon activity")

finally:
    shutil.rmtree(tmp, ignore_errors=True)


print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
