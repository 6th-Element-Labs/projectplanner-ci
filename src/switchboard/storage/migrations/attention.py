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

ATTENTION_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_attention_requests_queue "
    "ON attention_requests(project_id, status, expires_at, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_attention_requests_binding "
    "ON attention_requests(project_id, task_id, host_id, runner_session_id, work_session_id)",
    "CREATE INDEX IF NOT EXISTS ix_attention_decisions_request "
    "ON attention_decisions(request_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_attention_events_request "
    "ON attention_events(request_id, sequence)",
)


def upgrade_attention_schema(connection: sqlite3.Connection) -> None:
    """Create the attention schema; safe to replay."""
    connection.execute(ATTENTION_REQUESTS_SQL)
    connection.execute(ATTENTION_DECISIONS_SQL)
    connection.execute(ATTENTION_EVENTS_SQL)
    for statement in ATTENTION_INDEX_SQL:
        connection.execute(statement)


def downgrade_attention_schema(connection: sqlite3.Connection) -> None:
    """Reverse PROTO-7 in dependency order."""
    connection.execute("DROP TABLE IF EXISTS attention_events")
    connection.execute("DROP TABLE IF EXISTS attention_decisions")
    connection.execute("DROP TABLE IF EXISTS attention_requests")
