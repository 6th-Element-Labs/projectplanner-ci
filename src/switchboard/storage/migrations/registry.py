"""Additive migrations for the shared ``project_registry.db`` (ACCESS-18).

Unlike per-board ``schema_migrations`` (BUG-47 / ARCH-MS-28), the registry uses its own
ledger table so lifecycle columns can be applied once across web and MCP processes.
Migrations are forward-only ADD COLUMN operations: older code ignores new columns safely.
"""
from __future__ import annotations

import sqlite3
import time
from typing import List, Tuple

from constants import BUILTIN_PROJECTS

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
    ("access23_projects_replacement_board_id", "projects", "replacement_board_id",
     "ALTER TABLE projects ADD COLUMN replacement_board_id TEXT", ""),
    ("access23_projects_replacement_mission_id", "projects", "replacement_mission_id",
     "ALTER TABLE projects ADD COLUMN replacement_mission_id TEXT", ""),
    ("access23_projects_replacement_consolidation_id", "projects",
     "replacement_consolidation_id",
     "ALTER TABLE projects ADD COLUMN replacement_consolidation_id TEXT", ""),
    ("access18_projects_updated_at", "projects", "updated_at",
     "ALTER TABLE projects ADD COLUMN updated_at REAL", ""),
    ("access18_projects_updated_by", "projects", "updated_by",
     "ALTER TABLE projects ADD COLUMN updated_by TEXT", ""),
    ("access24_projects_purged_at", "projects", "purged_at",
     "ALTER TABLE projects ADD COLUMN purged_at REAL", ""),
    ("access24_projects_purge_intent_id", "projects", "purge_intent_id",
     "ALTER TABLE projects ADD COLUMN purge_intent_id TEXT", ""),
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


