"""Numbered, ledgered additive schema migrations (BUG-47 / ARCH-MS-28).

Replaces the historical loop in ``db.schema.apply_schema``::

    for col_sql in [...]:
        try:
            c.execute(col_sql)
        except Exception:
            pass  # column already exists

which ran at import in both the web (app.py) and MCP (mcp_server.py) startup paths. That
loop could not tell a benign "duplicate column name" from a disk-full, permission,
corruption, syntax, or lock failure — every error was swallowed identically, so a broken
deploy looked exactly like a healthy one.

Each migration here runs at most once, recorded in the ``schema_migrations`` ledger. A
column that already exists on a legacy DB (created before the ledger existed, or by a
concurrent writer) is the ONLY tolerated condition: it is detected authoritatively with
``PRAGMA table_info`` *before* the ALTER runs and reconciled into the ledger without
executing anything, with a narrow duplicate-column catch kept as defense in depth. Every
other error propagates, so a failed migration fails the startup that ran it instead of
silently degrading the schema.

Migrations are idempotent and safe to run on every startup: the ledger plus the PRAGMA
pre-check make an already-applied migration a no-op, so running once "during deploy before
either service starts" and running at each service import converge to the same result.

This module is the ADR-0007 / ADR-0009 home for numbered migrations under
``src/switchboard/storage/migrations/``. ``db.migrations`` re-exports this surface for
Layer-0 callers during the strangler cutover.
"""
from __future__ import annotations

import sqlite3
import time
from typing import List, Tuple

# Ordered and append-only. ``name`` is the immutable ledger key — never renumber, rename,
# or reuse one. Each tuple is (name, table, column, ddl); every entry adds one column and
# mirrors, in order, the additive ALTER statements this module replaced.
ADDITIVE_COLUMN_MIGRATIONS: List[Tuple[str, str, str, str]] = [
    ("0001_tasks_agent_state", "tasks", "agent_state",
     "ALTER TABLE tasks ADD COLUMN agent_state TEXT"),
    ("0002_agent_messages_signal", "agent_messages", "signal",
     "ALTER TABLE agent_messages ADD COLUMN signal TEXT"),
    ("0003_agent_messages_priority", "agent_messages", "priority",
     "ALTER TABLE agent_messages ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"),
    ("0004_agent_messages_idem_key", "agent_messages", "idem_key",
     "ALTER TABLE agent_messages ADD COLUMN idem_key TEXT"),
    ("0005_agent_messages_principal_id", "agent_messages", "principal_id",
     "ALTER TABLE agent_messages ADD COLUMN principal_id TEXT"),
    ("0006_wake_intents_effect_key", "wake_intents", "effect_key",
     "ALTER TABLE wake_intents ADD COLUMN effect_key TEXT"),
    ("0007_runner_control_requests_effect_key", "runner_control_requests", "effect_key",
     "ALTER TABLE runner_control_requests ADD COLUMN effect_key TEXT"),
    ("0008_deliverables_board_id", "deliverables", "board_id",
     "ALTER TABLE deliverables ADD COLUMN board_id TEXT"),
    ("0009_deliverable_task_links_board_id", "deliverable_task_links", "board_id",
     "ALTER TABLE deliverable_task_links ADD COLUMN board_id TEXT"),
    ("0010_breakdown_proposals_outcome_text", "deliverable_breakdown_proposals",
     "outcome_text",
     "ALTER TABLE deliverable_breakdown_proposals ADD COLUMN outcome_text TEXT"),
    ("0011_breakdown_proposals_review_reason", "deliverable_breakdown_proposals",
     "review_reason",
     "ALTER TABLE deliverable_breakdown_proposals ADD COLUMN review_reason TEXT"),
    ("0012_breakdown_proposals_deferred_until", "deliverable_breakdown_proposals",
     "deferred_until",
     "ALTER TABLE deliverable_breakdown_proposals ADD COLUMN deferred_until REAL"),
    ("0013_breakdown_proposals_reviewed_by", "deliverable_breakdown_proposals",
     "reviewed_by",
     "ALTER TABLE deliverable_breakdown_proposals ADD COLUMN reviewed_by TEXT"),
    ("0014_external_ci_runs_status_context", "external_ci_runs", "status_context",
     "ALTER TABLE external_ci_runs ADD COLUMN status_context TEXT"),
    ("0015_tasks_narration_source_revision", "tasks", "narration_source_revision",
     "ALTER TABLE tasks ADD COLUMN narration_source_revision INTEGER NOT NULL DEFAULT 0"),
    ("0016_tasks_narration_source_hash", "tasks", "narration_source_hash",
     "ALTER TABLE tasks ADD COLUMN narration_source_hash TEXT"),
    ("0017_deliverables_narration_source_revision", "deliverables",
     "narration_source_revision",
     "ALTER TABLE deliverables ADD COLUMN narration_source_revision INTEGER NOT NULL DEFAULT 0"),
    ("0018_deliverables_narration_source_hash", "deliverables", "narration_source_hash",
     "ALTER TABLE deliverables ADD COLUMN narration_source_hash TEXT"),
    # COORD-3 — structured coordinator decision trail (explainable planner).
    ("0020_decisions_decision_key", "decisions", "decision_key",
     "ALTER TABLE decisions ADD COLUMN decision_key TEXT"),
    ("0021_decisions_decision_kind", "decisions", "decision_kind",
     "ALTER TABLE decisions ADD COLUMN decision_kind TEXT"),
    ("0022_decisions_deliverable_id", "decisions", "deliverable_id",
     "ALTER TABLE decisions ADD COLUMN deliverable_id TEXT"),
    ("0023_decisions_coordinator_agent_id", "decisions", "coordinator_agent_id",
     "ALTER TABLE decisions ADD COLUMN coordinator_agent_id TEXT"),
    ("0024_decisions_inputs_json", "decisions", "inputs_json",
     "ALTER TABLE decisions ADD COLUMN inputs_json TEXT"),
    ("0025_decisions_policy_rule", "decisions", "policy_rule",
     "ALTER TABLE decisions ADD COLUMN policy_rule TEXT"),
    ("0026_decisions_chosen_action_json", "decisions", "chosen_action_json",
     "ALTER TABLE decisions ADD COLUMN chosen_action_json TEXT"),
    ("0027_decisions_skipped_alternatives_json", "decisions", "skipped_alternatives_json",
     "ALTER TABLE decisions ADD COLUMN skipped_alternatives_json TEXT"),
    ("0028_decisions_result_json", "decisions", "result_json",
     "ALTER TABLE decisions ADD COLUMN result_json TEXT"),
]

