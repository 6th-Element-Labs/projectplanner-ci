#!/usr/bin/env python3
"""Focused proof for the ARCH-MS-15 get-task query + update-task command."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix="arch-ms15-get-update-task-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import store  # noqa: E402
from switchboard.application.commands import update_task  # noqa: E402
from switchboard.application.queries import get_task  # noqa: E402
from switchboard.application.contracts.tasks import UpdateTaskCommand  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def make_task(title, **extra):
    return store.create_task(
        {"workstream_id": "ARCH", "title": title, **extra},
        actor="test",
        project="switchboard",
    )


try:
    store.init_project_registry()
    store.init_db("switchboard")

    # ---- UpdateTaskCommand contract (no store) --------------------------------
    coerced = UpdateTaskCommand.from_mapping("T-1", {"is_blocking": "false"})
    ok(coerced.fields["is_blocking"] is False,
       "command decodes the 'false' string instead of trusting truthiness")
    ok(UpdateTaskCommand.from_mapping("T-1", {"is_blocking": "true"}).fields["is_blocking"] is True,
       "command decodes the 'true' string to a real bool")
    ok(UpdateTaskCommand.from_mapping("T-1", {"depends_on": "a, A, b"}).depends_on == ("A", "B"),
       "command canonicalizes and de-duplicates replacement dependency ids")
    ok(UpdateTaskCommand.from_mapping("T-1", {"depends_on": "none"}).depends_on == (),
       "clear sentinel maps to an empty replacement edge list")
    absent = UpdateTaskCommand.from_mapping("T-1", {"description": "x"})
    ok(absent.depends_on is None and absent.to_store_fields() == {"description": "x"},
       "an untouched edge list is omitted from the store field map")
    ok(UpdateTaskCommand.from_mapping("T-1", {"depends_on": "none"}).to_store_fields() == {"depends_on": []},
       "a cleared edge list is written as an explicit empty list")

    # ---- get_task query ------------------------------------------------------
    dependency = make_task("dependency")
    target = make_task("original", risk_level="High")
    detail = get_task.execute_for(target["task_id"], project="switchboard")
    ok(detail is not None and detail["task_id"] == target["task_id"]
       and "activity" in detail and "dependency_state" in detail,
       "query returns full task detail for an existing id")
    ok(get_task.execute_for(target["task_id"].lower(), project="switchboard")["task_id"]
       == target["task_id"],
       "query resolves task ids case-insensitively through the store")
    ok(get_task.execute_for("NOPE-404", project="switchboard") is None,
       "query returns None for a missing task")

    # ---- update_task command: sparse update preserves untouched fields -------
    updated = update_task.execute_mapping_result(
        target["task_id"], {"description": "new desc"}, actor="test", project="switchboard")
    ok(updated["description"] == "new desc" and updated["title"] == "original"
       and updated["risk_level"] == "High",
       "sparse update writes only the supplied field and preserves the rest")

    # ---- update_task command: dependency replacement + fail-closed check -----
    linked = update_task.execute_mapping_result(
        target["task_id"], {"depends_on": f"{dependency['task_id'].lower()}"},
        actor="test", project="switchboard")
    ok(linked["depends_on"] == [dependency["task_id"]],
       "update canonicalizes a replacement dependency edge")

    rejected = update_task.execute_mapping_result(
        target["task_id"], {"depends_on": "MISSING-404"}, actor="test", project="switchboard")
    still = get_task.execute_for(target["task_id"], project="switchboard")
    ok(rejected.get("error_code") == "unknown_dependencies"
       and still["depends_on"] == [dependency["task_id"]],
       "unknown dependency ids fail closed and leave the edge list untouched")

    cleared = update_task.execute_mapping_result(
        target["task_id"], {"depends_on": "none"}, actor="test", project="switchboard")
    ok(cleared["depends_on"] == [],
       "the clear sentinel removes every dependency edge")

    # ---- update_task command: store error + missing task pass through --------
    done_blocked = update_task.execute_mapping_result(
        target["task_id"], {"status": "Done"}, actor="test", project="switchboard")
    ok(done_blocked.get("error") == "done_requires_merge_provenance",
       "Done without provenance surfaces the store error rather than silently passing")
    ok(update_task.execute_mapping_result(
        "NOPE-404", {"title": "x"}, actor="test", project="switchboard") is None,
       "updating a missing task returns None for the adapter to render as 404")

    # ---- both adapters invoke the shared application handlers -----------------
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    mcp_source = (ROOT / "mcp_server.py").read_text(encoding="utf-8")
    ok("get_task_query.execute_for" in app_source and "get_task_query.execute_for" in mcp_source,
       "REST and MCP read paths invoke the same get-task query")
    ok("update_task_command.execute_mapping_result" in app_source
       and "update_task_command.execute_mapping_result" in mcp_source,
       "REST and MCP write paths invoke the same update-task command")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
