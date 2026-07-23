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
    # CO-9 — durable, explainable hybrid Agent Host placement.
    ("0032_wake_intents_placement_json", "wake_intents", "placement_json",
     "ALTER TABLE wake_intents ADD COLUMN placement_json TEXT NOT NULL DEFAULT '{}'"),
    # COORD-18 review remediation — bind verdicts to the authenticated identity,
    # not only the caller-selected agent label. Historical backfills remain NULL.
    ("0037_review_verdicts_reviewer_principal_id", "review_verdicts",
     "reviewer_principal_id",
     "ALTER TABLE review_verdicts ADD COLUMN reviewer_principal_id TEXT"),
    # COORD-19 — durable authority identity and timestamp for finding waivers/overrides.
    ("0038_review_findings_resolved_principal_id", "review_findings",
     "resolved_principal_id",
     "ALTER TABLE review_findings ADD COLUMN resolved_principal_id TEXT"),
    ("0039_review_findings_resolved_at", "review_findings", "resolved_at",
     "ALTER TABLE review_findings ADD COLUMN resolved_at REAL"),
    # COORD-20 — force adversarial re-review after concurrency/lease findings.
    ("0040_review_verdicts_review_mode", "review_verdicts", "review_mode",
     "ALTER TABLE review_verdicts ADD COLUMN review_mode TEXT NOT NULL DEFAULT 'standard'"),
    ("0053_agent_host_enrollments_completion_recovery_hash",
     "agent_host_enrollments", "completion_recovery_hash",
     "ALTER TABLE agent_host_enrollments ADD COLUMN completion_recovery_hash TEXT"),
    ("0054_agent_host_enrollments_completion_recovery_expires_at",
     "agent_host_enrollments", "completion_recovery_expires_at",
     "ALTER TABLE agent_host_enrollments ADD COLUMN completion_recovery_expires_at REAL"),
    ("0057_agent_host_enrollments_completion_finalized_at",
     "agent_host_enrollments", "completion_finalized_at",
     "ALTER TABLE agent_host_enrollments ADD COLUMN completion_finalized_at REAL"),
    ("0060_personal_execution_connections_host_principal_id",
     "personal_execution_connections", "host_principal_id",
     "ALTER TABLE personal_execution_connections "
     "ADD COLUMN host_principal_id TEXT NOT NULL DEFAULT ''"),
    ("0061_agent_host_enrollments_execution_policy_json",
     "agent_host_enrollments", "execution_policy_json",
     "ALTER TABLE agent_host_enrollments "
     "ADD COLUMN execution_policy_json TEXT NOT NULL DEFAULT '{}'"),
    # BUG-89 — terminal wake history remains auditable but leaves hot Fleet/MCP reads.
    ("0066_wake_intents_archived_at", "wake_intents", "archived_at",
     "ALTER TABLE wake_intents ADD COLUMN archived_at REAL"),
    ("0076_task_claims_runner_session_id", "task_claims", "runner_session_id",
     "ALTER TABLE task_claims ADD COLUMN runner_session_id TEXT"),
    ("0077_task_claims_execution_generation", "task_claims", "execution_generation",
     "ALTER TABLE task_claims ADD COLUMN execution_generation INTEGER"),
    ("0078_task_claims_execution_role", "task_claims", "execution_role",
     "ALTER TABLE task_claims ADD COLUMN execution_role TEXT"),
    ("0079_task_claims_lease_epoch", "task_claims", "lease_epoch",
     "ALTER TABLE task_claims ADD COLUMN lease_epoch INTEGER"),
    ("0080_work_sessions_runner_session_id", "work_sessions", "runner_session_id",
     "ALTER TABLE work_sessions ADD COLUMN runner_session_id TEXT"),
    ("0081_work_sessions_execution_generation", "work_sessions", "execution_generation",
     "ALTER TABLE work_sessions ADD COLUMN execution_generation INTEGER"),
    ("0082_work_sessions_execution_role", "work_sessions", "execution_role",
     "ALTER TABLE work_sessions ADD COLUMN execution_role TEXT"),
    ("0083_work_sessions_lease_epoch", "work_sessions", "lease_epoch",
     "ALTER TABLE work_sessions ADD COLUMN lease_epoch INTEGER"),
]