# Idempotent DDL migrations (``CREATE ... IF NOT EXISTS``) applied after the column set,
# once each, recorded in the same ledger. (name, sql).
DDL_MIGRATIONS: List[Tuple[str, str]] = [
    ("0019_ux_messages_idem",
     "CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_idem "
     "ON agent_messages(idem_key) WHERE idem_key IS NOT NULL"),
    ("0029_ix_decisions_deliverable",
     "CREATE INDEX IF NOT EXISTS ix_decisions_deliverable ON decisions(deliverable_id)"),
    ("0030_ux_decisions_key",
     "CREATE UNIQUE INDEX IF NOT EXISTS ux_decisions_key "
     "ON decisions(decision_key) WHERE decision_key IS NOT NULL"),
    ("0031_ix_decisions_kind",
     "CREATE INDEX IF NOT EXISTS ix_decisions_kind ON decisions(decision_kind)"),
]


def is_duplicate_column(exc: BaseException) -> bool:
    """True only for SQLite's benign 'duplicate column name' error on ADD COLUMN."""
    return (isinstance(exc, sqlite3.OperationalError)
            and "duplicate column name" in str(exc).lower())


def _column_exists(c: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column
               for row in c.execute(f"PRAGMA table_info({table})").fetchall())


def _applied_migrations(c: sqlite3.Connection) -> set[str]:
    return {row["name"]
            for row in c.execute("SELECT name FROM schema_migrations").fetchall()}


def _record(c: sqlite3.Connection, name: str) -> None:
    c.execute("INSERT OR IGNORE INTO schema_migrations(name, applied_at) VALUES (?, ?)",
              (name, time.time()))


def run_additive_migrations(c: sqlite3.Connection) -> List[str]:
    """Apply every pending additive migration once; return the names newly applied.

    Fails loudly on any error that is not a benign already-present column. Safe to call on
    every startup: already-applied migrations and pre-existing columns are no-ops. The
    caller supplies the connection so this stays Layer-0 pure (see db.schema).
    """
    applied = _applied_migrations(c)
    newly: List[str] = []

    for name, table, column, ddl in ADDITIVE_COLUMN_MIGRATIONS:
        if name in applied:
            continue
        if _column_exists(c, table, column):
            # Legacy DB already carries this column (added before the ledger existed, or by
            # a concurrent writer). Reconcile the ledger without touching the schema.
            _record(c, name)
            continue
        try:
            c.execute(ddl)
        except sqlite3.OperationalError as exc:
            # Only a duplicate-column race is tolerated; disk, lock, permission, corruption,
            # and syntax failures propagate and fail the startup that ran this migration.
            if not is_duplicate_column(exc):
                raise
        _record(c, name)
        newly.append(name)

    for name, sql in DDL_MIGRATIONS:
        if name in applied:
            continue
        c.execute(sql)
        _record(c, name)
        newly.append(name)

    return newly

# Backward-compatible alias for BUG-47 tests that import the private name.
_is_duplicate_column = is_duplicate_column
