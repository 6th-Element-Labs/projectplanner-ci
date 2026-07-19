#!/usr/bin/env python3
"""BUG-90: data/additive migrations precede indexes that depend on them."""
from __future__ import annotations

import sqlite3

from path_setup import ROOT  # noqa: F401
from db.schema import apply_schema


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


# Reproduce the two independent production upgrade hazards: duplicated legacy inbox
# identities and a wake_intents table created before archived_at existed.
c = sqlite3.connect(":memory:")
c.row_factory = sqlite3.Row
c.executescript(
    """
    CREATE TABLE inbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT, external_id TEXT, sender TEXT, subject TEXT,
        summary TEXT, triage TEXT, status TEXT DEFAULT 'pending',
        received_at REAL, created_at REAL
    );
    INSERT INTO inbox(source, external_id, subject) VALUES ('github', 'delivery-1', 'first');
    INSERT INTO inbox(source, external_id, subject) VALUES ('github', 'delivery-1', 'duplicate');

    CREATE TABLE wake_intents (
        wake_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        reason TEXT NOT NULL,
        selector_json TEXT NOT NULL DEFAULT '{}',
        policy_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending',
        requested_at REAL NOT NULL,
        deadline REAL,
        claimed_at REAL,
        claimed_by_host TEXT,
        completed_at REAL,
        runner_session_id TEXT,
        agent_id TEXT,
        result_json TEXT NOT NULL DEFAULT '{}',
        task_id TEXT,
        principal_id TEXT,
        idem_key TEXT
    );
    INSERT INTO wake_intents(wake_id, source, reason, requested_at)
    VALUES ('wake-legacy', 'test', 'legacy fixture', 1);
    """
)

apply_schema(c)

duplicate_count = c.execute(
    "SELECT COUNT(*) n FROM inbox WHERE source='github' AND external_id='delivery-1'"
).fetchone()["n"]
ok(duplicate_count == 1,
   "legacy inbox duplicates are repaired before the unique index is created")

columns = {row["name"] for row in c.execute("PRAGMA table_info(wake_intents)").fetchall()}
ok("archived_at" in columns,
   "legacy wake_intents receives archived_at before history indexes are created")

indexes = {row["name"] for row in c.execute(
    "SELECT name FROM sqlite_master WHERE type='index'"
).fetchall()}
expected = {
    "ux_inbox_source_external", "ix_wake_intents_live_recent",
    "ix_wake_intents_recent", "ix_wake_intents_task_recent",
    "ix_wake_intents_runtime_recent", "ix_wake_intents_deliverable_recent",
}
ok(expected <= indexes,
   "all deferred indexes exist after their data and column migrations")

ledger = {row["name"] for row in c.execute(
    "SELECT name FROM schema_migrations"
).fetchall()}
ok({"0063_dedupe_inbox_source_external", "0064_ux_inbox_source_external",
    "0066_wake_intents_archived_at", "0067_ix_wake_intents_live_recent"} <= ledger,
   "the migration ledger records the ordered legacy upgrade")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
