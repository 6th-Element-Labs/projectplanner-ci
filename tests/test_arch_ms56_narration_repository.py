#!/usr/bin/env python3
"""ARCH-MS-56: narration persistence under storage/repositories."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms56-narration-")
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


try:
    for name in (
        "switchboard.storage.repositories.narration",
        "narration_store",
    ):
        try:
            importlib.import_module(name)
            ok(True, f"{name} imports cleanly")
        except Exception as exc:  # noqa: BLE001
            ok(False, f"{name} import failed: {exc!r}")

    ok((ROOT / "src/switchboard/storage/repositories/narration.py").is_file(),
       "narration.py exists under storage/repositories")
    ok((ROOT / "narration_store.py").is_file(),
       "narration_store.py shim exists at repo root")

    from switchboard.storage.repositories import narration as narr_repo  # noqa: E402
    from switchboard.storage.repositories import tasks as tasks_repo  # noqa: E402
    import narration_store  # noqa: E402
    import store  # noqa: E402

    ok(narration_store.enqueue_narration is narr_repo.enqueue_narration,
       "narration_store shim re-exports enqueue_narration")
    ok(store.enqueue_narration is narr_repo.enqueue_narration,
       "store facade delegates enqueue_narration")
    ok(store.set_task_narration is narr_repo.set_task_narration,
       "store facade delegates set_task_narration")
    ok(store.get_task_narration is narr_repo.get_task_narration,
       "store facade delegates get_task_narration")
    ok(store.list_pending_narrations is narr_repo.list_pending_narrations,
       "store facade delegates list_pending_narrations")
    ok(store.clear_pending_narration is narr_repo.clear_pending_narration,
       "store facade delegates clear_pending_narration")
    ok(store.task_narration_fingerprint is narr_repo.task_narration_fingerprint,
       "store facade delegates task_narration_fingerprint")
    ok(store._narration_state is narr_repo._narration_state,
       "store facade delegates _narration_state")
    ok(store.enqueue_narration.__module__
       == "switchboard.storage.repositories.narration",
       "enqueue_narration lives under switchboard.storage.repositories.narration")
    ok(isinstance(store.narration_repository, narr_repo.StoreNarrationRepository),
       "store.narration_repository is StoreNarrationRepository")

    shell_src = (ROOT / "src/switchboard/storage/repositories/shell.py").read_text()
    narr_src = (ROOT / "src/switchboard/storage/repositories/narration.py").read_text()
    tasks_src = (ROOT / "src/switchboard/storage/repositories/tasks.py").read_text()
    for name in (
        "task_narration_fingerprint",
        "_narration_state",
        "set_task_narration",
        "get_task_narration",
        "enqueue_narration",
        "list_pending_narrations",
        "clear_pending_narration",
        "_max_activity_cursor",
    ):
        ok(f"def {name}(" not in shell_src,
           f"shell residual no longer defines {name}")
    ok("def enqueue_narration(" in narr_src
       and "INSERT OR REPLACE INTO pending_narrations" in narr_src
       and "INSERT OR REPLACE INTO task_narrations" in narr_src,
       "narration SQL helpers live in narration.py")
    ok("from switchboard.storage.repositories.narration import" in tasks_src
       and "_store_facade()._narration_state" not in tasks_src
       and "s.enqueue_narration(" not in tasks_src,
       "tasks imports narration helpers directly (no shell/store enqueue)")

    shell_lines = shell_src.count("\n") + 1
    ok(shell_lines <= 2817 - 70,
       f"shell residual shrank meaningfully ({shell_lines} <= {2817 - 70})")

    store.init_db("switchboard")
    created = store.create_task({
        "title": "narration smoke",
        "status": "Not Started",
        "workstream_id": "ARCH-MS",
        "depends_on": [],
    }, actor="cursor/test", project="switchboard")
    tid = created["task_id"]
    pending = store.list_pending_narrations(project="switchboard")
    ok(any(p.get("task_id") == tid for p in pending),
       "create_task enqueues pending narration via extracted helper")
    store.set_task_narration(
        tid, "CEO voice smoke", activity_cursor=1,
        source_fingerprint="abc123", model="test", project="switchboard")
    row = store.get_task_narration(tid, project="switchboard")
    ok(row and row.get("narration") == "CEO voice smoke",
       "extracted set/get_task_narration round-trip")
    store.clear_pending_narration(tid, project="switchboard")
    ok(not any(p.get("task_id") == tid
               for p in store.list_pending_narrations(project="switchboard")),
       "extracted clear_pending_narration removes queue row")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
