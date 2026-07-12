#!/usr/bin/env python3
"""ARCH-MS-25: versioned contracts package proof gate."""
from __future__ import annotations

import importlib

from path_setup import ROOT

from switchboard.contracts import (
    CREATE_TASK_COMMAND_SCHEMA,
    GET_TASK_QUERY_SCHEMA,
    UPDATE_TASK_COMMAND_SCHEMA,
    CreateTaskCommand,
    GetTaskQuery,
    UpdateTaskCommand,
    get_schema,
    list_schemas,
)
from switchboard.application.contracts.tasks import CreateTaskCommand as LegacyCreateTaskCommand


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

legacy = LegacyCreateTaskCommand.from_mapping({
    "workstream_id": "ARCH",
    "title": "legacy shim",
    "depends_on": "x-1, X-1",
})
ok(legacy.to_store_data() == CreateTaskCommand.from_mapping({
    "workstream_id": "ARCH",
    "title": "legacy shim",
    "depends_on": "x-1, X-1",
}).to_store_data(),
   "application.contracts shim preserves create-task store mapping")

update = UpdateTaskCommand.from_mapping("t-1", {"is_blocking": "false", "depends_on": "none"})
ok(update.fields["is_blocking"] is False and update.depends_on == (),
   "UpdateTaskCommand preserves sparse update semantics")
ok(update.to_store_fields() == {"is_blocking": False, "depends_on": []},
   "UpdateTaskCommand emits explicit empty dependency list when cleared")

query = GetTaskQuery.from_inputs(" arch-ms-25 ", project=" switchboard ")
ok(query.task_id == "arch-ms-25" and query.project == "switchboard",
   "GetTaskQuery strips task_id and project")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
