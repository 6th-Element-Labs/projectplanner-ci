#!/usr/bin/env python3
"""Focused proof for the ARCH-MS-40 move-task application command."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

TMP = tempfile.mkdtemp(prefix="arch-ms40-move-task-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from switchboard.application.commands import move_task  # noqa: E402
from switchboard.application.contracts.tasks import MoveTaskCommand  # noqa: E402
from switchboard.contracts import MOVE_TASK_COMMAND_SCHEMA, MoveTaskCommand as Wired  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_project_registry()
    store.init_db("switchboard")
    store.create_project("Vulkan", actor="test")
    store.init_db("vulkan")

    # ---- MoveTaskCommand contract (no store) ---------------------------------
    command = MoveTaskCommand.from_mapping("T-1", {
        "project_from": " switchboard ",
        "destination_project": " vulkan ",
        "reason": " leaked ",
        "dependency_policy": "CLEAR",
    })
    ok(command.schema_id == MOVE_TASK_COMMAND_SCHEMA
       and command.project_from == "switchboard"
       and command.project_to == "vulkan"
       and command.reason == "leaked"
       and command.dependency_policy == "clear",
       "command normalizes destination alias, whitespace, and policy case")
    ok(Wired.SCHEMA == MOVE_TASK_COMMAND_SCHEMA,
       "package contracts re-export the move command schema")

    bad_policy = move_task.execute_mapping_result(
        "T-1",
        {"project_from": "switchboard", "project_to": "vulkan",
         "dependency_policy": "drop"},
        actor="test",
    )
    ok(bad_policy.get("error_code") == "invalid_move_task"
       and "dependency_policy" in (bad_policy.get("error") or ""),
       "invalid dependency_policy fails closed before persistence")

    same = move_task.execute_mapping_result(
        "T-1",
        {"project_from": "switchboard", "project_to": "switchboard"},
        actor="test",
    )
    ok(same.get("error_code") == "same_project",
       "same-project moves are rejected at the command layer")

    missing_dest = move_task.execute_mapping_result(
        "T-1", {"project_from": "switchboard"}, actor="test")
    ok(missing_dest.get("error_code") == "invalid_move_task"
       and "project_to" in (missing_dest.get("error") or ""),
       "missing destination fails closed before persistence")

    # ---- execute delegates to the injected mover -----------------------------
    calls = []

    def fake_move(task_id, **kwargs):
        calls.append((task_id, kwargs))
        return {"moved": True, "task_id": task_id, **kwargs}

    result = move_task.execute(
        MoveTaskCommand.from_mapping("ARCH-1", {
            "project_from": "switchboard",
            "project_to": "vulkan",
            "reason": "cleanup",
            "new_task_id": "ARCH-1B",
            "dependency_policy": "clear",
        }),
        actor="tester",
        move=fake_move,
    )
    ok(result["moved"] is True and calls
       and calls[0][0] == "ARCH-1"
       and calls[0][1]["project_from"] == "switchboard"
       and calls[0][1]["project_to"] == "vulkan"
       and calls[0][1]["actor"] == "tester"
       and calls[0][1]["new_task_id"] == "ARCH-1B"
       and calls[0][1]["dependency_policy"] == "clear",
       "command forwards normalized fields to the persistence mover")

    # ---- store-backed happy path (smoke) -------------------------------------
    source = store.create_task(
        {"workstream_id": "ARCH", "title": "move me"},
        actor="test",
        project="switchboard",
    )
    moved = move_task.execute_mapping_result(
        source["task_id"],
        {"project_from": "switchboard", "project_to": "vulkan",
         "reason": "ARCH-MS-40"},
        actor="test",
    )
    ok(moved.get("moved") is True
       and store.get_task(source["task_id"], project="switchboard") is None
       and store.get_task(source["task_id"], project="vulkan") is not None,
       "command moves a real task through store.move_task")

    # ---- both adapters invoke the shared application handler -----------------
    task_router_source = (
        ROOT / "src/switchboard/api/routers/tasks.py"
    ).read_text(encoding="utf-8")
    mcp_source = (ROOT / "src/switchboard/mcp/tools/tasks.py").read_text(encoding="utf-8")
    ok("move_task_command.execute_mapping_result" in task_router_source
       and "move_task_command.execute_mapping_result" in mcp_source
       and "store.move_task(" not in task_router_source
       and "store.move_task(" not in mcp_source,
       "REST and MCP adapters invoke the same move-task command")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
