"""Layer-0 database schema: DDL, additive migrations, registry schema, and seeding.

Extracted from store.py (ARCH-4). apply_schema/seed_from_plan take a connection (+ seed
path) so this module stays Layer-0 pure — the project-aware wrappers init_db/seed_if_empty
stay in store.py because they resolve the db/seed path via _conn/_resolve (Layer 1, ARCH-15).
The DDL text is preserved (dedent-only); a fresh DB is schema-identical to master.
"""
import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from constants import *  # noqa: F401,F403
from db.core import *     # noqa: F401,F403
from db.migrations import run_additive_migrations

__all__ = ["apply_schema", "seed_from_plan", "init_project_registry"]


def init_project_registry() -> None:
    with _registry_conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id         TEXT PRIMARY KEY,
                label      TEXT NOT NULL,
                pretitle   TEXT,
                db_path    TEXT NOT NULL,
                seed_path  TEXT,
                created_at REAL NOT NULL,
                created_by TEXT
            );
            CREATE TABLE IF NOT EXISTS orgs (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                slug       TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                created_by TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
                id           TEXT PRIMARY KEY,
                email        TEXT UNIQUE,
                display_name TEXT NOT NULL,
                created_at   REAL NOT NULL,
                disabled_at  REAL
            );
            CREATE TABLE IF NOT EXISTS org_memberships (
                org_id     TEXT NOT NULL,
                user_id    TEXT NOT NULL,
                role       TEXT NOT NULL,
                created_at REAL NOT NULL,
                created_by TEXT,
                PRIMARY KEY (org_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS project_access (
                project_id    TEXT PRIMARY KEY,
                org_id        TEXT NOT NULL,
                owner_user_id TEXT,
                purpose       TEXT,
                boundary      TEXT,
                created_at    REAL NOT NULL,
                created_by    TEXT,
                updated_at    REAL NOT NULL,
                visibility    TEXT               -- 'private' | 'org' (NULL treated as 'org')
            );
            CREATE TABLE IF NOT EXISTS project_role_grants (
                project_id   TEXT NOT NULL,
                subject_kind TEXT NOT NULL,
                subject_id   TEXT NOT NULL,
                role         TEXT NOT NULL,
                scopes       TEXT NOT NULL,
                created_at   REAL NOT NULL,
                created_by   TEXT,
                revoked_at   REAL,
                purpose      TEXT,
                expires_at   REAL,
                PRIMARY KEY (project_id, subject_kind, subject_id, role)
            );
            """
        )
        # Migration: add project_access.visibility to registries created before ACCESS-14.
        cols = [r["name"] for r in c.execute("PRAGMA table_info(project_access)").fetchall()]
        if "visibility" not in cols:
            c.execute("ALTER TABLE project_access ADD COLUMN visibility TEXT")
        # ACCESS-18: lifecycle metadata + registry migration ledger.
        import scripts.switchboard_path  # noqa: F401 — src/switchboard importable
        from switchboard.storage.migrations.registry import run_registry_migrations
        run_registry_migrations(c)


def apply_schema(c):
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            workstream_id TEXT, workstream_name TEXT,
            title TEXT, description TEXT,
            owner_org TEXT, owner_person_or_role TEXT, assignee TEXT,
            phase TEXT, status TEXT DEFAULT 'Not Started',
            effort_days REAL, duration_days INTEGER,
            start_date TEXT, finish_date TEXT, start_day INTEGER,
            depends_on TEXT, entry_criteria TEXT, exit_criteria TEXT, deliverable TEXT,
            risk_level TEXT, is_blocking INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 0,
            created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT, actor TEXT, kind TEXT, payload TEXT, created_at REAL
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        -- UI-27: durable operator-started autopilot scopes. The daemon only
        -- drains rows in active state; an empty table is therefore safely idle.
        CREATE TABLE IF NOT EXISTS autopilot_scopes (
            scope_id        TEXT PRIMARY KEY,
            profile_id      TEXT NOT NULL,
            scope_type      TEXT NOT NULL,
            deliverable_id  TEXT NOT NULL,
            task_project    TEXT NOT NULL DEFAULT '',
            task_id         TEXT NOT NULL DEFAULT '',
            runtime         TEXT NOT NULL DEFAULT 'codex',
            status          TEXT NOT NULL DEFAULT 'active',
            requested_by    TEXT NOT NULL,
            generation      INTEGER NOT NULL DEFAULT 1,
            created_at      REAL NOT NULL,
            updated_at      REAL NOT NULL,
            last_tick_at    REAL,
            last_result_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS ix_autopilot_scopes_active
            ON autopilot_scopes(profile_id, status, updated_at);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_autopilot_scopes_live_target
            ON autopilot_scopes(profile_id, scope_type, deliverable_id, task_project, task_id)
            WHERE status IN ('active', 'paused');
        -- UI-30: the kickoff record — server-side Scope gate approvals. Advisory
        -- until enforcement lands; an empty table means nothing is approved.
        CREATE TABLE IF NOT EXISTS kickoff_gates (
            gate        TEXT PRIMARY KEY,
            status      TEXT NOT NULL DEFAULT 'pending',
            version     INTEGER NOT NULL DEFAULT 0,
            approved_by TEXT NOT NULL DEFAULT '',
            approved_at REAL,
            note        TEXT NOT NULL DEFAULT '',
            updated_at  REAL NOT NULL DEFAULT 0
        );
        -- BUG-47: ledger for numbered additive migrations (db/migrations.py). One row per
        -- applied migration name; the runner uses it to skip already-applied migrations
        -- instead of relying on catch-all exception swallowing to detect "already done".
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name       TEXT PRIMARY KEY,
            applied_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session TEXT, role TEXT, content TEXT, payload TEXT, created_at REAL
        );
        CREATE TABLE IF NOT EXISTS digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL, since_ts REAL, content TEXT, meta TEXT
        );
        CREATE TABLE IF NOT EXISTS rag_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_kind TEXT, label TEXT, text TEXT, embedding TEXT, created_at REAL
        );
        CREATE TABLE IF NOT EXISTS inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT, external_id TEXT, sender TEXT, subject TEXT,
            summary TEXT, triage TEXT, status TEXT DEFAULT 'pending',
            received_at REAL, created_at REAL
        );
        -- The unique inbox index is installed by migration 0064 only after migration 0063
        -- removes legacy duplicates. Creating it here would prevent that repair from running.
        CREATE TABLE IF NOT EXISTS ingest_operations (
            idem_key TEXT PRIMARY KEY,
            request_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            response_json TEXT,
            error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS file_leases (
            id          TEXT PRIMARY KEY,
            agent_id    TEXT NOT NULL,
            task_id     TEXT,
            files       TEXT NOT NULL,
            claimed_at  REAL NOT NULL,
            ttl_minutes INTEGER NOT NULL DEFAULT 30,
            released_at REAL
        );
        CREATE TABLE IF NOT EXISTS decisions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id     TEXT,
            author      TEXT NOT NULL,
            title       TEXT NOT NULL,
            context     TEXT NOT NULL,
            decision    TEXT NOT NULL,
            rationale   TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'accepted',
            supersedes  INTEGER,
            created_at  REAL NOT NULL,
            decision_key TEXT,
            decision_kind TEXT,
            deliverable_id TEXT,
            coordinator_agent_id TEXT,
            inputs_json TEXT,
            policy_rule TEXT,
            chosen_action_json TEXT,
            skipped_alternatives_json TEXT,
            result_json TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_decisions_task ON decisions(task_id);
        CREATE TABLE IF NOT EXISTS agent_messages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            from_agent    TEXT NOT NULL,
            to_agent      TEXT NOT NULL,
            task_id       TEXT,
            message       TEXT NOT NULL,
            requires_ack  INTEGER NOT NULL DEFAULT 0,
            ack_deadline  REAL,
            sent_at       REAL NOT NULL,
            acked_at      REAL,
            ack_response  TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_messages_to ON agent_messages(to_agent, acked_at);
        CREATE TABLE IF NOT EXISTS coordination_monitors (
            id              TEXT PRIMARY KEY,
            kind            TEXT NOT NULL,
            target_type     TEXT NOT NULL,
            target_id       TEXT NOT NULL,
            task_id         TEXT,
            owner_agent     TEXT,
            subject_agent   TEXT,
            status          TEXT NOT NULL DEFAULT 'pending',
            deadline        REAL,
            condition_json  TEXT NOT NULL DEFAULT '{}',
            on_timeout_json TEXT NOT NULL DEFAULT '{}',
            result_json     TEXT NOT NULL DEFAULT '{}',
            created_at      REAL NOT NULL,
            updated_at      REAL NOT NULL,
            last_checked_at REAL,
            fired_at        REAL,
            resolved_at     REAL
        );
        CREATE INDEX IF NOT EXISTS ix_monitors_status
            ON coordination_monitors(status, deadline);
        CREATE INDEX IF NOT EXISTS ix_monitors_target
            ON coordination_monitors(target_type, target_id);
        CREATE TABLE IF NOT EXISTS principals (
            id            TEXT PRIMARY KEY,
            kind          TEXT NOT NULL,
            display_name  TEXT NOT NULL,
            project       TEXT NOT NULL,
            scopes        TEXT NOT NULL,
            token_hash    TEXT NOT NULL,
            created_at    REAL NOT NULL,
            revoked_at    REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_principals_token ON principals(token_hash);
        CREATE TABLE IF NOT EXISTS agent_host_rotation_recovery (
            token_hash   TEXT PRIMARY KEY,
            principal_id TEXT NOT NULL,
            host_id      TEXT NOT NULL,
            expires_at   REAL NOT NULL,
            created_at   REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_agent_host_rotation_recovery_principal
            ON agent_host_rotation_recovery(principal_id, expires_at);
        CREATE TABLE IF NOT EXISTS agent_host_revocation_recovery (
            token_hash   TEXT PRIMARY KEY,
            principal_id TEXT NOT NULL,
            host_id      TEXT NOT NULL,
            final_status TEXT NOT NULL,
            revoked_at   REAL NOT NULL,
            created_at   REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_agent_host_revocation_recovery_principal
            ON agent_host_revocation_recovery(principal_id, host_id);
        CREATE TABLE IF NOT EXISTS principal_passwords (
            login               TEXT PRIMARY KEY,
            principal_id        TEXT NOT NULL,
            password_hash       TEXT NOT NULL,
            password_updated_at REAL NOT NULL,
            must_rotate         INTEGER NOT NULL DEFAULT 0,
            created_at          REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_passwords_principal
            ON principal_passwords(principal_id);
        CREATE TABLE IF NOT EXISTS auth_sessions (
            session_id   TEXT PRIMARY KEY,
            principal_id TEXT NOT NULL,
            project      TEXT NOT NULL,
            session_hash TEXT NOT NULL,
            created_at   REAL NOT NULL,
            expires_at   REAL NOT NULL,
            last_seen_at REAL,
            revoked_at   REAL,
            user_agent   TEXT,
            ip           TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_auth_sessions_hash
            ON auth_sessions(session_hash);
        CREATE INDEX IF NOT EXISTS ix_auth_sessions_principal
            ON auth_sessions(principal_id, expires_at);
        CREATE TABLE IF NOT EXISTS agent_presence (
            agent_id      TEXT PRIMARY KEY,
            runtime       TEXT NOT NULL,
            model         TEXT,
            lane          TEXT,
            task_id       TEXT,
            control       TEXT NOT NULL DEFAULT '{}',
            principal_id  TEXT,
            registered_at REAL NOT NULL,
            heartbeat_at  REAL NOT NULL,
            ttl_s         INTEGER NOT NULL DEFAULT 120
        );
        CREATE INDEX IF NOT EXISTS ix_presence_lane ON agent_presence(lane, heartbeat_at);
        CREATE TABLE IF NOT EXISTS resource_leases (
            id            TEXT PRIMARY KEY,
            agent_id      TEXT NOT NULL,
            principal_id  TEXT,
            task_id       TEXT,
            resource_type TEXT NOT NULL,
            names         TEXT NOT NULL,
            claimed_at    REAL NOT NULL,
            ttl_seconds   INTEGER NOT NULL DEFAULT 1800,
            released_at   REAL
        );
        CREATE INDEX IF NOT EXISTS ix_resource_leases_agent ON resource_leases(agent_id);
        CREATE INDEX IF NOT EXISTS ix_resource_leases_type ON resource_leases(resource_type, released_at);
        CREATE TABLE IF NOT EXISTS task_claims (
            id             TEXT PRIMARY KEY,
            task_id        TEXT NOT NULL,
            agent_id       TEXT NOT NULL,
            principal_id   TEXT,
            status         TEXT NOT NULL,
            claimed_at     REAL NOT NULL,
            expires_at     REAL NOT NULL,
            completed_at   REAL,
            abandon_reason TEXT,
            idem_key       TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_task_claims_active
            ON task_claims(task_id, status, expires_at);
        CREATE TABLE IF NOT EXISTS task_git_state (
            task_id            TEXT PRIMARY KEY,
            branch             TEXT,
            head_sha           TEXT,
            pushed_at          REAL,
            pr_number          INTEGER,
            pr_url             TEXT,
            merged_sha         TEXT,
            merged_at          REAL,
            in_main_content    INTEGER NOT NULL DEFAULT 0,
            published_ref      TEXT,
            last_reconciled_at REAL,
            evidence_json      TEXT NOT NULL DEFAULT '{}',
            updated_at         REAL NOT NULL
        );
        -- COORD-18: code-review judgment is durable board state, fenced to the
        -- exact PR head it reviewed.  Findings are separate rows so remediation,
        -- audit, and future merge policy can query them without parsing activity.
        CREATE TABLE IF NOT EXISTS review_verdicts (
            verdict_id          TEXT PRIMARY KEY,
            task_id             TEXT NOT NULL,
            pr_url              TEXT NOT NULL,
            head_sha            TEXT NOT NULL,
            reviewer_principal  TEXT NOT NULL,
            reviewer_principal_id TEXT,
            review_mode         TEXT NOT NULL DEFAULT 'standard',
            status              TEXT NOT NULL,
            source              TEXT NOT NULL DEFAULT 'review_command',
            created_at          REAL NOT NULL,
            recorded_at         REAL NOT NULL,
            UNIQUE(task_id, head_sha)
        );
        CREATE INDEX IF NOT EXISTS ix_review_verdicts_task
            ON review_verdicts(task_id, created_at);
        CREATE TABLE IF NOT EXISTS review_findings (
            verdict_id          TEXT NOT NULL,
            task_id             TEXT NOT NULL,
            finding_id          TEXT NOT NULL,
            location            TEXT NOT NULL,
            category            TEXT NOT NULL,
            severity            TEXT NOT NULL,
            invariant_violated  TEXT NOT NULL,
            repair_requirement  TEXT NOT NULL,
            finding_class       TEXT NOT NULL,
            state               TEXT NOT NULL,
            resolved_by         TEXT,
            resolved_principal_id TEXT,
            resolved_reason     TEXT,
            resolved_sha        TEXT,
            resolved_at         REAL,
            created_at          REAL NOT NULL,
            updated_at          REAL NOT NULL,
            PRIMARY KEY(verdict_id, finding_id)
        );
        CREATE INDEX IF NOT EXISTS ix_review_findings_task_state
            ON review_findings(task_id, state, finding_id);
        -- SESSION-15: durable preflight predictions joined later to merge/CI outcomes.
        CREATE TABLE IF NOT EXISTS preflight_runs (
            run_id           TEXT PRIMARY KEY,
            task_id          TEXT,
            work_session_id  TEXT,
            claim_id         TEXT,
            agent_id         TEXT,
            head_sha         TEXT NOT NULL,
            base_sha         TEXT,
            branch           TEXT,
            repo_role        TEXT NOT NULL DEFAULT 'canonical',
            repo_path        TEXT,
            verdict          TEXT NOT NULL,
            ok               INTEGER NOT NULL DEFAULT 0,
            finding_count    INTEGER NOT NULL DEFAULT 0,
            blocking_count   INTEGER NOT NULL DEFAULT 0,
            source           TEXT NOT NULL,
            actor            TEXT NOT NULL,
            created_at       REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_preflight_runs_task_head
            ON preflight_runs(task_id, head_sha, created_at);
        CREATE INDEX IF NOT EXISTS ix_preflight_runs_session
            ON preflight_runs(work_session_id, created_at);
        CREATE TABLE IF NOT EXISTS preflight_findings (
            run_id         TEXT NOT NULL,
            finding_seq    INTEGER NOT NULL,
            code           TEXT NOT NULL,
            failure_class  TEXT NOT NULL,
            severity       TEXT NOT NULL,
            blocking       INTEGER NOT NULL DEFAULT 0,
            message        TEXT NOT NULL,
            remediation    TEXT NOT NULL DEFAULT '',
            details_json   TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(run_id, finding_seq)
        );
        CREATE INDEX IF NOT EXISTS ix_preflight_findings_code
            ON preflight_findings(code, blocking);
        -- COORD-20: each changes_requested verdict becomes one durable,
        -- bounded remediation round.  The row is the acceptance contract for
        -- the next claim and the source for hands-off / save metrics.
        CREATE TABLE IF NOT EXISTS review_remediations (
            remediation_id                 TEXT PRIMARY KEY,
            task_id                        TEXT NOT NULL,
            verdict_id                     TEXT NOT NULL UNIQUE,
            source_head_sha                TEXT NOT NULL,
            source_pr_url                  TEXT NOT NULL,
            round_no                       INTEGER NOT NULL,
            status                         TEXT NOT NULL,
            acceptance_criteria_json       TEXT NOT NULL DEFAULT '[]',
            escalation_findings_json       TEXT NOT NULL DEFAULT '[]',
            original_exit_criteria         TEXT,
            previous_status                TEXT,
            previous_assignee              TEXT,
            worker_runtime                  TEXT,
            wake_id                        TEXT,
            requires_adversarial_review    INTEGER NOT NULL DEFAULT 0,
            human_intervention_required    INTEGER NOT NULL DEFAULT 0,
            resolved_without_human         INTEGER NOT NULL DEFAULT 0,
            resolved_head_sha               TEXT,
            auto_finding_count              INTEGER NOT NULL DEFAULT 0,
            escalate_finding_count          INTEGER NOT NULL DEFAULT 0,
            save_counted                    INTEGER NOT NULL DEFAULT 0,
            decision_id                     TEXT,
            created_at                      REAL NOT NULL,
            updated_at                      REAL NOT NULL,
            resolved_at                     REAL
        );
        CREATE INDEX IF NOT EXISTS ix_review_remediations_task
            ON review_remediations(task_id, round_no);
        CREATE INDEX IF NOT EXISTS ix_review_remediations_status
            ON review_remediations(status, updated_at);
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            idem_key      TEXT NOT NULL,
            operation     TEXT NOT NULL,
            actor         TEXT NOT NULL,
            request_hash  TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at    REAL NOT NULL,
            PRIMARY KEY (idem_key, operation)
        );
        CREATE TABLE IF NOT EXISTS external_side_effects (
            effect_key     TEXT PRIMARY KEY,
            project        TEXT NOT NULL,
            effect_type    TEXT NOT NULL,
            target         TEXT NOT NULL,
            resource       TEXT NOT NULL,
            task_id        TEXT,
            claim_id       TEXT,
            agent_id       TEXT,
            status         TEXT NOT NULL,
            payload_hash   TEXT NOT NULL,
            payload_json   TEXT NOT NULL DEFAULT '{}',
            idem_key       TEXT,
            window_key     TEXT,
            requested_by   TEXT,
            claimed_by     TEXT,
            issued_by      TEXT,
            verified_by    TEXT,
            principal_id   TEXT,
            retry_count    INTEGER NOT NULL DEFAULT 0,
            last_error     TEXT,
            readback_json  TEXT NOT NULL DEFAULT '{}',
            requested_at   REAL NOT NULL,
            claimed_at     REAL,
            issued_at      REAL,
            verified_at    REAL,
            updated_at     REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_external_effects_status
            ON external_side_effects(status, effect_type, updated_at);
        CREATE INDEX IF NOT EXISTS ix_external_effects_task
            ON external_side_effects(task_id, status);
        CREATE INDEX IF NOT EXISTS ix_external_effects_resource
            ON external_side_effects(effect_type, target, resource);
        CREATE TABLE IF NOT EXISTS external_ci_runs (
            run_id          TEXT PRIMARY KEY,
            source_project  TEXT NOT NULL,
            source_repo     TEXT NOT NULL,
            source_branch   TEXT,
            source_sha      TEXT NOT NULL,
            mirror_repo     TEXT NOT NULL,
            mirror_branch   TEXT NOT NULL,
            workflow        TEXT NOT NULL,
            status_context  TEXT,
            status          TEXT NOT NULL DEFAULT 'requested',
            conclusion      TEXT,
            run_url         TEXT,
            logs_url        TEXT,
            artifacts_json  TEXT NOT NULL DEFAULT '[]',
            failure_class   TEXT,
            failure_reason  TEXT,
            task_id         TEXT,
            claim_id        TEXT,
            agent_id        TEXT,
            actor           TEXT,
            principal_id    TEXT,
            effect_key      TEXT,
            request_json    TEXT NOT NULL DEFAULT '{}',
            result_json     TEXT NOT NULL DEFAULT '{}',
            requested_at    REAL NOT NULL,
            mirrored_at     REAL,
            triggered_at    REAL,
            completed_at    REAL,
            updated_at      REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_external_ci_task
            ON external_ci_runs(task_id, updated_at);
        CREATE INDEX IF NOT EXISTS ix_external_ci_source
            ON external_ci_runs(source_project, source_sha);
        CREATE INDEX IF NOT EXISTS ix_external_ci_status
            ON external_ci_runs(status, updated_at);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_external_ci_effect
            ON external_ci_runs(effect_key) WHERE effect_key IS NOT NULL;
        CREATE TABLE IF NOT EXISTS publication_evidence (
            publication_id  TEXT PRIMARY KEY,
            source_project  TEXT NOT NULL,
            source_repo     TEXT NOT NULL,
            source_sha      TEXT NOT NULL,
            public_repo     TEXT NOT NULL,
            public_ref      TEXT NOT NULL,
            public_sha      TEXT,
            public_tag      TEXT,
            script          TEXT,
            guard_status    TEXT NOT NULL DEFAULT 'unknown',
            guard_json      TEXT NOT NULL DEFAULT '{}',
            artifact_url    TEXT,
            task_id         TEXT,
            claim_id        TEXT,
            agent_id        TEXT,
            actor           TEXT,
            principal_id    TEXT,
            published_at    REAL NOT NULL,
            created_at      REAL NOT NULL,
            updated_at      REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_publication_evidence_task
            ON publication_evidence(task_id, updated_at);
        CREATE INDEX IF NOT EXISTS ix_publication_evidence_source
            ON publication_evidence(source_project, source_sha);
        CREATE INDEX IF NOT EXISTS ix_publication_evidence_public
            ON publication_evidence(public_repo, public_ref);
        CREATE TABLE IF NOT EXISTS llm_spend (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id        TEXT,
            source            TEXT NOT NULL,
            confidence        TEXT NOT NULL DEFAULT 'unknown',
            task_id           TEXT,
            claim_id          TEXT,
            outcome_id        TEXT,
            agent_id          TEXT,
            principal_id      TEXT,
            runtime           TEXT,
            call_site         TEXT,
            provider          TEXT,
            model             TEXT,
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens      INTEGER NOT NULL DEFAULT 0,
            cost_usd          REAL NOT NULL DEFAULT 0.0,
            latency_ms        REAL,
            status            TEXT NOT NULL DEFAULT 'ok',
            metadata_json     TEXT NOT NULL DEFAULT '{}',
            created_at        REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_spend_task ON llm_spend(task_id);
        CREATE INDEX IF NOT EXISTS ix_spend_agent ON llm_spend(agent_id);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_spend_request
            ON llm_spend(request_id) WHERE request_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS spend_envelopes (
            principal_id      TEXT PRIMARY KEY,
            daily_limit_micros INTEGER NOT NULL CHECK(daily_limit_micros >= 0),
            monthly_limit_micros INTEGER NOT NULL CHECK(monthly_limit_micros >= 0),
            created_at        REAL NOT NULL,
            updated_at        REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS spend_reservations (
            reservation_id    TEXT PRIMARY KEY,
            request_id        TEXT NOT NULL UNIQUE,
            principal_id      TEXT NOT NULL,
            reserved_micros   INTEGER NOT NULL CHECK(reserved_micros > 0),
            actual_micros     INTEGER CHECK(actual_micros >= 0),
            provider          TEXT,
            model             TEXT,
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            status            TEXT NOT NULL CHECK(status IN ('reserved','reconciled','released')),
            reserved_at       REAL NOT NULL,
            reconciled_at     REAL,
            metadata_json     TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS ix_spend_reservations_principal_time
            ON spend_reservations(principal_id, reserved_at);
        CREATE TABLE IF NOT EXISTS outcomes (
            id             TEXT PRIMARY KEY,
            project        TEXT NOT NULL,
            task_id        TEXT,
            epic_id        TEXT,
            claim_id       TEXT,
            type           TEXT NOT NULL,
            title          TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'proposed',
            verifier       TEXT,
            verification   TEXT,
            evidence_json  TEXT NOT NULL DEFAULT '{}',
            value_json     TEXT NOT NULL DEFAULT '{}',
            created_at     REAL NOT NULL,
            verified_at    REAL
        );
        CREATE INDEX IF NOT EXISTS ix_outcomes_task ON outcomes(task_id, status);
        CREATE INDEX IF NOT EXISTS ix_outcomes_claim ON outcomes(claim_id);
        CREATE TABLE IF NOT EXISTS kpis (
            id             TEXT PRIMARY KEY,
            project        TEXT NOT NULL,
            name           TEXT NOT NULL,
            unit           TEXT NOT NULL,
            direction      TEXT NOT NULL,
            owner          TEXT,
            baseline_value REAL,
            current_value  REAL,
            target_value   REAL,
            period         TEXT,
            created_at     REAL NOT NULL,
            updated_at     REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_kpis_project ON kpis(project);
        CREATE TABLE IF NOT EXISTS outcome_kpi_links (
            id                TEXT PRIMARY KEY,
            project           TEXT NOT NULL,
            outcome_id        TEXT NOT NULL,
            kpi_id            TEXT NOT NULL,
            contribution      REAL,
            contribution_unit TEXT,
            confidence        TEXT NOT NULL,
            rationale         TEXT,
            created_at        REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_outcome_kpi_outcome ON outcome_kpi_links(outcome_id);
        CREATE INDEX IF NOT EXISTS ix_outcome_kpi_kpi ON outcome_kpi_links(kpi_id);
        CREATE TABLE IF NOT EXISTS task_summaries (
            task_id         TEXT PRIMARY KEY,
            rationale       TEXT NOT NULL,
            generated_at    REAL NOT NULL,
            activity_cursor INTEGER NOT NULL DEFAULT 0
        );
        -- NARRATE-2: CEO-voice task narration (separate store + audience from
        -- task_summaries.rationale; see docs/CEO-NARRATOR-CONTRACT.md).
        CREATE TABLE IF NOT EXISTS task_narrations (
            task_id            TEXT PRIMARY KEY,
            narration          TEXT NOT NULL,
            generated_at       REAL NOT NULL,
            activity_cursor    INTEGER NOT NULL DEFAULT 0,
            source_fingerprint TEXT,
            model              TEXT
        );
        -- Trigger queue: create/update_task enqueue a marker on meaningful status
        -- transitions; the narrate_pending drain job consumes it. task_id PRIMARY KEY
        -- dedupes a burst of transitions into one pending row.
        CREATE TABLE IF NOT EXISTS pending_narrations (
            task_id     TEXT PRIMARY KEY,
            status      TEXT,
            reason      TEXT,
            enqueued_at REAL NOT NULL
        );
        -- NARRATE-8: transactional narration outbox (ADR-0008). A meaningful task/
        -- deliverable mutation and its narration intent commit in the same SQLite
        -- transaction; durable outbox state — not the post-commit wake or a timer —
        -- is the source of pending work. Rows hold a strict
        -- switchboard.narration_requested.v1 envelope (see narration_events.py) plus
        -- mutable attempt/lease delivery state. Nothing consumes this yet; NARRATE-9
        -- owns the wakeable worker. The unique dedupe_key makes a retried domain write
        -- idempotent (INSERT OR IGNORE), so one request revision is emitted at most once.
        CREATE TABLE IF NOT EXISTS narration_outbox (
            event_id         TEXT PRIMARY KEY,
            schema_version   TEXT NOT NULL,
            event_type       TEXT NOT NULL,
            project          TEXT NOT NULL,
            entity_type      TEXT NOT NULL,
            entity_id        TEXT NOT NULL,
            source_revision  INTEGER NOT NULL,
            source_hash      TEXT NOT NULL,
            causal_event     TEXT NOT NULL,
            priority         TEXT NOT NULL DEFAULT 'normal',
            requested_at     REAL NOT NULL,
            dedupe_key       TEXT NOT NULL,
            supersedes       TEXT,
            attempt_state    TEXT NOT NULL DEFAULT 'pending',
            attempt_count    INTEGER NOT NULL DEFAULT 0,
            available_at     REAL NOT NULL,
            claimed_by       TEXT,
            lease_expires_at REAL,
            last_error       TEXT,
            authorization    TEXT NOT NULL,
            trace_id         TEXT NOT NULL,
            created_at       REAL NOT NULL,
            updated_at       REAL NOT NULL
        );
        -- NARRATE-12: durable generation receipt for every narration attempt (ADR-0008 M3).
        -- One row per attempt — deterministic template, LLM synthesis, or explicit fallback —
        -- recording the exact source revision/hash, model, prompt version, latency, tokens,
        -- cost, outcome, and fallback reason. A failed LLM receipt is NEVER overwritten by a
        -- fallback: the fallback is a separate row that preserves the failure. Receipts are the
        -- cost/audit ledger and the source of per-project budget accounting.
        CREATE TABLE IF NOT EXISTS narration_receipts (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id         TEXT,
            project          TEXT NOT NULL,
            entity_type      TEXT NOT NULL,
            entity_id        TEXT NOT NULL,
            source_revision  INTEGER,
            source_hash      TEXT,
            content_sig      TEXT,
            mode             TEXT NOT NULL,
            outcome          TEXT NOT NULL,
            model            TEXT,
            prompt_version   TEXT,
            latency_ms       REAL,
            tokens_in        INTEGER NOT NULL DEFAULT 0,
            tokens_out       INTEGER NOT NULL DEFAULT 0,
            cost_usd         REAL NOT NULL DEFAULT 0.0,
            fallback_reason  TEXT,
            narration        TEXT,
            narration_hash   TEXT,
            created_at       REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_narration_receipts_entity
            ON narration_receipts(project, entity_type, entity_id, id);
        CREATE INDEX IF NOT EXISTS ix_narration_receipts_cost
            ON narration_receipts(project, created_at);
        CREATE TABLE IF NOT EXISTS project_boards (
            id                         TEXT PRIMARY KEY,
            title                      TEXT NOT NULL,
            kind                       TEXT NOT NULL DEFAULT 'mission',
            status                     TEXT NOT NULL DEFAULT 'active',
            owner_org                  TEXT,
            owner_person_or_role       TEXT,
            purpose                    TEXT,
            end_state                  TEXT,
            description                TEXT,
            metadata_json              TEXT NOT NULL DEFAULT '{}',
            created_at                 REAL NOT NULL,
            updated_at                 REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deliverables (
            id                         TEXT PRIMARY KEY,
            board_id                   TEXT,
            title                      TEXT NOT NULL,
            status                     TEXT NOT NULL DEFAULT 'proposed',
            owner_org                  TEXT,
            owner_person_or_role       TEXT,
            end_state                  TEXT,
            why_it_matters             TEXT,
            confidence                 REAL,
            acceptance_criteria_json   TEXT NOT NULL DEFAULT '[]',
            policy_constraints_json    TEXT NOT NULL DEFAULT '{}',
            proof_requirements_json    TEXT NOT NULL DEFAULT '{}',
            kpi_links_json             TEXT NOT NULL DEFAULT '[]',
            metadata_json              TEXT NOT NULL DEFAULT '{}',
            created_at                 REAL NOT NULL,
            updated_at                 REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deliverable_milestones (
            id                         TEXT PRIMARY KEY,
            deliverable_id             TEXT NOT NULL,
            title                      TEXT NOT NULL,
            description                TEXT,
            status                     TEXT NOT NULL DEFAULT 'not_started',
            sort_order                 INTEGER NOT NULL DEFAULT 0,
            acceptance_criteria_json   TEXT NOT NULL DEFAULT '[]',
            proof_requirements_json    TEXT NOT NULL DEFAULT '{}',
            created_at                 REAL NOT NULL,
            updated_at                 REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_deliverable_milestones_deliverable
            ON deliverable_milestones(deliverable_id, sort_order);
        CREATE TABLE IF NOT EXISTS deliverable_task_links (
            id                         TEXT PRIMARY KEY,
            deliverable_id             TEXT NOT NULL,
            board_id                   TEXT,
            milestone_id               TEXT,
            project_id                 TEXT NOT NULL,
            task_id                    TEXT NOT NULL,
            role                       TEXT NOT NULL DEFAULT 'contributes',
            blocks_deliverable         INTEGER NOT NULL DEFAULT 0,
            proof_required_json        TEXT NOT NULL DEFAULT '{}',
            metadata_json              TEXT NOT NULL DEFAULT '{}',
            created_at                 REAL NOT NULL,
            updated_at                 REAL NOT NULL,
            UNIQUE(deliverable_id, project_id, task_id)
        );
        CREATE INDEX IF NOT EXISTS ix_deliverable_links_deliverable
            ON deliverable_task_links(deliverable_id, milestone_id);
        CREATE INDEX IF NOT EXISTS ix_deliverable_links_task
            ON deliverable_task_links(project_id, task_id);
        CREATE TABLE IF NOT EXISTS deliverable_breakdown_proposals (
            id                         TEXT PRIMARY KEY,
            deliverable_id             TEXT NOT NULL,
            status                     TEXT NOT NULL DEFAULT 'proposed',
            proposed_by                TEXT,
            approved_by                TEXT,
            reviewed_by                TEXT,
            outcome_text               TEXT,
            review_reason              TEXT,
            deferred_until             REAL,
            payload_json               TEXT NOT NULL DEFAULT '{}',
            created_at                 REAL NOT NULL,
            updated_at                 REAL NOT NULL,
            approved_at                REAL
        );
        CREATE INDEX IF NOT EXISTS ix_breakdown_proposals_deliverable
            ON deliverable_breakdown_proposals(deliverable_id, status, updated_at);
        CREATE TABLE IF NOT EXISTS agent_hosts (
            host_id            TEXT PRIMARY KEY,
            hostname           TEXT,
            agent_host_version TEXT,
            repo_root          TEXT,
            runtimes_json      TEXT NOT NULL DEFAULT '[]',
            limits_json        TEXT NOT NULL DEFAULT '{}',
            capacity_json      TEXT NOT NULL DEFAULT '{}',
            principal_id       TEXT,
            registered_at      REAL NOT NULL,
            heartbeat_at       REAL NOT NULL,
            heartbeat_ttl_s    INTEGER NOT NULL DEFAULT 60,
            status             TEXT NOT NULL DEFAULT 'online',
            last_error         TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_agent_hosts_heartbeat
            ON agent_hosts(status, heartbeat_at);
        CREATE TABLE IF NOT EXISTS agent_host_enrollments (
            enrollment_id          TEXT PRIMARY KEY,
            project_id             TEXT NOT NULL,
            requested_host_id      TEXT,
            host_id                TEXT,
            owner_user_id          TEXT NOT NULL,
            tenant_allowlist_json  TEXT NOT NULL DEFAULT '[]',
            project_allowlist_json TEXT NOT NULL DEFAULT '[]',
            provider_allowlist_json TEXT NOT NULL DEFAULT '[]',
            execution_policy_json  TEXT NOT NULL DEFAULT '{}',
            bootstrap_hash         TEXT NOT NULL UNIQUE,
            bootstrap_expires_at   REAL NOT NULL,
            bootstrap_consumed_at  REAL,
            completion_recovery_hash TEXT,
            completion_recovery_expires_at REAL,
            completion_finalized_at REAL,
            principal_id           TEXT,
            public_key_fingerprint TEXT,
            identity_generation    INTEGER NOT NULL DEFAULT 0,
            package_version        TEXT,
            platform               TEXT,
            hostname               TEXT,
            status                 TEXT NOT NULL DEFAULT 'pending',
            created_by_principal_id TEXT,
            created_at             REAL NOT NULL,
            updated_at             REAL NOT NULL,
            revoked_at             REAL,
            UNIQUE(project_id, host_id)
        );
        CREATE INDEX IF NOT EXISTS ix_agent_host_enrollments_status
            ON agent_host_enrollments(status, bootstrap_expires_at);
        CREATE INDEX IF NOT EXISTS ix_agent_host_enrollments_principal
            ON agent_host_enrollments(principal_id);
        CREATE TABLE IF NOT EXISTS wake_intents (
            wake_id           TEXT PRIMARY KEY,
            source            TEXT NOT NULL,
            reason            TEXT NOT NULL,
            selector_json     TEXT NOT NULL DEFAULT '{}',
            policy_json       TEXT NOT NULL DEFAULT '{}',
            status            TEXT NOT NULL DEFAULT 'pending',
            requested_at      REAL NOT NULL,
            deadline          REAL,
            claimed_at        REAL,
            claimed_by_host   TEXT,
            completed_at      REAL,
            runner_session_id TEXT,
            agent_id          TEXT,
            result_json       TEXT NOT NULL DEFAULT '{}',
            placement_json    TEXT NOT NULL DEFAULT '{}',
            task_id           TEXT,
            principal_id      TEXT,
            idem_key          TEXT,
            effect_key        TEXT,
            archived_at       REAL
        );
        CREATE INDEX IF NOT EXISTS ix_wake_intents_status
            ON wake_intents(status, deadline, requested_at);
        CREATE INDEX IF NOT EXISTS ix_wake_intents_host
            ON wake_intents(claimed_by_host, status);
        -- BUG-89 history indexes are installed by migrations 0067-0071 after additive
        -- migration 0066 has added archived_at to legacy wake_intents tables.
        CREATE UNIQUE INDEX IF NOT EXISTS ux_wake_intents_idem
            ON wake_intents(idem_key) WHERE idem_key IS NOT NULL;
        CREATE TABLE IF NOT EXISTS personal_execution_connections (
            execution_connection_id TEXT PRIMARY KEY,
            wake_id                 TEXT NOT NULL UNIQUE,
            task_id                 TEXT NOT NULL,
            claim_id                TEXT NOT NULL,
            work_session_id         TEXT NOT NULL,
            runner_session_id       TEXT NOT NULL,
            host_id                 TEXT NOT NULL,
            host_principal_id       TEXT NOT NULL,
            agent_id                TEXT NOT NULL,
            source_sha              TEXT NOT NULL,
            status                  TEXT NOT NULL DEFAULT 'reserved',
            created_at              REAL NOT NULL,
            expires_at              REAL NOT NULL,
            claimed_at              REAL,
            completed_at            REAL,
            updated_at              REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_personal_execution_connections_status
            ON personal_execution_connections(status, expires_at);
        CREATE TABLE IF NOT EXISTS runner_sessions (
            runner_session_id TEXT PRIMARY KEY,
            host_id           TEXT,
            agent_id          TEXT,
            runtime           TEXT,
            task_id           TEXT,
            claim_id          TEXT,
            pid               INTEGER,
            status            TEXT NOT NULL DEFAULT 'unknown',
            cwd               TEXT,
            control_json      TEXT NOT NULL DEFAULT '{}',
            metadata_json     TEXT NOT NULL DEFAULT '{}',
            last_snapshot_json TEXT NOT NULL DEFAULT '{}',
            principal_id      TEXT,
            started_at        REAL,
            heartbeat_at      REAL NOT NULL,
            heartbeat_ttl_s   INTEGER NOT NULL DEFAULT 60,
            updated_at        REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_runner_sessions_host
            ON runner_sessions(host_id, heartbeat_at);
        CREATE INDEX IF NOT EXISTS ix_runner_sessions_task
            ON runner_sessions(task_id, status);
        CREATE TABLE IF NOT EXISTS direct_session_tokens (
            token_hash        TEXT PRIMARY KEY,
            project_id        TEXT NOT NULL,
            task_id           TEXT NOT NULL,
            agent_id          TEXT NOT NULL,
            host_id           TEXT NOT NULL,
            wake_id           TEXT NOT NULL,
            runner_session_id TEXT NOT NULL,
            issued_at         REAL NOT NULL,
            expires_at        REAL NOT NULL,
            revoked_at        REAL
        );
        CREATE INDEX IF NOT EXISTS ix_direct_session_tokens_runner
            ON direct_session_tokens(runner_session_id, expires_at);
        CREATE TABLE IF NOT EXISTS work_sessions (
            work_session_id      TEXT PRIMARY KEY,
            project_id           TEXT NOT NULL,
            task_id              TEXT,
            claim_id             TEXT,
            agent_id             TEXT NOT NULL,
            runtime              TEXT,
            repo_role            TEXT NOT NULL,
            repo                 TEXT,
            default_branch       TEXT,
            branch               TEXT,
            upstream             TEXT,
            base_sha             TEXT,
            head_sha             TEXT,
            worktree_path        TEXT,
            clone_path           TEXT,
            storage_mode         TEXT NOT NULL,
            status               TEXT NOT NULL,
            dirty_status         TEXT NOT NULL,
            conflict_marker_count INTEGER NOT NULL DEFAULT 0,
            hygiene_json         TEXT NOT NULL DEFAULT '{}',
            file_leases_json     TEXT NOT NULL DEFAULT '[]',
            resource_leases_json TEXT NOT NULL DEFAULT '[]',
            env_json             TEXT NOT NULL DEFAULT '{}',
            policy_profile       TEXT,
            session_token_hash   TEXT,
            principal_id         TEXT,
            created_by           TEXT,
            updated_by           TEXT,
            created_at           REAL NOT NULL,
            updated_at           REAL NOT NULL,
            expires_at           REAL,
            completed_at         REAL
        );
        CREATE INDEX IF NOT EXISTS ix_work_sessions_task
            ON work_sessions(task_id, status, updated_at);
        CREATE INDEX IF NOT EXISTS ix_work_sessions_agent
            ON work_sessions(agent_id, status, updated_at);
        CREATE INDEX IF NOT EXISTS ix_work_sessions_branch
            ON work_sessions(repo_role, branch, status);
        CREATE INDEX IF NOT EXISTS ix_work_sessions_path
            ON work_sessions(worktree_path, clone_path);
        CREATE TABLE IF NOT EXISTS runner_control_requests (
            request_id        TEXT PRIMARY KEY,
            runner_session_id TEXT NOT NULL,
            host_id           TEXT,
            action            TEXT NOT NULL,
            status            TEXT NOT NULL,
            reason            TEXT,
            requested_by      TEXT,
            principal_id      TEXT,
            requested_at      REAL NOT NULL,
            claimed_at        REAL,
            claimed_by_host   TEXT,
            completed_at      REAL,
            snapshot_json     TEXT NOT NULL DEFAULT '{}',
            result_json       TEXT NOT NULL DEFAULT '{}',
            options_json      TEXT NOT NULL DEFAULT '{}',
            effect_key        TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_runner_control_status
            ON runner_control_requests(status, host_id, requested_at);
        CREATE INDEX IF NOT EXISTS ix_runner_control_session
            ON runner_control_requests(runner_session_id, requested_at);
        CREATE TABLE IF NOT EXISTS archived_tasks (
            archive_id          TEXT PRIMARY KEY,
            task_id             TEXT NOT NULL,
            operation           TEXT NOT NULL,
            actor               TEXT NOT NULL,
            reason              TEXT,
            source_project      TEXT NOT NULL,
            destination_project TEXT,
            snapshot_json       TEXT NOT NULL,
            created_at          REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_archived_tasks_task
            ON archived_tasks(task_id, created_at);
        CREATE TABLE IF NOT EXISTS background_job_runs (
            run_id          TEXT PRIMARY KEY,
            job_name        TEXT NOT NULL,
            project         TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            runtime         TEXT NOT NULL DEFAULT 'local_checkpoint',
            manifest_json   TEXT NOT NULL DEFAULT '{}',
            created_at      REAL NOT NULL,
            updated_at      REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_background_job_runs_job
            ON background_job_runs(job_name, updated_at);
        CREATE INDEX IF NOT EXISTS ix_background_job_runs_project
            ON background_job_runs(project, updated_at);
        -- PERF-1: durable webhook inbox (accept-and-ack, never drop). The GitHub
        -- webhook handler appends the raw event here in O(1) and returns 2xx; a
        -- separate drain worker applies provenance idempotently off the request
        -- path (dedup on delivery_guid). Canonical DDL mirrored in
        -- webhook_inbox._DDL, which self-heals pre-existing DBs on first touch.
        CREATE TABLE IF NOT EXISTS webhook_inbox (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_guid      TEXT NOT NULL,
            event              TEXT NOT NULL,
            project            TEXT NOT NULL,
            requested_project  TEXT,
            signature_verified INTEGER NOT NULL DEFAULT 0,
            headers_json       TEXT NOT NULL DEFAULT '{}',
            payload            TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'pending',
            attempts           INTEGER NOT NULL DEFAULT 0,
            last_error         TEXT,
            result_json        TEXT NOT NULL DEFAULT '{}',
            received_at        REAL NOT NULL,
            updated_at         REAL NOT NULL,
            applied_at         REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_webhook_inbox_guid
            ON webhook_inbox(delivery_guid);
        CREATE INDEX IF NOT EXISTS ix_webhook_inbox_status
            ON webhook_inbox(status, id);
        CREATE INDEX IF NOT EXISTS ix_tasks_ws ON tasks(workstream_id);
        CREATE INDEX IF NOT EXISTS ix_inbox_status ON inbox(status);
        CREATE INDEX IF NOT EXISTS ix_activity_task ON activity(task_id);
        CREATE INDEX IF NOT EXISTS ix_activity_ts ON activity(created_at);
        CREATE INDEX IF NOT EXISTS ix_activity_task_id ON activity(task_id, id);
        CREATE INDEX IF NOT EXISTS ix_activity_kind_id ON activity(kind, id);
        CREATE INDEX IF NOT EXISTS ix_chat_session ON chat(session);
        CREATE INDEX IF NOT EXISTS ix_leases_agent ON file_leases(agent_id);
        -- NARRATE-8 outbox access paths: idempotent emit (unique dedupe_key),
        -- recovery-only sweep over actionable rows, and per-entity revision order.
        CREATE UNIQUE INDEX IF NOT EXISTS ux_narration_outbox_dedupe
            ON narration_outbox(dedupe_key);
        CREATE INDEX IF NOT EXISTS ix_narration_outbox_recovery
            ON narration_outbox(attempt_state, available_at);
        CREATE INDEX IF NOT EXISTS ix_narration_outbox_entity
            ON narration_outbox(entity_type, entity_id, source_revision);
        """
    )
    # BUG-47: additive column/index migrations run through the numbered, ledgered runner in
    # db/migrations.py — idempotent and safe on every startup, but a real failure (disk,
    # lock, permission, corruption, syntax) now propagates instead of being swallowed as a
    # benign "column already exists". The migration list, including the narration source
    # revision/hash columns (NARRATE-8) and ux_messages_idem, lives there.
    run_additive_migrations(c)


def seed_from_plan(c, seed_path):
    n = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    if n:
        return n
    if not seed_path or not os.path.exists(seed_path):
        return 0
    plan = json.load(open(seed_path))
    now = time.time()
    order = 0
    for w in plan.get("workstreams", []):
        for t in w.get("tasks", []):
            order += 1
            so = order if t.get("sort_order") is None else t.get("sort_order")
            c.execute(
                """INSERT OR REPLACE INTO tasks
                (task_id, workstream_id, workstream_name, title, description,
                 owner_org, owner_person_or_role, assignee, phase, status,
                 effort_days, duration_days, start_date, finish_date, start_day,
                 depends_on, entry_criteria, exit_criteria, deliverable,
                 risk_level, is_blocking, sort_order, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (t["task_id"], w["workstream_id"], w["name"], t.get("title"),
                 t.get("description"), t.get("owner_org"), t.get("owner_person_or_role"),
                 t.get("assignee"), t.get("phase"), t.get("status", "Not Started"),
                 t.get("effort_days"), t.get("duration_days"), t.get("start_date"),
                 t.get("finish_date"), t.get("start_day"),
                 json.dumps(t.get("depends_on", [])), t.get("entry_criteria"),
                 t.get("exit_criteria"), t.get("deliverable"), t.get("risk_level"),
                 1 if t.get("is_blocking") else 0, so, now, now),
            )
    for k in META_SECTIONS:
        if k in plan:
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                      (k, json.dumps(plan[k])))
    if "people" not in plan:
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                  ("people", json.dumps(DEFAULT_PEOPLE)))
    return order
