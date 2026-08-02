#!/usr/bin/env python3
"""BUG-272: canonical Done atomically closes an existing v4 mission."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = tempfile.mkdtemp(prefix="bug272-terminal-mission-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from switchboard.application.commands import mission_journal  # noqa: E402
from switchboard.application.mission_bot_v4.runtime import (  # noqa: E402
    project_terminal_provenance,
    run_scoped_mission_tick,
)
from switchboard.storage.repositories.mission_journal import (  # noqa: E402
    default_mission_journal_repository,
)


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def create_task(title):
    return store.create_task(
        {
            "workstream_id": "BUG",
            "title": title,
            "description": "Prove canonical merge terminal projection.",
            "ui_impact": "no",
        },
        actor="bug272-test",
        project="switchboard",
    )


def merge(task_id, pr_number, merged_sha):
    head_sha = f"{pr_number:040d}"[-40:]
    store.mark_task_pr_opened(
        task_id,
        pr_number=pr_number,
        pr_url=f"https://github.com/6th-Element-Labs/projectplanner/pull/{pr_number}",
        branch=f"codex/{task_id.lower()}",
        head_sha=head_sha,
        actor="bug272-test",
        project="switchboard",
    )
    return store.mark_task_merged(
        task_id,
        merged_sha=merged_sha,
        pr_number=pr_number,
        pr_url=f"https://github.com/6th-Element-Labs/projectplanner/pull/{pr_number}",
        branch=f"codex/{task_id.lower()}",
        head_sha=head_sha,
        actor="bug272-test",
        project="switchboard",
    )


class RuntimeStore:
    @staticmethod
    def validate_autopilot_scope_authority(_authority, **_kwargs):
        return {"allowed": True}

    @staticmethod
    def get_task(task_id, **kwargs):
        return store.get_task(task_id, project=kwargs.get("project") or "switchboard")

    @staticmethod
    def task_has_live_execution(_task_id, **_kwargs):
        return False

    @staticmethod
    def list_wake_intents(**_kwargs):
        return []

    @staticmethod
    def list_runner_sessions(**_kwargs):
        return []


try:
    store.init_project_registry()
    store.init_db("switchboard")

    task = create_task("Close live mission with merge provenance")
    task_id = task["task_id"]
    mission_journal.create_mission(task_id, project="switchboard")
    merged_sha = "a" * 40
    result = merge(task_id, 1272, merged_sha)
    before = default_mission_journal_repository.get_item(
        task_id, project="switchboard",
    )
    tick = run_scoped_mission_tick(
        task_id,
        project="switchboard",
        scope_project="switchboard",
        scope_authority={
            "schema": "switchboard.autopilot_scope_authority.v1",
            "scope_id": "bug272-scope",
        },
        actor="bug272-v4-runtime",
        agent_id="bug272-v4-runtime",
        store_mod=RuntimeStore,
    )
    projection = tick.get("terminal_projection") or {}
    item = default_mission_journal_repository.get_item(
        task_id, project="switchboard",
    )
    events = default_mission_journal_repository.list_events(
        task_id, project="switchboard", after_sequence=0, limit=100,
    )
    terminal = [
        row for row in events
        if row.get("event_type") == "terminal_provenance_persisted"
    ]
    ok(result.get("status") == "Done", "canonical provenance remains Done authority")
    ok(before.get("state") == "ACTIVE", "canonical provenance remains uncoupled until projection")
    ok(
        tick.get("action") == "wait" and tick.get("reason") == "terminal_provenance",
        "opt-in v4 tick observes terminal provenance and never pages a runner",
    )
    ok(projection.get("projected") is True, "staged v4 runtime projects canonical Done")
    ok(item.get("state") == "DONE", "existing v4 mission closes as DONE")
    ok(
        item.get("terminal_kind") == "github_merge"
        and item.get("terminal_ref") == merged_sha,
        "mission stores the exact canonical merge identity",
    )
    ok(
        len(terminal) == 1
        and terminal[0].get("source_plane") == "coordination",
        "one typed coordination terminal fact is appended",
    )
    replay = project_terminal_provenance(
        task_id,
        project="switchboard",
        actor="bug272-v4-runtime-replay",
        task_reader=store.get_task,
    )
    replay_events = default_mission_journal_repository.list_events(
        task_id, project="switchboard", after_sequence=0, limit=100,
    )
    ok(replay.get("projected") is True, "same terminal projection is idempotent")
    ok(
        replay.get("receipt", {}).get("event_created") is False,
        "projection replay reads back the existing terminal fact",
    )
    ok(
        sum(
            row.get("event_type") == "terminal_provenance_persisted"
            for row in replay_events
        ) == 1,
        "merge replay does not duplicate the terminal fact",
    )

    sibling = create_task("Close a second task from the same canonical merge")
    sibling_id = sibling["task_id"]
    mission_journal.create_mission(sibling_id, project="switchboard")
    merge(sibling_id, 1272, merged_sha)
    sibling_projection = project_terminal_provenance(
        sibling_id,
        project="switchboard",
        actor="bug272-v4-runtime",
        task_reader=store.get_task,
    )
    sibling_item = default_mission_journal_repository.get_item(
        sibling_id, project="switchboard",
    )
    ok(
        sibling_projection.get("projected") is True
        and sibling_item.get("state") == "DONE",
        "tasks sharing one merge SHA receive independent terminal events",
    )

    stale_task = create_task("Repair stale mission on idempotent merge reconcile")
    stale_id = stale_task["task_id"]
    stale_sha = "b" * 40
    merge(stale_id, 1273, stale_sha)
    missing = project_terminal_provenance(
        stale_id,
        project="switchboard",
        actor="bug272-v4-runtime",
        task_reader=store.get_task,
    )
    ok(
        missing.get("release_blocked") is True
        and missing.get("reason") == "mission_not_found",
        "terminal projection fails closed and never creates a missing mission",
    )
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nBUG-272 terminal mission projection: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