def _backfill_protected_system_projects(
        c: sqlite3.Connection, *, enforce_protection: bool) -> None:
    """Project configured system homes into the registry without clobbering edits.

    ``BUILTIN_PROJECTS`` remains the deployment/bootstrap compatibility input for
    database and seed paths.  Lifecycle reads and mutations use the registry rows
    created here, so no downstream operation needs to compare customer project ids.

    Path reconciliation intentionally runs on every registry initialization: env
    overrides may change between deployments.  Protection and active-state repair
    run only when the migration is first applied, so a later separately governed
    migration can deliberately remove protection without bootstrap undoing it.
    Editable label/pretitle and audit metadata are always preserved.
    """
    now = time.time()
    migration_actor = "migration:access22-protected-system-projects"
    for project_id, config in BUILTIN_PROJECTS.items():
        row = c.execute(
            "SELECT db_path, seed_path, lifecycle_status, archived_at, archived_by, "
            "archive_reason, is_protected, is_system FROM projects WHERE id=?",
            (project_id,),
        ).fetchone()
        if row is None:
            c.execute(
                "INSERT OR IGNORE INTO projects("
                "id, label, pretitle, db_path, seed_path, created_at, created_by, "
                "lifecycle_status, is_protected, is_system, updated_at, updated_by"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, config["label"], config.get("pretitle") or "",
                 config["db"], config.get("seed"), now, migration_actor,
                 "active", 1, 1, now, migration_actor),
            )
            row = c.execute(
                "SELECT db_path, seed_path, lifecycle_status, archived_at, archived_by, "
                "archive_reason, is_protected, is_system FROM projects WHERE id=?",
                (project_id,),
            ).fetchone()

        expected = {"db_path": config["db"], "seed_path": config.get("seed")}
        if enforce_protection:
            expected.update({
                "lifecycle_status": "active",
                "archived_at": None,
                "archived_by": None,
                "archive_reason": None,
                "is_protected": 1,
                "is_system": 1,
            })
        current = dict(row)
        if all(current.get(key) == value for key, value in expected.items()):
            continue
        if enforce_protection:
            c.execute(
                "UPDATE projects SET db_path=?, seed_path=?, lifecycle_status='active', "
                "archived_at=NULL, archived_by=NULL, archive_reason=NULL, "
                "is_protected=1, is_system=1, updated_at=?, updated_by=? WHERE id=?",
                (config["db"], config.get("seed"), now, migration_actor, project_id),
            )
        else:
            c.execute(
                "UPDATE projects SET db_path=?, seed_path=?, updated_at=?, updated_by=? "
                "WHERE id=?",
                (config["db"], config.get("seed"), now, migration_actor, project_id),
            )


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

    consolidation_migration = "access23_project_consolidations"
    if consolidation_migration not in done:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS project_consolidations (
                consolidation_id          TEXT PRIMARY KEY,
                source_project_id         TEXT NOT NULL,
                replacement_project_id    TEXT NOT NULL,
                replacement_board_id      TEXT,
                replacement_mission_id    TEXT,
                replacement_deliverable_id TEXT,
                status                    TEXT NOT NULL,
                plan_hash                 TEXT NOT NULL UNIQUE,
                impact_report_hash        TEXT NOT NULL,
                plan_json                 TEXT NOT NULL,
                history_json              TEXT NOT NULL,
                routing_json              TEXT NOT NULL,
                rollback_json             TEXT NOT NULL,
                actor                     TEXT NOT NULL,
                reason                    TEXT NOT NULL,
                approved_by               TEXT NOT NULL,
                approved_at               REAL NOT NULL,
                created_at                REAL NOT NULL,
                applied_at                REAL,
                verified_at               REAL,
                rolled_back_at            REAL,
                rollback_reason           TEXT,
                rollback_actor            TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_project_consolidations_source
                ON project_consolidations(source_project_id, created_at, consolidation_id);
            CREATE INDEX IF NOT EXISTS ix_project_consolidations_replacement
                ON project_consolidations(replacement_project_id, created_at, consolidation_id);
            """
        )
        _record(c, consolidation_migration)
        newly.append(consolidation_migration)

    protected_records_migration = "access22_protected_system_project_records"
    first_protected_backfill = protected_records_migration not in done
    _backfill_protected_system_projects(c, enforce_protection=first_protected_backfill)
    if first_protected_backfill:
        _record(c, protected_records_migration)
        newly.append(protected_records_migration)

    purge_migration = "access24_project_purge"
    if purge_migration not in done:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS project_purge_intents (
                intent_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                status TEXT NOT NULL,
                intent_hash TEXT NOT NULL UNIQUE,
                impact_report_hash TEXT NOT NULL,
                export_uri TEXT NOT NULL,
                export_hash TEXT NOT NULL,
                export_created_at REAL NOT NULL,
                retention_days INTEGER NOT NULL,
                intent_json TEXT NOT NULL,
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at REAL NOT NULL,
                verified_by TEXT,
                verified_at REAL,
                executed_by TEXT,
                executed_at REAL,
                failure_json TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_project_purge_intents_project
                ON project_purge_intents(project_id, created_at, intent_id);
            CREATE TABLE IF NOT EXISTS project_purge_tombstones (
                tombstone_id TEXT PRIMARY KEY,
                intent_id TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL,
                registry_record_json TEXT NOT NULL,
                audit_receipt_json TEXT NOT NULL,
                database_path_hash TEXT NOT NULL,
                database_removed INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_cleanup_reviews (
                review_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                impact_report_hash TEXT NOT NULL,
                impact_receipt_json TEXT NOT NULL,
                approved_by TEXT NOT NULL,
                approved_at REAL NOT NULL,
                rationale TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(project_id, impact_report_hash)
            );
            CREATE INDEX IF NOT EXISTS ix_project_cleanup_reviews_project
                ON project_cleanup_reviews(project_id, created_at, review_id);
            """
        )
        _record(c, purge_migration)
        newly.append(purge_migration)

    provider_vault_migration = "co6_provider_credential_vault"
    if provider_vault_migration not in done:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS provider_connections (
                credential_reference TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_account_id TEXT NOT NULL,
                auth_type TEXT NOT NULL,
                project_allowlist_json TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL,
                refresh_state TEXT NOT NULL,
                revocation_state TEXT NOT NULL,
                concurrency_policy_json TEXT NOT NULL,
                expires_at REAL,
                credential_version INTEGER NOT NULL,
                encrypted_credential BLOB,
                credential_nonce BLOB,
                key_id TEXT,
                audit_provenance_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                created_by TEXT NOT NULL,
                rotated_at REAL,
                rotated_by TEXT,
                revoked_at REAL,
                revoked_by TEXT,
                revocation_reason TEXT,
                deleted_at REAL,
                deleted_by TEXT,
                deletion_reason TEXT,
                updated_at REAL NOT NULL,
                updated_by TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_provider_connections_live_account
                ON provider_connections(tenant_id, user_id, provider, provider_account_id)
                WHERE lifecycle_state IN ('active', 'expired');
            CREATE INDEX IF NOT EXISTS ix_provider_connections_tenant_user
                ON provider_connections(tenant_id, user_id, provider, lifecycle_state);

            CREATE TABLE IF NOT EXISTS provider_credential_leases (
                lease_id TEXT PRIMARY KEY,
                credential_reference TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_account_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                host_id TEXT NOT NULL,
                runner_session_id TEXT NOT NULL,
                work_session_id TEXT NOT NULL,
                credential_version INTEGER NOT NULL,
                state TEXT NOT NULL,
                acquired_at REAL NOT NULL,
                acquired_by TEXT NOT NULL,
                expires_at REAL NOT NULL,
                released_at REAL,
                released_by TEXT,
                release_reason TEXT,
                FOREIGN KEY(credential_reference)
                    REFERENCES provider_connections(credential_reference)
            );
            CREATE INDEX IF NOT EXISTS ix_provider_credential_leases_active
                ON provider_credential_leases(credential_reference, state, expires_at);
            CREATE INDEX IF NOT EXISTS ix_provider_credential_leases_binding
                ON provider_credential_leases(
                    project_id, task_id, host_id, runner_session_id, work_session_id, state
                );

            CREATE TABLE IF NOT EXISTS provider_credential_events (
                event_id TEXT PRIMARY KEY,
                credential_reference TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_account_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                project_id TEXT,
                task_id TEXT,
                host_id TEXT,
                runner_session_id TEXT,
                work_session_id TEXT,
                lease_id TEXT,
                reason_code TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_provider_credential_events_reference
                ON provider_credential_events(credential_reference, created_at, event_id);
            CREATE INDEX IF NOT EXISTS ix_provider_credential_events_tenant
                ON provider_credential_events(tenant_id, user_id, created_at, event_id);
            """
        )
        _record(c, provider_vault_migration)
        newly.append(provider_vault_migration)

    provider_lease_state_migration = "co6_provider_credential_lease_state_machine"
    if provider_lease_state_migration not in done:
        c.executescript(
            """
            ALTER TABLE provider_credential_leases
                ADD COLUMN acquiring_principal_id TEXT NOT NULL DEFAULT '';
            ALTER TABLE provider_credential_leases
                ADD COLUMN acquiring_principal_kind TEXT NOT NULL DEFAULT 'system';
            ALTER TABLE provider_credential_leases
                ADD COLUMN acquiring_principal_scopes_json TEXT NOT NULL DEFAULT '[]';
            ALTER TABLE provider_credential_leases
                ADD COLUMN acquiring_principal_admin INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE provider_credential_leases
                ADD COLUMN materializing_at REAL;
            ALTER TABLE provider_credential_leases
                ADD COLUMN activated_at REAL;
            """
        )
        _record(c, provider_lease_state_migration)
        newly.append(provider_lease_state_migration)

    provider_capacity_migration = "co8_subscription_capacity_state_machine"
    if provider_capacity_migration not in done:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS provider_capacity_accounts (
                credential_reference TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_account_id TEXT NOT NULL,
                state TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                retry_after_seconds INTEGER,
                reset_at REAL,
                next_poll_at REAL,
                cooldown_until REAL,
                poll_attempts INTEGER NOT NULL DEFAULT 0,
                poll_window_started_at REAL,
                state_version INTEGER NOT NULL DEFAULT 1,
                observed_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                updated_by TEXT NOT NULL,
                FOREIGN KEY(credential_reference)
                    REFERENCES provider_connections(credential_reference)
            );
            CREATE INDEX IF NOT EXISTS ix_provider_capacity_accounts_identity
                ON provider_capacity_accounts(
                    tenant_id, user_id, provider, provider_account_id, state, next_poll_at
                );

            CREATE TABLE IF NOT EXISTS provider_capacity_checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                credential_reference TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_account_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                claim_id TEXT NOT NULL,
                host_id TEXT NOT NULL,
                runner_session_id TEXT NOT NULL,
                work_session_id TEXT NOT NULL,
                state TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                status TEXT NOT NULL,
                checkpoint_json TEXT NOT NULL DEFAULT '{}',
                retry_after_seconds INTEGER,
                reset_at REAL,
                next_retry_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                resumed_at REAL,
                UNIQUE(
                    credential_reference, project_id, task_id, claim_id, work_session_id
                ),
                FOREIGN KEY(credential_reference)
                    REFERENCES provider_connections(credential_reference)
            );
            CREATE INDEX IF NOT EXISTS ix_provider_capacity_checkpoints_resume
                ON provider_capacity_checkpoints(
                    credential_reference, status, next_retry_at, task_id
                );

            CREATE TABLE IF NOT EXISTS provider_capacity_polls (
                poll_id TEXT PRIMARY KEY,
                credential_reference TEXT NOT NULL,
                idem_key TEXT NOT NULL,
                state_version INTEGER NOT NULL,
                attempt INTEGER NOT NULL,
                status TEXT NOT NULL,
                requested_at REAL NOT NULL,
                lease_expires_at REAL NOT NULL,
                completed_at REAL,
                receipt_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(credential_reference, idem_key),
                FOREIGN KEY(credential_reference)
                    REFERENCES provider_connections(credential_reference)
            );
            CREATE INDEX IF NOT EXISTS ix_provider_capacity_polls_account
                ON provider_capacity_polls(credential_reference, requested_at, poll_id);

            CREATE TABLE IF NOT EXISTS provider_capacity_events (
                event_id TEXT PRIMARY KEY,
                credential_reference TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_account_id TEXT NOT NULL,
                project_id TEXT,
                task_id TEXT,
                claim_id TEXT,
                work_session_id TEXT,
                state TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                actor TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                FOREIGN KEY(credential_reference)
                    REFERENCES provider_connections(credential_reference)
            );
            CREATE INDEX IF NOT EXISTS ix_provider_capacity_events_account
                ON provider_capacity_events(credential_reference, created_at, event_id);
            CREATE INDEX IF NOT EXISTS ix_provider_capacity_events_task
                ON provider_capacity_events(project_id, task_id, created_at, event_id);
            """
        )
        _record(c, provider_capacity_migration)
        newly.append(provider_capacity_migration)

    ownership_migration = "enforce8_execution_connection_ownership"
    if ownership_migration not in done:
        c.executescript(
            """
            ALTER TABLE provider_connections
                ADD COLUMN connection_kind TEXT NOT NULL DEFAULT 'personal_subscription';
            ALTER TABLE provider_connections
                ADD COLUMN billing_account_id TEXT NOT NULL DEFAULT '';
            ALTER TABLE provider_connections
                ADD COLUMN budget_policy_json TEXT NOT NULL DEFAULT '{}';
            ALTER TABLE provider_connections
                ADD COLUMN host_allowlist_json TEXT NOT NULL DEFAULT '[]';
            ALTER TABLE provider_connections
                ADD COLUMN ownership_proof_json TEXT NOT NULL DEFAULT '{}';
            ALTER TABLE provider_connections
                ADD COLUMN materialization_mode TEXT NOT NULL DEFAULT 'vault_envelope';

            ALTER TABLE provider_credential_leases
                ADD COLUMN execution_connection_id TEXT NOT NULL DEFAULT '';
            ALTER TABLE provider_credential_leases
                ADD COLUMN connection_kind TEXT NOT NULL DEFAULT 'personal_subscription';
            ALTER TABLE provider_credential_leases
                ADD COLUMN billing_account_id TEXT NOT NULL DEFAULT '';
            ALTER TABLE provider_credential_leases
                ADD COLUMN claim_id TEXT NOT NULL DEFAULT '';
            ALTER TABLE provider_credential_leases
                ADD COLUMN wake_id TEXT NOT NULL DEFAULT '';
            ALTER TABLE provider_credential_leases
                ADD COLUMN account_affinity_id TEXT NOT NULL DEFAULT '';

            ALTER TABLE provider_credential_events
                ADD COLUMN execution_connection_id TEXT NOT NULL DEFAULT '';
            ALTER TABLE provider_credential_events
                ADD COLUMN connection_kind TEXT NOT NULL DEFAULT 'personal_subscription';
            ALTER TABLE provider_credential_events
                ADD COLUMN claim_id TEXT NOT NULL DEFAULT '';
            ALTER TABLE provider_credential_events
                ADD COLUMN wake_id TEXT NOT NULL DEFAULT '';
            CREATE INDEX IF NOT EXISTS ix_provider_credential_leases_execution_binding
                ON provider_credential_leases(
                    execution_connection_id, project_id, task_id, claim_id, work_session_id,
                    runner_session_id, host_id, wake_id, state
                );
            """
        )
        _record(c, ownership_migration)
        newly.append(ownership_migration)

    return newly