# Idempotent DDL migrations (``CREATE ... IF NOT EXISTS``) applied after the column set,
# once each, recorded in the same ledger. (name, sql).
DDL_MIGRATIONS: List[Tuple[str, str]] = [
    ("0062_ingest_operations",
     "CREATE TABLE IF NOT EXISTS ingest_operations ("
     "idem_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, status TEXT NOT NULL, "
     "response_json TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)"),
    ("0063_dedupe_inbox_source_external",
     "DELETE FROM inbox WHERE external_id IS NOT NULL AND external_id <> '' "
     "AND id NOT IN (SELECT MIN(id) FROM inbox WHERE external_id IS NOT NULL "
     "AND external_id <> '' GROUP BY source, external_id)"),
    ("0064_ux_inbox_source_external",
     "CREATE UNIQUE INDEX IF NOT EXISTS ux_inbox_source_external "
     "ON inbox(source, external_id) WHERE external_id IS NOT NULL AND external_id <> ''"),
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
    # COORD-18 — durable, SHA-fenced code-review verdicts and queryable findings.
    ("0033_review_verdicts",
     "CREATE TABLE IF NOT EXISTS review_verdicts ("
     "verdict_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, pr_url TEXT NOT NULL, "
     "head_sha TEXT NOT NULL, reviewer_principal TEXT NOT NULL, status TEXT NOT NULL, "
     "source TEXT NOT NULL DEFAULT 'review_command', created_at REAL NOT NULL, "
     "recorded_at REAL NOT NULL, UNIQUE(task_id, head_sha))"),
    ("0034_review_findings",
     "CREATE TABLE IF NOT EXISTS review_findings ("
     "verdict_id TEXT NOT NULL, task_id TEXT NOT NULL, finding_id TEXT NOT NULL, "
     "location TEXT NOT NULL, category TEXT NOT NULL, severity TEXT NOT NULL, "
     "invariant_violated TEXT NOT NULL, repair_requirement TEXT NOT NULL, "
     "finding_class TEXT NOT NULL, state TEXT NOT NULL, resolved_by TEXT, "
     "resolved_principal_id TEXT, resolved_reason TEXT, resolved_sha TEXT, "
     "resolved_at REAL, created_at REAL NOT NULL, "
     "updated_at REAL NOT NULL, PRIMARY KEY(verdict_id, finding_id))"),
    ("0035_ix_review_verdicts_task",
     "CREATE INDEX IF NOT EXISTS ix_review_verdicts_task "
     "ON review_verdicts(task_id, created_at)"),
    ("0036_ix_review_findings_task_state",
     "CREATE INDEX IF NOT EXISTS ix_review_findings_task_state "
     "ON review_findings(task_id, state, finding_id)"),
    ("0041_review_remediations",
     "CREATE TABLE IF NOT EXISTS review_remediations ("
     "remediation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
     "verdict_id TEXT NOT NULL UNIQUE, source_head_sha TEXT NOT NULL, "
     "source_pr_url TEXT NOT NULL, round_no INTEGER NOT NULL, status TEXT NOT NULL, "
     "acceptance_criteria_json TEXT NOT NULL DEFAULT '[]', "
     "escalation_findings_json TEXT NOT NULL DEFAULT '[]', "
     "original_exit_criteria TEXT, previous_status TEXT, previous_assignee TEXT, "
     "worker_runtime TEXT, wake_id TEXT, requires_adversarial_review INTEGER NOT NULL DEFAULT 0, "
     "human_intervention_required INTEGER NOT NULL DEFAULT 0, "
     "resolved_without_human INTEGER NOT NULL DEFAULT 0, resolved_head_sha TEXT, "
     "auto_finding_count INTEGER NOT NULL DEFAULT 0, "
     "escalate_finding_count INTEGER NOT NULL DEFAULT 0, save_counted INTEGER NOT NULL DEFAULT 0, "
     "decision_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, resolved_at REAL)"),
    ("0042_ix_review_remediations_task",
     "CREATE INDEX IF NOT EXISTS ix_review_remediations_task "
     "ON review_remediations(task_id, round_no)"),
    ("0043_ix_review_remediations_status",
     "CREATE INDEX IF NOT EXISTS ix_review_remediations_status "
     "ON review_remediations(status, updated_at)"),
    # SESSION-15: durable preflight prediction→outcome learning loop
    ("0044_preflight_runs",
     "CREATE TABLE IF NOT EXISTS preflight_runs ("
     "run_id TEXT PRIMARY KEY, task_id TEXT, work_session_id TEXT, claim_id TEXT, "
     "agent_id TEXT, head_sha TEXT NOT NULL, base_sha TEXT, branch TEXT, "
     "repo_role TEXT NOT NULL DEFAULT 'canonical', repo_path TEXT, "
     "verdict TEXT NOT NULL, ok INTEGER NOT NULL DEFAULT 0, "
     "finding_count INTEGER NOT NULL DEFAULT 0, "
     "blocking_count INTEGER NOT NULL DEFAULT 0, "
     "source TEXT NOT NULL, actor TEXT NOT NULL, "
     "created_at REAL NOT NULL)"),
    ("0045_ix_preflight_runs_task_head",
     "CREATE INDEX IF NOT EXISTS ix_preflight_runs_task_head "
     "ON preflight_runs(task_id, head_sha, created_at)"),
    ("0046_ix_preflight_runs_session",
     "CREATE INDEX IF NOT EXISTS ix_preflight_runs_session "
     "ON preflight_runs(work_session_id, created_at)"),
    ("0047_preflight_findings",
     "CREATE TABLE IF NOT EXISTS preflight_findings ("
     "run_id TEXT NOT NULL, finding_seq INTEGER NOT NULL, "
     "code TEXT NOT NULL, failure_class TEXT NOT NULL, "
     "severity TEXT NOT NULL, blocking INTEGER NOT NULL DEFAULT 0, "
     "message TEXT NOT NULL, remediation TEXT NOT NULL DEFAULT '', "
     "details_json TEXT NOT NULL DEFAULT '{}', "
     "PRIMARY KEY(run_id, finding_seq))"),
    ("0048_ix_preflight_findings_code",
     "CREATE INDEX IF NOT EXISTS ix_preflight_findings_code "
     "ON preflight_findings(code, blocking)"),
    # BUG-75 — durable PTY capability-ticket revocation until JWT expiry.
    ("0049_runner_pty_revoked_jtis",
     "CREATE TABLE IF NOT EXISTS runner_pty_revoked_jtis ("
     "jti TEXT PRIMARY KEY, "
     "expires_at REAL NOT NULL, "
     "revoked_at REAL NOT NULL)"),
    ("0050_ix_runner_pty_revoked_jtis_expires",
     "CREATE INDEX IF NOT EXISTS ix_runner_pty_revoked_jtis_expires "
     "ON runner_pty_revoked_jtis(expires_at)"),
    # ADAPTER-18 — a previous host bearer is accepted only by the rotation
    # recovery endpoint during a short response-loss window. Values remain hashed.
    ("0051_agent_host_rotation_recovery",
     "CREATE TABLE IF NOT EXISTS agent_host_rotation_recovery ("
     "token_hash TEXT PRIMARY KEY, principal_id TEXT NOT NULL, host_id TEXT NOT NULL, "
     "expires_at REAL NOT NULL, created_at REAL NOT NULL)"),
    ("0052_ix_agent_host_rotation_recovery_principal",
     "CREATE INDEX IF NOT EXISTS ix_agent_host_rotation_recovery_principal "
     "ON agent_host_rotation_recovery(principal_id, expires_at)"),
    # ADAPTER-18 review remediation — personal wakes reserve one durable execution
    # connection at request time and activate it only with the exact live runner.
    ("0055_personal_execution_connections",
     "CREATE TABLE IF NOT EXISTS personal_execution_connections ("
     "execution_connection_id TEXT PRIMARY KEY, wake_id TEXT NOT NULL UNIQUE, "
     "task_id TEXT NOT NULL, claim_id TEXT NOT NULL, work_session_id TEXT NOT NULL, "
     "runner_session_id TEXT NOT NULL, host_id TEXT NOT NULL, "
     "host_principal_id TEXT NOT NULL, agent_id TEXT NOT NULL, "
     "source_sha TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'reserved', "
     "created_at REAL NOT NULL, expires_at REAL NOT NULL, claimed_at REAL, "
     "completed_at REAL, updated_at REAL NOT NULL)"),
    ("0056_ix_personal_execution_connections_status",
     "CREATE INDEX IF NOT EXISTS ix_personal_execution_connections_status "
     "ON personal_execution_connections(status, expires_at)"),
    # A revoked bearer remains useful only as an opaque, endpoint-specific receipt
    # for idempotent revoke/uninstall readback after an ambiguous response.
    ("0058_agent_host_revocation_recovery",
     "CREATE TABLE IF NOT EXISTS agent_host_revocation_recovery ("
     "token_hash TEXT PRIMARY KEY, principal_id TEXT NOT NULL, host_id TEXT NOT NULL, "
     "final_status TEXT NOT NULL, revoked_at REAL NOT NULL, created_at REAL NOT NULL)"),
    ("0059_ix_agent_host_revocation_recovery_principal",
     "CREATE INDEX IF NOT EXISTS ix_agent_host_revocation_recovery_principal "
     "ON agent_host_revocation_recovery(principal_id, host_id)"),
    # UI-27 — the coordinator daemon drains only durable operator-started
    # deliverable/task scopes. No rows means no automatic work.
    ("0062_autopilot_scopes",
     "CREATE TABLE IF NOT EXISTS autopilot_scopes ("
     "scope_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, scope_type TEXT NOT NULL, "
     "deliverable_id TEXT NOT NULL, task_project TEXT NOT NULL DEFAULT '', "
     "task_id TEXT NOT NULL DEFAULT '', runtime TEXT NOT NULL DEFAULT 'codex', "
     "status TEXT NOT NULL DEFAULT 'active', requested_by TEXT NOT NULL, "
     "generation INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, "
     "updated_at REAL NOT NULL, last_tick_at REAL, "
     "last_result_json TEXT NOT NULL DEFAULT '{}')"),
    ("0063_ix_autopilot_scopes_active",
     "CREATE INDEX IF NOT EXISTS ix_autopilot_scopes_active "
     "ON autopilot_scopes(profile_id, status, updated_at)"),
    ("0064_ux_autopilot_scopes_live_target",
     "CREATE UNIQUE INDEX IF NOT EXISTS ux_autopilot_scopes_live_target "
     "ON autopilot_scopes(profile_id, scope_type, deliverable_id, task_project, task_id) "
     "WHERE status IN ('active', 'paused')"),
    # UI-30 — server-side Scope gate approvals (the kickoff record). Advisory
    # until enforcement lands; an empty table means nothing is approved.
    ("0065_kickoff_gates",
     "CREATE TABLE IF NOT EXISTS kickoff_gates ("
     "gate TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending', "
     "version INTEGER NOT NULL DEFAULT 0, approved_by TEXT NOT NULL DEFAULT '', "
     "approved_at REAL, note TEXT NOT NULL DEFAULT '', "
     "updated_at REAL NOT NULL DEFAULT 0)"),
    ("0067_ix_wake_intents_live_recent",
     "CREATE INDEX IF NOT EXISTS ix_wake_intents_live_recent "
     "ON wake_intents(archived_at, status, requested_at DESC, wake_id DESC)"),
    ("0068_ix_wake_intents_task_recent",
     "CREATE INDEX IF NOT EXISTS ix_wake_intents_task_recent "
     "ON wake_intents(task_id, requested_at DESC, wake_id DESC)"),
    ("0069_ix_wake_intents_runtime_recent",
     "CREATE INDEX IF NOT EXISTS ix_wake_intents_runtime_recent "
     "ON wake_intents(json_extract(selector_json, '$.runtime'), "
     "requested_at DESC, wake_id DESC)"),
    ("0070_ix_wake_intents_deliverable_recent",
     "CREATE INDEX IF NOT EXISTS ix_wake_intents_deliverable_recent "
     "ON wake_intents(json_extract(selector_json, '$.deliverable_id'), "
     "requested_at DESC, wake_id DESC)"),
    ("0071_ix_wake_intents_recent",
     "CREATE INDEX IF NOT EXISTS ix_wake_intents_recent "
     "ON wake_intents(requested_at DESC, wake_id DESC)"),
    # BUG-143 — dispatch eligibility is derived from structural link/task state.
    ("0072_remove_deliverable_link_dispatch_eligible",
     "UPDATE deliverable_task_links SET metadata_json = "
     "json_remove(CASE WHEN json_valid(metadata_json) THEN metadata_json ELSE '{}' END, "
     "'$.dispatch_eligible') WHERE metadata_json LIKE '%dispatch_eligible%'"),
    ("0073_remove_deliverable_dispatch_eligible",
     "UPDATE deliverables SET metadata_json = "
     "json_remove(CASE WHEN json_valid(metadata_json) THEN metadata_json ELSE '{}' END, "
     "'$.dispatch_eligible') WHERE metadata_json LIKE '%dispatch_eligible%'"),
    # SIMPLIFY-14 — Task Execution owns completion as an append-only transition
    # log.  This is deliberately not a completion-run/state-machine table: each
    # row is evidence for one phase of the existing task execution identity.
    ("0074_task_execution_completion_phases",
     "CREATE TABLE IF NOT EXISTS task_execution_completion_phases ("
     "transition_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
     "pr_number INTEGER NOT NULL, head_sha TEXT NOT NULL, "
     "runner_generation INTEGER NOT NULL, phase TEXT NOT NULL, "
     "outcome TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}', "
     "failure_json TEXT NOT NULL DEFAULT '{}', actor TEXT NOT NULL, "
     "transitioned_at REAL NOT NULL, "
     "UNIQUE(task_id, pr_number, head_sha, runner_generation, phase))"),
    ("0075_ix_task_execution_completion_identity",
     "CREATE INDEX IF NOT EXISTS ix_task_execution_completion_identity "
     "ON task_execution_completion_phases("
     "task_id, pr_number, head_sha, runner_generation, transitioned_at DESC)"),
]


def is_duplicate_column(exc: BaseException) -> bool:
    """True only for SQLite's benign 'duplicate column name' error on ADD COLUMN."""
    return (isinstance(exc, sqlite3.OperationalError)
            and "duplicate column name" in str(exc).lower())


def _column_exists(c: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column
               for row in c.execute(f"PRAGMA table_info({table})").fetchall())


def _table_exists(c: sqlite3.Connection, table: str) -> bool:
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone() is not None


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

    deferred: List[Tuple[str, str, str, str]] = []
    for name, table, column, ddl in ADDITIVE_COLUMN_MIGRATIONS:
        if name in applied:
            continue
        if not _table_exists(c, table):
            # Some feature tables are themselves introduced later in DDL_MIGRATIONS.
            # Defer their additive upgrades until after that create-if-missing pass.
            deferred.append((name, table, column, ddl))
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

    for name, table, column, ddl in deferred:
        if _column_exists(c, table, column):
            _record(c, name)
            continue
        try:
            c.execute(ddl)
        except sqlite3.OperationalError as exc:
            if not is_duplicate_column(exc):
                raise
        _record(c, name)
        newly.append(name)

    return newly

# Backward-compatible alias for BUG-47 tests that import the private name.
_is_duplicate_column = is_duplicate_column
