#!/usr/bin/env python3
"""COORD-127: every task scope has a v4 inbox and legacy gaps self-repair."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


TMP = Path(tempfile.mkdtemp(prefix="coord127-mission-bootstrap-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)

from path_setup import ROOT as _ROOT  # noqa: E402,F401

import store  # noqa: E402
from switchboard.application.commands import connect_dispatch, task_execution  # noqa: E402
from switchboard.application.mission_bot_v4 import run_scoped_mission_tick  # noqa: E402
from switchboard.storage.repositories import autopilot_scopes  # noqa: E402
from switchboard.storage.repositories.mission_journal import (  # noqa: E402
    default_mission_journal_repository as journal,
)


PROJECT = "switchboard"
HEAD = "c" * 40
COORDINATOR = "switchboard/scoped-owner/coord127"


try:
    store.init_project_registry()
    store.init_db(PROJECT)
    task = store.create_task(
        {
            "workstream_id": "COORD",
            "title": "Task Execution must bootstrap its v4 mission",
            "status": "Not Started",
            "ui_impact": "no",
        },
        actor="coord127-test",
        project=PROJECT,
    )
    task_id = task["task_id"]

    dispatches: list[dict] = []
    original_enqueue = connect_dispatch.enqueue_task
    connect_dispatch.enqueue_task = lambda *_args, **kwargs: (
        dispatches.append(dict(kwargs))
        or {
            "dispatched": True,
            "started": True,
            "action": "started",
            "wake_id": f"wake-{len(dispatches)}",
        }
    )
    try:
        # Exercise the real Task Execution command with only the external
        # Capacity adapter replaced.  In particular, do not call the scope
        # repository or mission command directly in this setup.
        started = task_execution.start_task(
            task_id,
            project=PROJECT,
            actor="coord127-test",
            runtime="codex",
        )
        assert started["action"] == "started", started
        scopes = autopilot_scopes.list_autopilot_scopes(
            project=PROJECT, status="active", limit=50,
        )
        scope = next(row for row in scopes if row.get("task_id") == task_id)
        created_mission = journal.get_item(task_id, project=PROJECT)
        assert created_mission is not None
        assert created_mission["requested_role"] == "implementation"
        assert int(created_mission["latest_sequence"]) >= 1

        # Reproduce the deployed pre-cutover shape: the explicitly started W2
        # scope survives, but its v4 journal rows are absent and the task has
        # since acquired a persisted PR.  We deliberately leave board status
        # alone here: C3's In Review transition requires a real host-stop
        # receipt, which is a separate protocol and must not be forged by this
        # scope-bootstrap test.
        with store._conn(PROJECT) as connection:
            connection.execute(
                "DELETE FROM mission_events WHERE project_id=? AND task_id=?",
                (PROJECT, task_id),
            )
            connection.execute(
                "DELETE FROM mission_items WHERE project_id=? AND task_id=?",
                (PROJECT, task_id),
            )
            connection.execute(
                "INSERT INTO task_git_state("
                "task_id,branch,head_sha,pushed_at,pr_number,pr_url,"
                "in_main_content,evidence_json,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,strftime('%s','now'))",
                (
                    task_id,
                    f"codex/{task_id}-proof",
                    HEAD,
                    1.0,
                    127,
                    "https://github.com/example/projectplanner/pull/127",
                    0,
                    "{}",
                ),
            )
        store.register_agent(
            COORDINATOR,
            "scoped-completion-coordinator",
            lane="COORD",
            ttl_s=120,
            actor="coord127-test",
            project=PROJECT,
        )
        authority = autopilot_scopes.acquire_autopilot_scope_lease(
            scope["scope_id"],
            holder_agent_id=COORDINATOR,
            project=PROJECT,
            ttl_seconds=120,
        )
        assert not authority.get("error"), authority

        stale_authority = {
            **authority,
            "fence_epoch": int(authority["fence_epoch"]) - 1,
        }
        refused = run_scoped_mission_tick(
            task_id,
            project=PROJECT,
            scope_project=PROJECT,
            scope_authority=stale_authority,
            actor="coord127-test",
            agent_id=COORDINATOR,
            store_mod=store,
        )
        assert refused["action"] == "wait", refused
        assert refused["reason"] == "scope_authority_denied", refused
        assert journal.get_item(task_id, project=PROJECT) is None

        # This is the real v4 runtime and worker.  It must repair the missing
        # inert inbox under the exact scope fence, then request review through
        # Task Execution's normal start_task/Connect door.
        tick = run_scoped_mission_tick(
            task_id,
            project=PROJECT,
            scope_project=PROJECT,
            scope_authority=authority,
            actor="coord127-test",
            agent_id=COORDINATOR,
            store_mod=store,
        )
        assert tick["action"] == "start_task", tick
        assert tick["mission_bootstrap"]["repaired"] is True
        assert tick["mission_bootstrap"]["scope_id"] == scope["scope_id"]
        assert tick["mission_bootstrap"]["scope_generation"] == authority["generation"]
        assert tick["mission_bootstrap"]["scope_fence"] == authority["fence_epoch"]
        repaired_mission = journal.get_item(task_id, project=PROJECT)
        assert repaired_mission["requested_role"] == "review_merge"
        assert dispatches[-1]["role"] == "review_merge"
        assert dispatches[-1]["source_sha"] == HEAD
    finally:
        connect_dispatch.enqueue_task = original_enqueue
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print("PASS: Task Execution creates the v4 inbox before its task scope")
print("PASS: a fenced v4 tick repairs legacy mission_not_found and starts exact-head review")
