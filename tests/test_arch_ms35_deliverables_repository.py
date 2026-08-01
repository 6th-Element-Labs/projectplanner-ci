#!/usr/bin/env python3
"""ARCH-MS-35: deliverables under switchboard.storage.repositories.deliverables."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms35-deliv-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


for name in (
    "switchboard.storage.repositories.deliverables",
    "deliverables_store",
):
    try:
        importlib.import_module(name)
        ok(True, f"{name} imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"{name} import failed: {exc!r}")

ok((ROOT / "src/switchboard/storage/repositories/deliverables.py").is_file(),
   "deliverables.py exists under storage/repositories")
ok((ROOT / "deliverables_store.py").is_file(),
   "deliverables_store.py shim exists at repo root")

from switchboard.storage.repositories import deliverables as deliv_repo  # noqa: E402
import deliverables_store  # noqa: E402
import store  # noqa: E402

ok(deliverables_store.create_deliverable is deliv_repo.create_deliverable,
   "deliverables_store shim re-exports package create_deliverable")
ok(deliverables_store.get_mission_status is deliv_repo.get_mission_status,
   "deliverables_store shim re-exports package get_mission_status")
ok(store.create_deliverable is deliv_repo.create_deliverable,
   "store facade delegates create_deliverable to package module")
ok(store.get_deliverable is deliv_repo.get_deliverable,
   "store facade delegates get_deliverable to package module")
ok(store.list_deliverables is deliv_repo.list_deliverables,
   "store facade delegates list_deliverables to package module")
ok(store.get_mission_status is deliv_repo.get_mission_status,
   "store facade delegates get_mission_status to package module")
ok(store.deliverable_tally is deliv_repo.deliverable_tally,
   "store facade delegates deliverable_tally to package module")
ok(store.list_task_deliverable_links is deliv_repo.list_task_deliverable_links,
   "store facade delegates list_task_deliverable_links to package module")
ok(store.create_deliverable.__module__
   == "switchboard.storage.repositories.deliverables",
   "create_deliverable lives under switchboard.storage.repositories.deliverables")
ok(isinstance(store.deliverables_repository, deliv_repo.StoreDeliverablesRepository),
   "store.deliverables_repository is StoreDeliverablesRepository")

rollup = deliv_repo._mission_milestone_rollup_status
mid = "acceptance"
ok(rollup([{"milestone_id": mid, "task_detail": {
    "status": "Done", "provenance": {"terminal": True}}}], mid) == "done",
   "milestone rollup is done only when every linked task has terminal proof")
ok(rollup([{"milestone_id": mid, "task_detail": {
    "status": "Done", "provenance": {"terminal": False}}}], mid) == "in_review",
   "Done without terminal proof cannot render a milestone done")
ok(rollup([{"milestone_id": mid, "task_detail": {
    "status": "In Progress", "active_claims": [{"claim_id": "c"}]}}], mid) == "in_progress",
   "active linked work renders the milestone in progress")
ok(rollup([
    {"milestone_id": mid, "task_detail": {"status": "Done", "provenance": {"terminal": True}}},
    {"milestone_id": mid, "task_detail": {"status": "In Progress"}},
], mid) == "in_progress", "active work takes precedence over partial completed proof")
ok(rollup([{"milestone_id": mid, "task_detail": {"status": "Blocked"}}], mid) == "blocked",
   "a blocked linked task renders the milestone blocked")

try:
    store.init_project_registry()
    store.init_db("switchboard")
    board = store.create_project_board(
        {"id": "arch-ms35-board", "title": "ARCH-MS-35 board", "kind": "mission"},
        actor="arch-ms35",
        project="switchboard",
    )
    ok(bool(board and board.get("id") == "arch-ms35-board"),
       "create_project_board persists a board")

    created = store.create_deliverable(
        {
            "id": "arch-ms35-deliv",
            "title": "ARCH-MS-35 deliverable",
            "board_id": "arch-ms35-board",
        },
        actor="arch-ms35",
        project="switchboard",
    )
    ok(bool(created and created.get("id") == "arch-ms35-deliv"),
       "create_deliverable persists a deliverable")

    fetched = store.get_deliverable("arch-ms35-deliv", project="switchboard")
    ok(bool(fetched and fetched.get("title") == "ARCH-MS-35 deliverable"),
       "get_deliverable returns the created deliverable")

    listed = store.list_deliverables(project="switchboard")
    ok(any(d.get("id") == "arch-ms35-deliv" for d in (listed or [])),
       "list_deliverables includes the created deliverable")

    task = store.create_task(
        {"workstream_id": "ARCH-MS", "title": "ms35 deliverable link proof",
         "description": "deliverables repository extract"},
        actor="arch-ms35",
        project="switchboard",
    )
    ok(bool(task and task.get("task_id")),
       "create_task persists a task for deliverable link proof")
    tid = task["task_id"]

    linked = store.link_task_to_deliverable(
        "arch-ms35-deliv", "switchboard", tid, actor="arch-ms35",
        project="switchboard",
    )
    ok(bool(linked) and not linked.get("error"),
       "link_task_to_deliverable links task")

    task_links = store.list_task_deliverable_links(tid, project="switchboard")
    ok(any(l.get("deliverable_id") == "arch-ms35-deliv" for l in (task_links or [])),
       "list_task_deliverable_links returns the link")

    status = store.get_mission_status(
        project="switchboard", deliverable_id="arch-ms35-deliv")
    ok(isinstance(status, dict), "get_mission_status returns a dict")

    # PERF-style monkeypatch: store._create_deliverable_impl must be honored via facade
    calls = []
    real_impl = store._create_deliverable_impl

    def _spy(*args, **kwargs):
        calls.append(True)
        return real_impl(*args, **kwargs)

    store._create_deliverable_impl = _spy
    try:
        store.create_deliverable(
            {
                "id": "arch-ms35-deliv-2",
                "title": "second",
                "board_id": "arch-ms35-board",
            },
            actor="arch-ms35",
            project="switchboard",
        )
        ok(bool(calls), "store._create_deliverable_impl monkeypatch is honored")
    finally:
        store._create_deliverable_impl = real_impl

except Exception as exc:  # noqa: BLE001
    ok(False, f"runtime deliverable proof failed: {exc!r}")

print(f"\n{passed} passed, {failed} failed")
shutil.rmtree(TMP, ignore_errors=True)
raise SystemExit(1 if failed else 0)
