#!/usr/bin/env python3
"""ARCH-MS-41: OpenAPI 3.1 from Pydantic contracts + CI diff gate."""
from __future__ import annotations

import json
import subprocess
import sys

from path_setup import ROOT

from switchboard.contracts.openapi import (
    OPENAPI_VERSION,
    API_TITLE,
    API_VERSION,
    build_openapi_document,
    component_name,
    render_openapi_json,
    required_operation_ids,
    required_paths,
)
from switchboard.contracts import (
    CLAIM_NEXT_COMMAND_SCHEMA,
    CLAIM_TASK_COMMAND_SCHEMA,
    COMPLETE_CLAIM_COMMAND_SCHEMA,
    CREATE_TASK_COMMAND_SCHEMA,
    GET_TASK_QUERY_SCHEMA,
    MOVE_TASK_COMMAND_SCHEMA,
    UPDATE_TASK_COMMAND_SCHEMA,
)

GOLDEN = ROOT / "openapi" / "switchboard.openapi.json"
GENERATOR = ROOT / "scripts" / "generate_openapi.py"

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# --- document shape ----------------------------------------------------------
doc = build_openapi_document()
ok(doc.get("openapi") == OPENAPI_VERSION, f"openapi declares {OPENAPI_VERSION}")
ok(doc["info"]["title"] == API_TITLE, "info.title identifies Switchboard REST")
ok(doc["info"]["version"] == API_VERSION, "info.version is v1")

paths = doc.get("paths") or {}
for path in required_paths():
    ok(path in paths, f"path present: {path}")

operation_ids = {
    op.get("operationId")
    for path_item in paths.values()
    for op in path_item.values()
    if isinstance(op, dict)
}
for op_id in required_operation_ids():
    ok(op_id in operation_ids, f"operationId present: {op_id}")

schemas = (doc.get("components") or {}).get("schemas") or {}
for schema_id in (
    CREATE_TASK_COMMAND_SCHEMA,
    UPDATE_TASK_COMMAND_SCHEMA,
    MOVE_TASK_COMMAND_SCHEMA,
    GET_TASK_QUERY_SCHEMA,
    CLAIM_TASK_COMMAND_SCHEMA,
    CLAIM_NEXT_COMMAND_SCHEMA,
    COMPLETE_CLAIM_COMMAND_SCHEMA,
):
    name = component_name(schema_id)
    ok(name in schemas, f"component schema for {schema_id} → {name}")
    ok("$id" not in schemas[name], f"{name} has no duplicate $id (OpenAPI component)")

# CreateTaskCommand should expose required wire fields from Pydantic.
create = schemas[component_name(CREATE_TASK_COMMAND_SCHEMA)]
required = set(create.get("required") or [])
ok("workstream_id" in required and "title" in required,
   "CreateTaskCommand required fields include workstream_id + title")
props = create.get("properties") or {}
ok("description" in props, "CreateTaskCommand documents optional description")

# --- deterministic serialization --------------------------------------------
first = render_openapi_json(doc)
second = render_openapi_json(build_openapi_document())
ok(first == second, "render_openapi_json is deterministic")
ok(first.endswith("\n"), "rendered OpenAPI ends with newline")
parsed = json.loads(first)
ok(parsed["openapi"] == OPENAPI_VERSION, "serialized JSON still OpenAPI 3.1")

# --- CI diff gate -----------------------------------------------------------
ok(GOLDEN.is_file(), f"checked-in golden exists: {GOLDEN.relative_to(ROOT)}")
ok(GENERATOR.is_file(), f"generator script exists: {GENERATOR.relative_to(ROOT)}")

if GOLDEN.is_file():
    golden_text = GOLDEN.read_text(encoding="utf-8")
    ok(golden_text == first,
       "golden openapi/switchboard.openapi.json matches generated document")
    if golden_text != first:
        print("         regenerate with: python scripts/generate_openapi.py")

check = subprocess.run(
    [sys.executable, str(GENERATOR), "--check"],
    cwd=str(ROOT),
    capture_output=True,
    text=True,
)
ok(check.returncode == 0, "scripts/generate_openapi.py --check exits 0")
if check.returncode != 0:
    print((check.stderr or check.stdout or "").strip())

print()
print(f"{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
