#!/usr/bin/env python3
"""Focused proof for the ARCH-MS-8 create-task application command."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT, entrypoint_source

TMP = tempfile.mkdtemp(prefix="arch-ms8-create-task-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from switchboard.application.commands import create_task  # noqa: E402
from switchboard.application.contracts.tasks import CreateTaskCommand  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_project_registry()
    store.init_db("switchboard")
    dependency = store.create_task(
        {"workstream_id": "ARCH", "title": "dependency"},
        actor="test",
        project="switchboard",
    )

    command = CreateTaskCommand.from_mapping({
        "workstream_id": "ARCH",
        "title": "created through application command",
        "depends_on": f"{dependency['task_id'].lower()}, {dependency['task_id']}",
        "risk_level": "High",
    })
    created = create_task.execute(command, actor="test", project="switchboard")
    ok(created["depends_on"] == [dependency["task_id"]],
       "command canonicalizes and de-duplicates dependency ids")
    ok(created["risk_level"] == "High" and created["title"] == command.title,
       "command preserves typed create-task fields")

    before = len(store.list_tasks(project="switchboard"))
    try:
        create_task.execute(
            CreateTaskCommand.from_mapping({
                "workstream_id": "ARCH",
                "title": "must not be written",
                "depends_on": "MISSING-404",
            }),
            actor="test",
            project="switchboard",
        )
        rejected = False
    except create_task.CreateTaskError as exc:
        rejected = exc.code == "unknown_dependencies"
    ok(rejected and len(store.list_tasks(project="switchboard")) == before,
       "unknown dependencies fail closed before task creation")

    app_source = entrypoint_source("app")
    task_router_source = (
        ROOT / "src/switchboard/api/routers/tasks.py"
    ).read_text(encoding="utf-8")
    mcp_source = (ROOT / "src/switchboard/mcp/tools/tasks.py").read_text(encoding="utf-8")
    ok("_create_task_router" in app_source and
       "create_task_command.execute_mapping_result" in task_router_source and
       "create_task_command.execute_mapping_result" in mcp_source,
       "REST and MCP adapters invoke the same application handler")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
