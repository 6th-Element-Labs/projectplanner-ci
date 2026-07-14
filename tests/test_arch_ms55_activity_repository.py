#!/usr/bin/env python3
"""ARCH-MS-55: activity stream + meta KV under storage/repositories."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms55-activity-")
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
        "switchboard.storage.repositories.activity",
        "activity_store",
    ):
        try:
            importlib.import_module(name)
            ok(True, f"{name} imports cleanly")
        except Exception as exc:  # noqa: BLE001
            ok(False, f"{name} import failed: {exc!r}")

    ok((ROOT / "src/switchboard/storage/repositories/activity.py").is_file(),
       "activity.py exists under storage/repositories")
    ok((ROOT / "activity_store.py").is_file(),
       "activity_store.py shim exists at repo root")

    from switchboard.storage.repositories import activity as act_repo  # noqa: E402
    from switchboard.storage.repositories import projects as projects_repo  # noqa: E402
    from switchboard.storage.repositories import work_sessions as ws_repo  # noqa: E402
    import activity_store  # noqa: E402
    import store  # noqa: E402

    ok(activity_store.append_activity is act_repo.append_activity,
       "activity_store shim re-exports append_activity")
    ok(store.append_activity is act_repo.append_activity,
       "store facade delegates append_activity to package module")
    ok(store.get_meta is act_repo.get_meta,
       "store facade delegates get_meta")
    ok(store.set_meta is act_repo.set_meta,
       "store facade delegates set_meta")
    ok(store.get_activity_delta is act_repo.get_activity_delta,
       "store facade delegates get_activity_delta")
    ok(store.activity_since is act_repo.activity_since,
       "store facade delegates activity_since")
    ok(store._activity_cursor is act_repo._activity_cursor,
       "store facade delegates _activity_cursor")
    ok(store.get_contacts is act_repo.get_contacts,
       "store facade delegates get_contacts")
    ok(store.append_activity.__module__
       == "switchboard.storage.repositories.activity",
       "append_activity lives under switchboard.storage.repositories.activity")
    ok(isinstance(store.activity_repository, act_repo.StoreActivityRepository),
       "store.activity_repository is StoreActivityRepository")

    shell_src = (ROOT / "src/switchboard/storage/repositories/shell.py").read_text()
    act_src = (ROOT / "src/switchboard/storage/repositories/activity.py").read_text()
    projects_src = (ROOT / "src/switchboard/storage/repositories/projects.py").read_text()
    ws_src = (ROOT / "src/switchboard/storage/repositories/work_sessions.py").read_text()
    ok("def append_activity(" not in shell_src,
       "shell residual no longer defines append_activity")
    ok("def get_activity_delta(" not in shell_src,
       "shell residual no longer defines get_activity_delta")
    ok("def activity_since(" not in shell_src,
       "shell residual no longer defines activity_since")
    ok("def _activity_cursor(" not in shell_src,
       "shell residual no longer defines _activity_cursor")
    ok("def get_meta(" not in shell_src,
       "shell residual no longer defines get_meta")
    ok("def set_meta(" not in shell_src,
       "shell residual no longer defines set_meta")
    ok("def get_contacts(" not in shell_src,
       "shell residual no longer defines get_contacts")
    ok("_SEED_CONTACTS =" not in shell_src,
       "shell residual no longer owns _SEED_CONTACTS")
    ok("def append_activity(" in act_src
       and "INSERT INTO activity" in act_src
       and "def get_meta(" in act_src,
       "activity/meta SQL helpers live in activity.py")
    ok("from switchboard.storage.repositories.activity import" in projects_src
       and "_store_facade().get_meta" not in projects_src
       and "_store_facade().append_activity" not in projects_src,
       "projects imports activity helpers directly")
    ok("from switchboard.storage.repositories.activity import" in ws_src
       and "_store_facade().append_activity" not in ws_src,
       "work_sessions imports append_activity directly")

    shell_lines = shell_src.count("\n") + 1
    ok(shell_lines <= 2924 - 80,
       f"shell residual shrank by >=80 lines ({shell_lines} <= {2924 - 80})")

    store.init_db("switchboard")
    row_id = store.append_activity(
        "arch_ms55.smoke", "cursor/test", {"ok": True},
        task_id="ARCH-MS-55", project="switchboard")
    ok(isinstance(row_id, int) and row_id > 0,
       "extracted append_activity inserts a row")
    store.set_meta("arch_ms55_probe", {"v": 1}, project="switchboard")
    ok(store.get_meta("arch_ms55_probe", project="switchboard") == {"v": 1},
       "extracted get_meta/set_meta round-trip")
    cursor = store._activity_cursor(project="switchboard")
    ok(cursor >= row_id,
       "extracted _activity_cursor tracks activity ids")
    # get_activity_delta joins tasks; seed a task (id assigned by create_task).
    created = store.create_task({
        "title": "activity smoke",
        "status": "In Progress",
        "workstream_id": "ARCH-MS",
        "depends_on": [],
    }, actor="cursor/test", project="switchboard")
    smoke_id = created["task_id"]
    delta = store.get_activity_delta(since_cursor=0, project="switchboard")
    ok(any(u.get("task_id") == smoke_id for u in delta.get("updates", [])),
       "extracted get_activity_delta returns inserted task update")
    # activity_since uses the default project connection (verbatim move).
    store.init_db()
    store.append_activity("arch_ms55.default", "cursor/test", {"ok": True},
                          task_id="ARCH-MS-55")
    since = store.activity_since(0)
    ok(any(r.get("kind") == "arch_ms55.default" for r in since),
       "extracted activity_since returns inserted event on default project")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
