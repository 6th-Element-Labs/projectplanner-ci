#!/usr/bin/env python3
"""ARCH-MS-46: work sessions under switchboard.storage.repositories.work_sessions."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms46-ws-")
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
    "switchboard.storage.repositories.work_sessions",
    "work_sessions_store",
):
    try:
        importlib.import_module(name)
        ok(True, f"{name} imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"{name} import failed: {exc!r}")

ok((ROOT / "src/switchboard/storage/repositories/work_sessions.py").is_file(),
   "work_sessions.py exists under storage/repositories")
ok((ROOT / "work_sessions_store.py").is_file(),
   "work_sessions_store.py shim exists at repo root")

from switchboard.storage.repositories import work_sessions as ws_repo  # noqa: E402
import work_sessions_store  # noqa: E402
import store  # noqa: E402

ok(work_sessions_store.create_work_session is ws_repo.create_work_session,
   "work_sessions_store shim re-exports package create_work_session")
ok(work_sessions_store.list_session_health is ws_repo.list_session_health,
   "work_sessions_store shim re-exports package list_session_health")
ok(store.create_work_session is ws_repo.create_work_session,
   "store facade delegates create_work_session to package module")
ok(store.get_work_session is ws_repo.get_work_session,
   "store facade delegates get_work_session to package module")
ok(store.list_work_sessions is ws_repo.list_work_sessions,
   "store facade delegates list_work_sessions to package module")
ok(store.update_work_session is ws_repo.update_work_session,
   "store facade delegates update_work_session to package module")
ok(store.list_session_health is ws_repo.list_session_health,
   "store facade delegates list_session_health to package module")
ok(store.preflight_work_session is ws_repo.preflight_work_session,
   "store facade delegates preflight_work_session to package module")
ok(store.create_work_session.__module__
   == "switchboard.storage.repositories.work_sessions",
   "create_work_session lives under switchboard.storage.repositories.work_sessions")
ok(isinstance(store.work_sessions_repository, ws_repo.StoreWorkSessionsRepository),
   "store.work_sessions_repository is StoreWorkSessionsRepository")

shell_src = (ROOT / "src/switchboard/storage/repositories/shell.py").read_text()
ws_src = (ROOT / "src/switchboard/storage/repositories/work_sessions.py").read_text()
ok("def create_work_session(" not in shell_src,
   "shell residual no longer defines create_work_session")
ok("def list_session_health(" not in shell_src,
   "shell residual no longer defines list_session_health")
ok("def create_work_session(" in ws_src,
   "work_sessions repository owns create_work_session")
ok(len(ws_src.splitlines()) > 500,
   "work_sessions repository holds a substantial verbatim extract")

try:
    store.init_project_registry()
    store.init_db("switchboard")
    created_task = store.create_task(
        {"workstream_id": "ARCH-MS", "title": "ms46 work session proof",
         "description": "work_sessions repository extract"},
        actor="arch-ms46",
        project="switchboard",
    )
    ok(bool(created_task and created_task.get("task_id")),
       "create_task persists a task for work-session proof")
    tid = created_task["task_id"]
    agent = "cursor/arch-ms46-proof"
    created = store.create_work_session(
        {
            "schema": "switchboard.work_session.v1",
            "agent_id": agent,
            "task_id": tid,
            "project_id": "switchboard",
            "repo_role": "canonical",
            "branch": "cursor/ARCH-MS-46-work-sessions",
            "storage_mode": "external",
            "dirty_status": "clean",
            "status": "active",
            "workspace_path": str(ROOT),
        },
        actor="arch-ms46",
        project="switchboard",
    )
    ok(bool(created and (created.get("created") or created.get("work_session"))),
       f"create_work_session succeeds via package SQL ({created.get('error')})")
    session = created.get("work_session") or created
    wid = session.get("work_session_id")
    ok(bool(wid), "created work session has work_session_id")
    fetched = store.get_work_session(wid, project="switchboard")
    ok(bool(fetched and fetched.get("work_session_id") == wid),
       "get_work_session reads the row via package SQL")
    listed = store.list_work_sessions(project="switchboard", task_id=tid)
    ok(any(s.get("work_session_id") == wid for s in (listed or [])),
       "list_work_sessions includes the created session")
    health = store.list_session_health(project="switchboard", task_id=tid)
    ok(bool(health and health.get("schema") == "switchboard.session_health_list.v1"),
       "list_session_health returns the health list schema")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nARCH-MS-46 work_sessions repository: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
