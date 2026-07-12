#!/usr/bin/env python3
"""ARCH-MS-27: storage repository Protocols + store implementations proof gate."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from path_setup import ROOT

TMP = tempfile.mkdtemp(prefix="arch-ms27-storage-")
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


# --- package skeleton --------------------------------------------------------
for name in (
    "switchboard.storage",
    "switchboard.storage.repositories",
    "switchboard.storage.repositories.protocols",
    "switchboard.storage.repositories.protocols.tasks",
    "switchboard.storage.repositories.protocols.access",
    "switchboard.storage.repositories.tasks",
    "switchboard.storage.repositories.access",
):
    try:
        importlib.import_module(name)
        ok(True, f"{name} imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"{name} import failed: {exc!r}")

for subpath in (
    "src/switchboard/storage/repositories/protocols/__init__.py",
    "src/switchboard/storage/repositories/protocols/tasks.py",
    "src/switchboard/storage/repositories/protocols/access.py",
    "src/switchboard/storage/repositories/tasks.py",
):
    ok((ROOT / subpath).is_file(), f"{subpath} exists on disk")

from switchboard.storage.repositories.protocols import (  # noqa: E402
    AccessRepository,
    TaskRepository,
)
from switchboard.storage.repositories.access import (  # noqa: E402
    AccessStoreRepository,
    default_access_repository,
)
from switchboard.storage.repositories.tasks import (  # noqa: E402
    StoreTaskRepository,
    default_task_repository,
)

import store  # noqa: E402

# --- Protocol satisfaction ---------------------------------------------------
ok(isinstance(StoreTaskRepository(), TaskRepository),
   "StoreTaskRepository satisfies TaskRepository Protocol")
ok(isinstance(AccessStoreRepository(), AccessRepository),
   "AccessStoreRepository satisfies AccessRepository Protocol")
ok(isinstance(store.task_repository, TaskRepository),
   "store.task_repository is a TaskRepository")
ok(isinstance(store.access_repository, AccessRepository),
   "store.access_repository is an AccessRepository")
ok(store.task_repository is not None and callable(store.task_repository.get_task),
   "store exposes task_repository with get_task")
ok(callable(default_task_repository) and callable(default_access_repository),
   "default repository factories are callable")

# --- application uses interfaces ---------------------------------------------
from switchboard.application.commands import create_task as create_cmd  # noqa: E402
from switchboard.application.commands import update_task as update_cmd  # noqa: E402
from switchboard.application.queries import get_task as get_cmd  # noqa: E402
from switchboard.contracts import CreateTaskCommand, GetTaskQuery, UpdateTaskCommand  # noqa: E402


class FakeTaskRepository:
    """In-memory TaskRepository — proves application does not require store SQL."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def get_task(self, task_id: str, project: str = "maxwell") -> Optional[dict[str, Any]]:
        row = self.rows.get(task_id.upper())
        return dict(row) if row else None

    def create_task(
            self,
            data: dict[str, Any],
            actor: str = "user",
            project: str = "maxwell") -> Optional[dict[str, Any]]:
        task_id = str(data.get("task_id") or f"{data['workstream_id']}-FAKE").upper()
        row = {
            "task_id": task_id,
            "title": data["title"],
            "workstream_id": data["workstream_id"],
            "status": data.get("status") or "Not Started",
            "actor": actor,
            "project": project,
        }
        self.rows[task_id] = row
        return dict(row)

    def update_task(
            self,
            task_id: str,
            fields: dict[str, Any],
            actor: str = "user",
            project: str = "maxwell") -> Optional[dict[str, Any]]:
        key = task_id.upper()
        if key not in self.rows:
            return None
        self.rows[key].update(fields)
        self.rows[key]["actor"] = actor
        self.rows[key]["project"] = project
        return dict(self.rows[key])


ok(isinstance(FakeTaskRepository(), TaskRepository),
   "in-memory FakeTaskRepository satisfies TaskRepository")

fake = FakeTaskRepository()
created = create_cmd.execute(
    CreateTaskCommand.from_mapping({
        "workstream_id": "ARCH",
        "title": "protocol proof",
    }),
    actor="test",
    project="switchboard",
    tasks=fake,
)
ok(bool(created) and created.get("title") == "protocol proof",
   "create_task command uses injected TaskRepository")
created_id = created["task_id"]
ok(get_cmd.execute(GetTaskQuery.from_inputs(created_id, project="switchboard"),
                   tasks=fake)["title"] == "protocol proof",
   "get_task query uses injected TaskRepository")
updated = update_cmd.execute(
    UpdateTaskCommand.from_mapping(created_id, {"title": "updated via protocol"}),
    actor="test",
    project="switchboard",
    tasks=fake,
)
ok(updated and updated["title"] == "updated via protocol",
   "update_task command uses injected TaskRepository")

# SQL must not appear in application command modules.
for rel in (
    "src/switchboard/application/commands/create_task.py",
    "src/switchboard/application/commands/update_task.py",
    "src/switchboard/application/queries/get_task.py",
):
    text = (ROOT / rel).read_text(encoding="utf-8")
    ok("sqlite" not in text.lower() and "SELECT " not in text,
       f"{rel} has no SQL")
    ok("TaskRepository" in text, f"{rel} depends on TaskRepository")

# Access repository can resolve known projects without application SQL.
access = store.access_repository
ok(access.has_project("switchboard") or access.has_project("maxwell"),
   "access repository answers has_project without application SQL")
ok(isinstance(access.projects(), list), "access repository lists projects")

print(f"\nARCH-MS-27 storage protocols: {passed} passed, {failed} failed")
shutil.rmtree(TMP, ignore_errors=True)
raise SystemExit(1 if failed else 0)
