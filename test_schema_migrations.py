#!/usr/bin/env python3
"""BUG-47 — additive schema migrations are numbered, ledgered, and fail loud.

Guards the regression where db.schema.apply_schema ran every additive ALTER inside
`try: c.execute(col_sql) except Exception: pass`, so a disk-full, permission, corruption,
lock, or syntax failure was indistinguishable from a benign "column already exists" and was
silently swallowed at import by both the web and MCP startup paths.
"""
import os
import sqlite3
import tempfile

os.environ.setdefault("PM_DB_PATH", tempfile.mktemp(suffix=".bug47.db"))
os.environ.setdefault("PM_PROJECT_REGISTRY_DB_PATH", os.environ["PM_DB_PATH"] + ".reg")

from db.schema import apply_schema
from db.migrations import (
    ADDITIVE_COLUMN_MIGRATIONS,
    DDL_MIGRATIONS,
    run_additive_migrations,
    _is_duplicate_column,
)

passed = 0
failed = 0


def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}")


def mem():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ledger(conn):
    return {r["name"] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}


ALL_NAMES = {m[0] for m in ADDITIVE_COLUMN_MIGRATIONS} | {m[0] for m in DDL_MIGRATIONS}

# 1. Fresh DB: apply_schema builds the full additive schema and records every migration.
c = mem()
apply_schema(c)
check("tasks.agent_state added", "agent_state" in cols(c, "tasks"))
check("tasks.narration_source_revision added", "narration_source_revision" in cols(c, "tasks"))
check("deliverables.board_id added", "board_id" in cols(c, "deliverables"))
check("breakdown_proposals.reviewed_by added",
      "reviewed_by" in cols(c, "deliverable_breakdown_proposals"))
check("every migration recorded in the ledger", ledger(c) == ALL_NAMES)
check("ux_messages_idem index created", c.execute(
    "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='ux_messages_idem'"
).fetchone()[0] == 1)
check("review_verdicts table created", c.execute(
    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='review_verdicts'"
).fetchone()[0] == 1)
check("review verdicts persist authenticated reviewer principal IDs",
      "reviewer_principal_id" in cols(c, "review_verdicts"))
check("review_findings table created", c.execute(
    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='review_findings'"
).fetchone()[0] == 1)
check("review finding resolutions persist authority identity and timestamp",
      {"resolved_principal_id", "resolved_at"}.issubset(cols(c, "review_findings")))
check("review finding query index created", c.execute(
    "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
    "AND name='ix_review_findings_task_state'"
).fetchone()[0] == 1)

# 2. Idempotent: re-running executes nothing new and never errors.
check("second run applies zero new migrations", run_additive_migrations(c) == [])
apply_schema(c)
check("ledger stable after a second apply_schema", ledger(c) == ALL_NAMES)

# 3. Legacy DB (columns already present, ledger lost): reconcile via PRAGMA, no error, no
#    duplicate-column ALTER. This is the exact case the old swallow-all loop was hiding.
c3 = mem()
apply_schema(c3)
c3.execute("DELETE FROM schema_migrations")
run_additive_migrations(c3)  # must not raise even though every column already exists
check("legacy DB with pre-existing columns reconciles the ledger", ledger(c3) == ALL_NAMES)

# 4. Regression: a real, non-duplicate failure propagates instead of being swallowed. A DB
#    that is missing the `tasks` table makes migration 0001 raise "no such table".
c4 = mem()
c4.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at REAL NOT NULL)")
propagated = False
try:
    run_additive_migrations(c4)
except sqlite3.OperationalError as exc:
    propagated = "no such table" in str(exc).lower()
check("a real migration failure propagates rather than being swallowed", propagated)

# 5. The tolerated-error classifier catches ONLY duplicate-column.
check("duplicate column name is classified benign",
      _is_duplicate_column(sqlite3.OperationalError("duplicate column name: agent_state")))
check("disk I/O error is NOT classified benign",
      not _is_duplicate_column(sqlite3.OperationalError("disk I/O error")))
check("corruption is NOT classified benign",
      not _is_duplicate_column(sqlite3.DatabaseError("database disk image is malformed")))

# 6. Migration names are unique and stable (a duplicated/renumbered key would corrupt the
#    ledger's idempotency).
names = [m[0] for m in ADDITIVE_COLUMN_MIGRATIONS] + [m[0] for m in DDL_MIGRATIONS]
check("migration names are unique", len(names) == len(set(names)))

# 7. Legacy decisions table: COORD-3 columns must be added before their indexes.
# Creating those indexes in base DDL fails startup before the additive runner can repair
# a real pre-COORD-3 database.
c5 = mem()
apply_schema(c5)
c5.execute("DROP TABLE decisions")
c5.execute("""
    CREATE TABLE decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT, author TEXT NOT NULL, title TEXT NOT NULL, context TEXT NOT NULL,
        decision TEXT NOT NULL, rationale TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'accepted', supersedes INTEGER, created_at REAL NOT NULL
    )
""")
c5.execute("DELETE FROM schema_migrations WHERE name >= '0020'")
apply_schema(c5)
check("legacy decisions table gains structured coordinator columns",
      {"decision_key", "decision_kind", "deliverable_id", "inputs_json",
       "chosen_action_json", "skipped_alternatives_json", "result_json"}.issubset(
           cols(c5, "decisions")))
check("legacy decisions indexes are created after columns",
      {"ux_decisions_key", "ix_decisions_deliverable", "ix_decisions_kind"}.issubset({
          row["name"] for row in c5.execute("PRAGMA index_list(decisions)").fetchall()
      }))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
