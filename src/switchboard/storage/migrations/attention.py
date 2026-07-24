"""PROTO-7 reversible schema migration for durable attention requests."""
from __future__ import annotations

import sqlite3

ATTENTION_REQUESTS_SQL = """
CREATE TABLE IF NOT EXISTS attention_requests (
    request_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_id TEXT,
    provider TEXT NOT NULL,
    host_id TEXT,
    runner_session_id TEXT,
    work_session_id TEXT,
    provider_request_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    prompt TEXT NOT NULL,
    context_json TEXT NOT NULL,
    choices_json TEXT NOT NULL,
    recommended_default_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    version INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    expires_at REAL,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    decided_at REAL,
    delivery_started_at REAL,
    resolved_at REAL,
    terminal_reason TEXT,
    delivery_receipt_json TEXT,
    CHECK (status IN (
        'pending', 'decision_recorded', 'delivering', 'resolved',
        'failed', 'expired', 'cancelled', 'orphaned'
    )),
    UNIQUE(project_id, provider, provider_request_id),
    UNIQUE(project_id, idempotency_key)
)
"""

ATTENTION_DECISIONS_SQL = """
CREATE TABLE IF NOT EXISTS attention_decisions (
    decision_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    request_version INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    decision_hash TEXT NOT NULL,
    choice_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    actor_principal_id TEXT,
    created_at REAL NOT NULL,
    delivery_claimed_by TEXT,
    delivery_claimed_at REAL,
    delivered_at REAL,
    delivery_receipt_json TEXT,
    FOREIGN KEY(request_id) REFERENCES attention_requests(request_id),
    UNIQUE(request_id, idempotency_key)
)
"""

ATTENTION_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS attention_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    request_version INTEGER NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    FOREIGN KEY(request_id) REFERENCES attention_requests(request_id),
    UNIQUE(request_id, sequence)
)
"""

ATTENTION_COMPLETION_WAKES_SQL = """
CREATE TABLE IF NOT EXISTS attention_completion_wakes (
    wake_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    decision_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    deliverable_id TEXT NOT NULL DEFAULT '',
    completion_run_id TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    pr_number INTEGER,
    choice_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL,
    claimed_by TEXT,
    lease_expires_at REAL,
    wake_receipt_json TEXT,
    completion_receipt_json TEXT,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK (status IN (
        'pending', 'claimed', 'accepted', 'failed', 'resolved', 'cancelled'
    )),
    FOREIGN KEY(request_id) REFERENCES attention_requests(request_id),
    FOREIGN KEY(decision_id) REFERENCES attention_decisions(decision_id)
)
"""

ATTENTION_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_attention_requests_queue "
    "ON attention_requests(project_id, status, expires_at, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_attention_requests_binding "
    "ON attention_requests(project_id, task_id, host_id, runner_session_id, work_session_id)",
    "CREATE INDEX IF NOT EXISTS ix_attention_decisions_request "
    "ON attention_decisions(request_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_attention_events_request "
    "ON attention_events(request_id, sequence)",
    "CREATE INDEX IF NOT EXISTS ix_attention_completion_wakes_ready "
    "ON attention_completion_wakes(project_id, status, available_at, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_attention_completion_wakes_task "
    "ON attention_completion_wakes(project_id, task_id, status, created_at)",
)


def upgrade_attention_schema(connection: sqlite3.Connection) -> None:
    """Create the attention schema; safe to replay."""
    connection.execute(ATTENTION_REQUESTS_SQL)
    connection.execute(ATTENTION_DECISIONS_SQL)
    connection.execute(ATTENTION_EVENTS_SQL)
    connection.execute(ATTENTION_COMPLETION_WAKES_SQL)
    for statement in ATTENTION_INDEX_SQL:
        connection.execute(statement)


def downgrade_attention_schema(connection: sqlite3.Connection) -> None:
    """Reverse PROTO-7 in dependency order."""
    connection.execute("DROP TABLE IF EXISTS attention_completion_wakes")
    connection.execute("DROP TABLE IF EXISTS attention_events")
    connection.execute("DROP TABLE IF EXISTS attention_decisions")
    connection.execute("DROP TABLE IF EXISTS attention_requests")


__all__ = [
    "ATTENTION_REQUESTS_SQL",
    "ATTENTION_DECISIONS_SQL",
    "ATTENTION_EVENTS_SQL",
    "ATTENTION_COMPLETION_WAKES_SQL",
    "ATTENTION_INDEX_SQL",
    "upgrade_attention_schema",
    "downgrade_attention_schema",
]
