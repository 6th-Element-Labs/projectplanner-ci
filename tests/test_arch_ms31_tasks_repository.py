#!/usr/bin/env python3
"""ARCH-MS-31: task persistence under switchboard.storage.repositories.tasks."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms31-tasks-")
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
    "switchboard.storage.repositories.tasks",
    "tasks_store",
):
    try:
        importlib.import_module(name)
        ok(True, f"{name} imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"{name} import failed: {exc!r}")

ok((ROOT / "src/switchboard/storage/repositories/tasks.py").is_file(),
   "tasks.py exists under storage/repositories")
ok((ROOT / "tasks_store.py").is_file(),
   "tasks_store.py shim exists at repo root")

from switchboard.storage.repositories import tasks as tasks_repo  # noqa: E402
import tasks_store  # noqa: E402
import store  # noqa: E402

ok(tasks_store.create_task is tasks_repo.create_task,
   "tasks_store shim re-exports package create_task")
ok(tasks_store.get_task is tasks_repo.get_task,
   "tasks_store shim re-exports package get_task")
ok(store.create_task is tasks_repo.create_task,
   "store facade delegates create_task to package module")
ok(store.get_task is tasks_repo.get_task,
   "store facade delegates get_task to package module")
ok(store.board_payload is tasks_repo.board_payload,
   "store facade delegates board_payload to package module")
ok(store.move_task is tasks_repo.move_task,
   "store facade delegates move_task to package module")
ok(store.create_task.__module__ == "switchboard.storage.repositories.tasks",
   "create_task lives under switchboard.storage.repositories.tasks")
ok(isinstance(store.task_repository, tasks_repo.StoreTaskRepository),
   "store.task_repository is StoreTaskRepository")

try:
    store.init_project_registry()
    store.init_db("switchboard")
    created = store.create_task(
        {"workstream_id": "ARCH-MS", "title": "ms31 proof task",
         "description": "task repository extract"},
        actor="arch-ms31",
        project="switchboard",
    )
    ok(bool(created and created.get("task_id")),
       "create_task persists a task")
    tid = created["task_id"]
    fetched = store.get_task(tid, project="switchboard")
    ok(fetched is not None and fetched.get("title") == "ms31 proof task",
       "get_task reads persisted task")
    ok("provenance" in (fetched or {}) and "dependency_state" in (fetched or {}),
       "get_task still enriches provenance and dependency_state")
    listed = store.list_tasks_slim(workstream="ARCH-MS", project="switchboard")
    ok(any(t.get("task_id") == tid for t in listed),
       "list_tasks_slim returns the created task")
    rollups = store.board_rollups(project="switchboard", tasks=listed)
    ok(rollups.get("total_tasks", 0) >= 1 and "status_counts" in rollups,
       "board_rollups computes counts from task rows")
    updated = store.update_task(
        tid, {"description": "updated by ms31"}, actor="arch-ms31",
        project="switchboard")
    ok(updated is not None and updated.get("description") == "updated by ms31",
       "update_task mutates and rehydrates task detail")
    via_repo = store.task_repository.get_task(tid, project="switchboard")
    ok(via_repo is not None and via_repo.get("task_id") == tid,
       "StoreTaskRepository.get_task uses package SQL")

    # PERF-2 / write-retry compatibility: callers (and tests) monkeypatch store._*_impl.
    calls = []
    real_impl = store._create_task_impl
    real_get = store.get_task

    def patched_impl(*_args, **_kwargs):
        calls.append(1)
        return "ARCH-MS-PATCHED-1"

    store._create_task_impl = patched_impl
    store.get_task = lambda task_id, project=None: {"task_id": task_id}
    try:
        patched = store.create_task(
            {"workstream_id": "ARCH-MS", "title": "patch probe"},
            actor="arch-ms31", project="switchboard")
        ok(len(calls) == 1 and patched and patched.get("task_id") == "ARCH-MS-PATCHED-1",
           "create_task honors store._create_task_impl monkeypatches via facade")
    finally:
        store._create_task_impl = real_impl
        store.get_task = real_get
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nARCH-MS-31 tasks repository: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
