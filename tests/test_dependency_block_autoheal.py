#!/usr/bin/env python3
"""Dependency-only Blocked tasks return to Autopilot when their graph is ready."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = tempfile.mkdtemp(prefix="dependency-block-autoheal-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def task(title, *, status="Not Started", depends_on=None):
    return store.create_task(
        {
            "workstream_id": "AUTOHEAL",
            "title": title,
            "description": "Exercise dependency lifecycle truth and autonomous dispatch.",
            "entry_criteria": "The dependency graph is recorded.",
            "exit_criteria": "The lifecycle transition is persisted and audited.",
            "status": status,
            "depends_on": depends_on or [],
        },
        actor="dependency-autoheal-test",
        project="switchboard",
    )


def merge(task_id, number):
    store.mark_task_pr_opened(
        task_id,
        pr_number=number,
        pr_url=f"https://github.com/example/projectplanner/pull/{number}",
        branch=f"codex/{task_id.lower()}",
        head_sha=f"{number:040d}"[-40:],
        actor="dependency-autoheal-test",
        project="switchboard",
    )
    return store.mark_task_merged(
        task_id,
        merged_sha=f"{number + 1:040d}"[-40:],
        pr_number=number,
        pr_url=f"https://github.com/example/projectplanner/pull/{number}",
        actor="dependency-autoheal-test",
        project="switchboard",
    )


try:
    store.init_project_registry()
    store.init_db("switchboard")

    dependency = task("Dependency closes")
    successor = task(
        "Legacy dependency-only blocked successor",
        status="Blocked",
        depends_on=[dependency["task_id"]],
    )
    ok(store.get_task(successor["task_id"], project="switchboard")["status"] == "Blocked",
       "successor starts Blocked while dependency is open")

    merged = merge(dependency["task_id"], 701)
    healed = store.get_task(successor["task_id"], project="switchboard")
    ok(merged.get("status") == "Done", "dependency closes with proven Done status")
    ok(healed.get("status") == "Not Started",
       "final dependency completion auto-heals successor to Not Started")
    ok((healed.get("dependency_state") or {}).get("ready") is True,
       "healed successor is ready according to dependency truth")
    events = [row for row in healed.get("activity") or []
              if row.get("kind") == "task.dependency_status_healed"]
    ok(len(events) == 1
       and (events[0].get("payload") or {}).get("completed_task_id") == dependency["task_id"],
       "auto-heal writes one auditable dependency lifecycle event")

    claimed = store.claim_task(
        successor["task_id"], "codex/dependency-autoheal", actor="dependency-autoheal-test",
        project="switchboard")
    ok(claimed.get("claimed") is True,
       "Autopilot exact-claim path can immediately claim the healed successor")

    held_dependency = task("Dependency for explicit hold")
    explicit_hold = task(
        "Explicit operator hold", depends_on=[held_dependency["task_id"]])
    store.update_task(
        explicit_hold["task_id"], {"status": "Blocked"},
        actor="operator", project="switchboard")
    store.update_task(
        explicit_hold["task_id"], {"title": "Explicit operator hold, clarified"},
        actor="operator", project="switchboard")
    merge(held_dependency["task_id"], 711)
    held = store.get_task(explicit_hold["task_id"], project="switchboard")
    ok(held.get("status") == "Blocked",
       "status-only operator Blocked edit is preserved after dependencies close")

    legacy_dependency = task("Already completed dependency")
    merge(legacy_dependency["task_id"], 721)
    stale = task(
        "Stale row found by mission polling",
        depends_on=[legacy_dependency["task_id"]],
    )
    # Reproduce a legacy row from before create/edit activity carried block-cause
    # provenance: literal Blocked survived even though dependency truth was ready.
    with store._conn("switchboard") as connection:
        connection.execute(
            "UPDATE tasks SET status='Blocked', updated_at=? WHERE task_id=?",
            (time.time(), stale["task_id"]),
        )
    links = store._batch_enrich_mission_links([{
        "project_id": "switchboard",
        "task_id": stale["task_id"],
        "blocks_deliverable": True,
        "metadata": {},
        "role": "implementation",
    }])
    detail = (links[0] if links else {}).get("task_detail") or {}
    ok(detail.get("status") == "Not Started"
       and (detail.get("dependency_state") or {}).get("ready") is True,
       "mission cockpit repairs a pre-existing stale row before planning actions")
    actions = store._mission_next_actions({}, links, None)
    ok(any(action.get("action") == "claim_task"
           and action.get("task_id") == stale["task_id"] for action in actions),
       "Autopilot mission projection selects the repaired task")

    created_hold = task(
        "Explicit block created after dependencies completed",
        status="Blocked",
        depends_on=[legacy_dependency["task_id"]],
    )
    store.heal_dependency_blocked_tasks(project="switchboard")
    ok(store.get_task(created_hold["task_id"], project="switchboard")["status"] == "Blocked",
       "new explicit Blocked creation is preserved with durable block-cause metadata")

    semantic = task(
        "Semantic exception remains blocked",
        status="Blocked",
        depends_on=[legacy_dependency["task_id"]],
    )
    with store._conn("switchboard") as connection:
        connection.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (semantic["task_id"], "test", "git.pr_merged_semantic_blocked",
             json.dumps({"semantic_gate": {"ok": False}}), time.time()),
        )
    store.heal_dependency_blocked_tasks(project="switchboard")
    ok(store.get_task(semantic["task_id"], project="switchboard")["status"] == "Blocked",
       "semantic exception Blocked status is never dependency-auto-healed")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
