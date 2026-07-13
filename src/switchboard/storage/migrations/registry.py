"""Additive migrations for the shared ``project_registry.db`` (ACCESS-18).

Unlike per-board ``schema_migrations`` (BUG-47 / ARCH-MS-28), the registry uses its own
ledger table so lifecycle columns can be applied once across web and MCP processes.
Migrations are forward-only ADD COLUMN operations: older code ignores new columns safely.
"""
from __future__ import annotations

import sqlite3
import time
from typing import List, Tuple

# (name, table, column, ddl, backfill_sql)
REGISTRY_COLUMN_MIGRATIONS: List[Tuple[str, str, str, str, str]] = [
    ("access18_projects_lifecycle_status", "projects", "lifecycle_status",
     "ALTER TABLE projects ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'active'",
     "UPDATE projects SET lifecycle_status='active' WHERE lifecycle_status IS NULL OR lifecycle_status=''"),
    ("access18_projects_archived_at", "projects", "archived_at",
     "ALTER TABLE projects ADD COLUMN archived_at REAL", ""),
    ("access18_projects_archived_by", "projects", "archived_by",
     "ALTER TABLE projects ADD COLUMN archived_by TEXT", ""),
    ("access18_projects_archive_reason", "projects", "archive_reason",
     "ALTER TABLE projects ADD COLUMN archive_reason TEXT", ""),
    ("access18_projects_is_protected", "projects", "is_protected",
     "ALTER TABLE projects ADD COLUMN is_protected INTEGER NOT NULL DEFAULT 0",
     "UPDATE projects SET is_protected=0 WHERE is_protected IS NULL"),
    ("access18_projects_is_system", "projects", "is_system",
     "ALTER TABLE projects ADD COLUMN is_system INTEGER NOT NULL DEFAULT 0",
     "UPDATE projects SET is_system=0 WHERE is_system IS NULL"),
    ("access18_projects_replacement_project_id", "projects", "replacement_project_id",
     "ALTER TABLE projects ADD COLUMN replacement_project_id TEXT", ""),
    ("access18_projects_replacement_deliverable_id", "projects", "replacement_deliverable_id",
     "ALTER TABLE projects ADD COLUMN replacement_deliverable_id TEXT", ""),
    ("access18_projects_updated_at", "projects", "updated_at",
     "ALTER TABLE projects ADD COLUMN updated_at REAL", ""),
    ("access18_projects_updated_by", "projects", "updated_by",
     "ALTER TABLE projects ADD COLUMN updated_by TEXT", ""),
    ("access18_project_access_updated_by", "project_access", "updated_by",
     "ALTER TABLE project_access ADD COLUMN updated_by TEXT", ""),
]


def _ensure_ledger(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS registry_migrations (
            name       TEXT PRIMARY KEY,
            applied_at REAL NOT NULL
        )
        """
    )


def _column_exists(c: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column
               for row in c.execute(f"PRAGMA table_info({table})").fetchall())


def _applied(c: sqlite3.Connection) -> set[str]:
    _ensure_ledger(c)
    return {row[0] for row in c.execute("SELECT name FROM registry_migrations").fetchall()}


def _record(c: sqlite3.Connection, name: str) -> None:
    c.execute("INSERT OR IGNORE INTO registry_migrations(name, applied_at) VALUES (?, ?)",
              (name, time.time()))


def run_registry_migrations(c: sqlite3.Connection) -> List[str]:
    """Apply pending registry migrations once; return names newly applied."""
    _ensure_ledger(c)
    done = _applied(c)
    newly: List[str] = []

    for name, table, column, ddl, backfill in REGISTRY_COLUMN_MIGRATIONS:
        if name in done:
            continue
        if _column_exists(c, table, column):
            if backfill:
                c.execute(backfill)
            _record(c, name)
            continue
        c.execute(ddl)
        if backfill:
            c.execute(backfill)
        _record(c, name)
        newly.append(name)

    event_migration = "access20_project_lifecycle_events"
    if event_migration not in done:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS project_lifecycle_events (
                event_id           TEXT PRIMARY KEY,
                project_id         TEXT NOT NULL,
                from_status        TEXT NOT NULL,
                to_status          TEXT NOT NULL,
                actor              TEXT NOT NULL,
                reason             TEXT,
                impact_report_hash TEXT,
                validation_json    TEXT NOT NULL DEFAULT '{}',
                created_at         REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_project_lifecycle_events_project
                ON project_lifecycle_events(project_id, created_at, event_id);
            """
        )
        _record(c, event_migration)
        newly.append(event_migration)

    return newly
