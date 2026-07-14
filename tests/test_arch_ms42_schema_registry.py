#!/usr/bin/env python3
"""ARCH-MS-42: Event JSON Schema registry + compatibility tests."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from path_setup import ROOT

from switchboard.contracts import (
    CLAIM_TASK_COMMAND_SCHEMA,
    CREATE_TASK_COMMAND_SCHEMA,
    ClaimTaskCommand,
    CreateTaskCommand,
    REQUEST_WAKE_COMMAND_SCHEMA,
    RequestWakeCommand,
    list_schemas,
)
from switchboard.contracts.schema_export import (
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA,
    build_manifest,
    check_schema_registry,
    is_v1_schema_id,
    registered_v1_schemas,
    render_json,
    schema_filename,
    write_schema_registry,
)

SCHEMAS_DIR = ROOT / "schemas"
GENERATOR = ROOT / "scripts" / "generate_schemas.py"
FIXTURES_DIR = ROOT / "fixtures" / "contracts"

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# --- registry selection ------------------------------------------------------
all_registered = list_schemas()
v1 = registered_v1_schemas()
ok(len(v1) >= 20, f"at least 20 v1 schemas registered (got {len(v1)})")
ok(all(is_v1_schema_id(sid) for sid in v1), "every exported id matches switchboard.*.v1")
ok(all(not sid.endswith(".v2") for sid in v1), "v2 schemas are excluded from the registry")
ok(
    CREATE_TASK_COMMAND_SCHEMA in v1 and CLAIM_TASK_COMMAND_SCHEMA in v1,
    "task + claim v1 contracts are included",
)
ok(
    REQUEST_WAKE_COMMAND_SCHEMA in v1,
    "wake command schemas are included (agents/wakes packages loaded)",
)

for schema_id, payload in v1.items():
    ok(payload.get("$id") == schema_id, f"$id preserved for {schema_id}")

# Non-v1 registrations (e.g. project.v2) must still exist in-process but stay out.
non_v1 = [sid for sid in all_registered if not is_v1_schema_id(sid)]
ok(bool(non_v1), "registry still holds non-v1 schemas (e.g. project.v2)")
ok(all(sid not in v1 for sid in non_v1), "non-v1 schemas omitted from export map")

# --- deterministic serialization --------------------------------------------
first = render_json(v1[CREATE_TASK_COMMAND_SCHEMA])
second = render_json(registered_v1_schemas()[CREATE_TASK_COMMAND_SCHEMA])
ok(first == second, "render_json is deterministic for create_command")
ok(first.endswith("\n"), "rendered schema ends with newline")
ok('"$id"' in first, "serialized JSON keeps $id key")

manifest = build_manifest(v1)
ok(manifest["schema"] == MANIFEST_SCHEMA, "manifest advertises switchboard.schema_manifest.v1")
ok(manifest["count"] == len(v1), "manifest count matches exported schemas")
ok(
    [e["$id"] for e in manifest["schemas"]] == sorted(v1),
    "manifest schema list is sorted by $id",
)

# --- checked-in tree + CI diff gate ----------------------------------------
ok(SCHEMAS_DIR.is_dir(), f"checked-in schemas/ directory exists")
ok(GENERATOR.is_file(), f"generator script exists: {GENERATOR.relative_to(ROOT)}")
ok((SCHEMAS_DIR / MANIFEST_FILENAME).is_file(), "schemas/manifest.json exists")

problems = check_schema_registry(SCHEMAS_DIR)
ok(not problems, "checked-in schemas/ matches live registry")
if problems:
    for note in problems:
        print(f"         drift: {note}")

for schema_id in v1:
    path = SCHEMAS_DIR / schema_filename(schema_id)
    ok(path.is_file(), f"artifact present: {schema_filename(schema_id)}")
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        ok(loaded.get("$id") == schema_id, f"artifact $id == {schema_id}")

check = subprocess.run(
    [sys.executable, str(GENERATOR), "--check"],
    cwd=str(ROOT),
    capture_output=True,
    text=True,
)
ok(check.returncode == 0, "scripts/generate_schemas.py --check exits 0")
if check.returncode != 0:
    print((check.stderr or check.stdout or "").strip())

# Write to a temp dir and confirm --check still passes against schemas/.
import tempfile

with tempfile.TemporaryDirectory(prefix="arch-ms42-schemas-") as tmp:
    write_schema_registry(Path(tmp))
    tmp_problems = check_schema_registry(Path(tmp))
    ok(not tmp_problems, "fresh write_schema_registry produces a clean tree")
    # Accidental v2 leftovers must be removed on write (not only flagged by --check).
    stale = Path(tmp) / "switchboard.project.v2.json"
    stale.write_text('{"$id":"switchboard.project.v2"}\n', encoding="utf-8")
    write_schema_registry(Path(tmp))
    ok(not stale.exists(), "write_schema_registry removes stale switchboard.*.json (incl. v2)")
    ok(not check_schema_registry(Path(tmp)), "tree is clean after removing stale v2 file")

# --- golden instance compatibility (model_validate) ------------------------
ok(FIXTURES_DIR.is_dir(), "fixtures/contracts/ exists")

_COMPAT = (
    (CREATE_TASK_COMMAND_SCHEMA, CreateTaskCommand, "create_task_command.v1.json"),
    (CLAIM_TASK_COMMAND_SCHEMA, ClaimTaskCommand, "claim_task_command.v1.json"),
    (REQUEST_WAKE_COMMAND_SCHEMA, RequestWakeCommand, "request_wake_command.v1.json"),
)

for schema_id, model, filename in _COMPAT:
    fixture_path = FIXTURES_DIR / filename
    ok(fixture_path.is_file(), f"fixture present: {filename}")
    if not fixture_path.is_file():
        continue
    instance = json.loads(fixture_path.read_text(encoding="utf-8"))
    try:
        parsed = model.model_validate(instance)
        ok(True, f"{model.__name__}.model_validate accepts {filename}")
        ok(parsed.schema == schema_id, f"{filename} carries schema {schema_id}")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"{model.__name__}.model_validate rejected {filename}: {exc!r}")

# Docs surface
readme = ROOT / "docs" / "schemas" / "README.md"
ok(readme.is_file(), "docs/schemas/README.md documents the registry")

print()
print(f"{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
