#!/usr/bin/env python3
"""ARCH-MS-28: formalize BUG-47 ledger migrations under switchboard.storage.migrations."""
from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401 — src/ on sys.path via ROOT

os.environ.setdefault("PM_DB_PATH", tempfile.mktemp(suffix=".arch-ms28.db"))
os.environ.setdefault("PM_PROJECT_REGISTRY_DB_PATH", os.environ["PM_DB_PATH"] + ".reg")

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# --- package skeleton --------------------------------------------------------
for name in (
    "switchboard.storage.migrations",
    "switchboard.storage.migrations.runner",
):
    try:
        importlib.import_module(name)
        ok(True, f"{name} imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"{name} import failed: {exc!r}")

ok((ROOT / "src/switchboard/storage/migrations/runner.py").is_file(),
   "runner.py exists under storage/migrations")

from switchboard.storage.migrations import (  # noqa: E402
    ADDITIVE_COLUMN_MIGRATIONS,
    DDL_MIGRATIONS,
    is_duplicate_column,
    run_additive_migrations,
)
from switchboard.storage.migrations.runner import (  # noqa: E402
    ADDITIVE_COLUMN_MIGRATIONS as RUNNER_COLUMNS,
    DDL_MIGRATIONS as RUNNER_DDL,
    run_additive_migrations as runner_run,
)
import db.migrations as legacy_migrations  # noqa: E402
from db.schema import apply_schema  # noqa: E402

ok(ADDITIVE_COLUMN_MIGRATIONS is RUNNER_COLUMNS,
   "package exports the canonical ADDITIVE_COLUMN_MIGRATIONS list")
ok(DDL_MIGRATIONS is RUNNER_DDL,
   "package exports the canonical DDL_MIGRATIONS list")
ok(run_additive_migrations is runner_run,
   "package run_additive_migrations is the runner implementation")
ok(legacy_migrations.run_additive_migrations is run_additive_migrations,
   "db.migrations shim re-exports package runner")
ok(legacy_migrations.ADDITIVE_COLUMN_MIGRATIONS is ADDITIVE_COLUMN_MIGRATIONS,
   "db.migrations shim re-exports migration registry")

ALL_NAMES = {m[0] for m in ADDITIVE_COLUMN_MIGRATIONS} | {m[0] for m in DDL_MIGRATIONS}
ok(len(ALL_NAMES) >= 19, "ledger includes numbered column and DDL migrations")

# --- behavioral parity (subset of BUG-47 gate) -------------------------------
conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
apply_schema(conn)

cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
ok("agent_state" in cols and "narration_source_hash" in cols,
   "apply_schema applies package-backed migrations on fresh DB")

ledger = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
ok(ledger == ALL_NAMES, "fresh DB records every migration in schema_migrations ledger")
ok(run_additive_migrations(conn) == [], "second run is a no-op")

conn.execute("DELETE FROM schema_migrations")
run_additive_migrations(conn)
ok({r["name"] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()} == ALL_NAMES,
   "legacy DB with pre-existing columns reconciles ledger via package runner")

propagated = False
broken = sqlite3.connect(":memory:")
broken.row_factory = sqlite3.Row
broken.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at REAL NOT NULL)")
try:
    run_additive_migrations(broken)
except sqlite3.OperationalError as exc:
    propagated = "no such table" in str(exc).lower()
ok(propagated, "non-duplicate migration failures still propagate")

ok(is_duplicate_column(sqlite3.OperationalError("duplicate column name: agent_state")),
   "is_duplicate_column classifies benign duplicate-column errors")
ok(not is_duplicate_column(sqlite3.OperationalError("disk I/O error")),
   "is_duplicate_column rejects disk failures")

print(f"\nARCH-MS-28 storage migrations: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
