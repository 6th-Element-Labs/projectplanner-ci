#!/usr/bin/env python3
"""ARCH-MS-25: versioned contracts package proof gate (+ BUG-55 parity vectors)."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

# The command modules import ``store``; point it at a throwaway sandbox before
# any application import. The error-path checks below never touch the DB.
TMP = tempfile.mkdtemp(prefix="arch-ms25-contracts-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from pydantic import ValidationError  # noqa: E402

from switchboard.contracts import (  # noqa: E402
    CREATE_TASK_COMMAND_SCHEMA,
    GET_TASK_QUERY_SCHEMA,
    UPDATE_TASK_COMMAND_SCHEMA,
    CreateTaskCommand,
    GetTaskQuery,
    UpdateTaskCommand,
    get_schema,
    list_schemas,
)
from switchboard.application.commands import create_task as create_task_command  # noqa: E402
from switchboard.application.commands import update_task as update_task_command  # noqa: E402
from switchboard.application.contracts.tasks import (  # noqa: E402
    CreateTaskCommand as LegacyCreateTaskCommand,
)


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# --- package skeleton --------------------------------------------------------
for name in ("switchboard.contracts", "switchboard.contracts.tasks",
             "switchboard.contracts.tasks.v1", "switchboard.contracts.registry"):
    try:
        importlib.import_module(name)
        ok(True, f"{name} imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"{name} import failed: {exc!r}")

init_path = ROOT / "src/switchboard/contracts/__init__.py"
ok(init_path.is_file(), "src/switchboard/contracts/ package exists on disk")

# --- schema ids + registry ---------------------------------------------------
ok(CreateTaskCommand.schema_name() == CREATE_TASK_COMMAND_SCHEMA,
   "CreateTaskCommand advertises a stable schema id")
ok(UpdateTaskCommand.schema_name() == UPDATE_TASK_COMMAND_SCHEMA,
   "UpdateTaskCommand advertises a stable schema id")
ok(GetTaskQuery.schema_name() == GET_TASK_QUERY_SCHEMA,
   "GetTaskQuery advertises a stable schema id")

create_schema = get_schema(CREATE_TASK_COMMAND_SCHEMA)
ok(create_schema is not None and create_schema.get("$id") == CREATE_TASK_COMMAND_SCHEMA,
   "registry records JSON Schema with $id for create command")
ok(CREATE_TASK_COMMAND_SCHEMA in list_schemas(),
   "list_schemas includes the create command schema")

# --- pydantic validation + adapter parity ------------------------------------
command = CreateTaskCommand.from_mapping({
    "workstream_id": " ARCH ",
    "title": " contract proof ",
    "depends_on": "a-1, A-1, b-2",
})
ok(command.workstream_id == "ARCH" and command.title == "contract proof",
   "CreateTaskCommand strips required text fields")
ok(command.depends_on == ("A-1", "B-2"),
   "CreateTaskCommand canonicalizes dependency ids")
ok(command.schema == CREATE_TASK_COMMAND_SCHEMA,
   "CreateTaskCommand carries the default schema field")

ok(LegacyCreateTaskCommand is CreateTaskCommand,
   "application.contracts shim re-exports the canonical CreateTaskCommand")

update = UpdateTaskCommand.from_mapping("t-1", {"is_blocking": "false", "depends_on": "none"})
ok(update.fields["is_blocking"] is False and update.depends_on == (),
   "UpdateTaskCommand preserves sparse update semantics")
ok(update.to_store_fields() == {"is_blocking": False, "depends_on": []},
   "UpdateTaskCommand emits explicit empty dependency list when cleared")

query = GetTaskQuery.from_inputs(" arch-ms-25 ", project=" switchboard ")
ok(query.task_id == "arch-ms-25" and query.project == "switchboard",
   "GetTaskQuery strips task_id and project")

mcp_locals = CreateTaskCommand.from_mapping({
    "workstream_id": "SUBJ",
    "title": "with deps",
    "ctx": None,
    "depends_on": "DEP-1, DEP-2",
    "project": "helm",
    "agent_id": "",
    "services": object(),
})
ok(mcp_locals.depends_on == ("DEP-1", "DEP-2"),
   "CreateTaskCommand ignores MCP adapter locals beyond task fields")

# --- golden store-mapping vectors (BUG-55) -------------------------------------
# Pinned from the pre-Pydantic dataclass from_mapping/to_store_data
# (git 68a99cf^:src/switchboard/application/contracts/tasks.py). If a vector
# drifts, an adapter-visible store mapping changed — that is a contract break,
# not a refactor.
FULL_INPUT = {
    "workstream_id": " ARCH ",
    "title": " golden vector ",
    "description": "desc",
    "workstream_name": "Architecture",
    "owner_org": "6EL",
    "owner_person_or_role": "steve",
    "assignee": "fable",
    "phase": "Build",
    "status": "In Progress",
    "effort_days": 2.5,
    "duration_days": 4,
    "start_date": "2026-07-01",
    "finish_date": "2026-07-05",
    "depends_on": "b-2, a-1, B-2",
    "entry_criteria": "entry",
    "exit_criteria": "exit",
    "deliverable": "a deliverable",
    "risk_level": "Low",
    "is_blocking": True,
    # adapter noise both eras ignore
    "project": "switchboard",
    "agent_id": "",
    "ctx": None,
}
FULL_STORE = {
    "workstream_id": "ARCH",
    "title": "golden vector",
    "description": "desc",
    "workstream_name": "Architecture",
    "owner_org": "6EL",
    "owner_person_or_role": "steve",
    "assignee": "fable",
    "phase": "Build",
    "status": "In Progress",
    "effort_days": 2.5,
    "duration_days": 4.0,
    "start_date": "2026-07-01",
    "finish_date": "2026-07-05",
    "depends_on": ["B-2", "A-1"],
    "entry_criteria": "entry",
    "exit_criteria": "exit",
    "deliverable": "a deliverable",
    "risk_level": "Low",
    "is_blocking": True,
}
ok(CreateTaskCommand.from_mapping(FULL_INPUT).to_store_data() == FULL_STORE,
   "golden vector: full payload maps to the dataclass-era store row")

NULL_OPTIONALS = {
    "description": None, "workstream_name": None, "owner_org": None,
    "owner_person_or_role": None, "assignee": None, "phase": None,
    "status": None, "effort_days": None, "duration_days": None,
    "start_date": None, "finish_date": None, "entry_criteria": None,
    "exit_criteria": None, "deliverable": None, "risk_level": None,
}
MCP_LOCALS_INPUT = {  # the MCP create_task tool passes locals(): '' for unset params
    "workstream_id": "BUG", "title": "mcp golden", "description": "",
    "owner_org": "", "owner_person_or_role": "", "status": "", "phase": "",
    "risk_level": "", "depends_on": "", "project": "switchboard",
    "agent_id": "", "system_actor": "", "system_reason": "",
}
ok(CreateTaskCommand.from_mapping(MCP_LOCALS_INPUT).to_store_data()
   == {"workstream_id": "BUG", "title": "mcp golden", "depends_on": [],
       "is_blocking": False, **NULL_OPTIONALS},
   "golden vector: MCP locals() '' defaults persist NULL, not ''")

ok(CreateTaskCommand.from_mapping(
       {"workstream_id": "ARCH", "title": "minimal"}).to_store_data()
   == {"workstream_id": "ARCH", "title": "minimal", "depends_on": [],
       "is_blocking": False, **NULL_OPTIONALS},
   "golden vector: minimal payload leaves every optional NULL")

blankish = CreateTaskCommand.from_mapping({
    "workstream_id": "ARCH", "title": "blank numerics",
    "effort_days": "", "duration_days": " ",
})
ok(blankish.effort_days is None and blankish.duration_days is None,
   "blank-string numerics mean unset, not a parse error")

ok(CreateTaskCommand.from_mapping({
       "workstream_id": "ARCH", "title": "bool tokens", "is_blocking": "false",
   }).is_blocking is False,
   "create-path is_blocking 'false' decodes False (dataclass-era bool() bug stays fixed)")

# --- REST error-path regression: 500 → structured 400 (BUG-55) -----------------
missing = create_task_command.execute_mapping_result(
    {"title": "no workstream"}, actor="test", project="switchboard")
ok(missing.get("error_code") == "invalid_create_task"
   and bool(missing.get("error"))
   and "workstream_id" in str(missing.get("message")),
   "missing workstream_id returns structured invalid_create_task, not a raise")

empty_payload = create_task_command.execute_mapping_result(
    {}, actor="test", project="switchboard")
ok(empty_payload.get("error_code") == "invalid_create_task",
   "empty payload returns structured invalid_create_task")

junk_typed = create_task_command.execute_mapping_result(
    {"workstream_id": "ARCH", "title": "junk", "effort_days": "abc"},
    actor="test", project="switchboard")
ok(junk_typed.get("error_code") == "invalid_create_task"
   and "effort_days" in str(junk_typed.get("message")),
   "type-invalid effort_days returns structured invalid_create_task "
   "(the dataclass era silently persisted the junk)")

present_empty = create_task_command.execute_mapping_result(
    {"workstream_id": "", "title": ""}, actor="test", project="switchboard")
ok(present_empty.get("error_code") == "invalid_create_task"
   and present_empty.get("message") == "workstream_id and title are required",
   "present-but-empty required fields keep the dataclass-era error message")

# --- update guard symmetry -----------------------------------------------------
try:
    CreateTaskCommand.model_validate({})
    contract_error = None
except ValidationError as exc:
    contract_error = exc
ok(contract_error is not None,
   "model_validate({}) raises the ValidationError the adapters must convert")

if contract_error is None:
    ok(False, "update guard check skipped — no ValidationError captured")
else:
    class _RaisingUpdateCommand:
        @staticmethod
        def from_mapping(task_id, value):
            raise contract_error

    _original_update_command = update_task_command.UpdateTaskCommand
    update_task_command.UpdateTaskCommand = _RaisingUpdateCommand
    try:
        guarded = update_task_command.execute_mapping_result(
            "T-1", {"title": "x"}, actor="test", project="switchboard")
    finally:
        update_task_command.UpdateTaskCommand = _original_update_command
    ok(isinstance(guarded, dict) and guarded.get("error_code") == "invalid_update_task",
       "update execute_mapping_result converts ValidationError to invalid_update_task")

shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
