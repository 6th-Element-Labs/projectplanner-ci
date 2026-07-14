"""Audit export application query (ARCH-MS-63).

Moved from ``repositories/shell.py``. Fan-in read model that assembles a redacted
enterprise evidence bundle. Persistence stays in repositories; this module owns
redaction policy and bundle assembly — not a fat audit SQL repository.
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from constants import DEFAULT_PROJECT
from db.core import _json_payload
import evidence_claims


__all__ = [
    "_AUDIT_REDACT_KEYS",
    "_audit_redact",
    "_canonical_repo_root",
    "_evidence_claim_reports",
    "audit_export",
    "execute",
    "execute_mapping_result",
]


def _store():
    import store
    return store


_AUDIT_REDACT_KEYS = {
    "auth_capsule",
    "ciphertext",
    "credential",
    "credential_nonce",
    "encrypted_credential",
    "nonce",
    "password",
    "password_hash",
    "raw_token",
    "secret",
    "session_hash",
    "token",
    "token_hash",
}


def _audit_redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).lower() in _AUDIT_REDACT_KEYS:
                continue
            else:
                out[key] = _audit_redact(item)
        return out
    if isinstance(value, list):
        return [_audit_redact(item) for item in value]
    return value


def _audit_table_rows(c: sqlite3.Connection, table: str,
                      order_by: str = "") -> List[Dict[str, Any]]:
    sql = f"SELECT * FROM {table}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    rows = [dict(r) for r in c.execute(sql).fetchall()]
    return [_audit_redact(r) for r in rows]


def _audit_json_rows(c: sqlite3.Connection, table: str, json_columns: Tuple[str, ...],
                     order_by: str = "") -> List[Dict[str, Any]]:
    rows = _audit_table_rows(c, table, order_by=order_by)
    for row in rows:
        for column in json_columns:
            if column in row:
                key = column[:-5] if column.endswith("_json") else column
                row[key] = _audit_redact(_json_payload(row.pop(column)))
    return rows


def _audit_activity_rows(c: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = _audit_table_rows(c, "activity", order_by="created_at, id")
    for row in rows:
        row["payload"] = _audit_redact(_json_payload(row.get("payload") or ""))
    return rows


def _canonical_repo_root() -> str:
    """Repo root for evidence/path resolution (application/queries → repo root)."""
    configured = (os.environ.get("PM_REPO_PATH") or "").strip()
    if configured:
        return os.path.abspath(configured)
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
    )


def _evidence_claim_reports(c: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = _audit_table_rows(c, "activity", order_by="created_at, id")
    for row in rows:
        row["payload"] = _json_payload(row.get("payload") or "")
    return evidence_claims.evaluate_activities(rows, _canonical_repo_root())


def _audit_tasks(c: sqlite3.Connection, project: str) -> List[Dict[str, Any]]:
    store = _store()
    tasks: List[Dict[str, Any]] = []
    rows = c.execute("SELECT * FROM tasks ORDER BY sort_order, task_id").fetchall()
    for row in rows:
        task = store._task_row(row)
        task["git_state"] = store._load_git_state(c, task["task_id"])
        task["provenance"] = store._provenance_summary(task["git_state"])
        task["active_claims"] = store._active_task_claims_in(c, task["task_id"])
        task["tally"] = store.task_tally(task["task_id"], project=project)
        tasks.append(_audit_redact(task))
    return tasks


def _audit_registry_scope(project: str) -> Dict[str, Any]:
    store = _store()
    store.init_project_registry()
    with store._registry_conn() as c:
        project_access = c.execute(
            "SELECT * FROM project_access WHERE project_id=?", (project,)
        ).fetchone()
        role_grants = c.execute(
            "SELECT * FROM project_role_grants WHERE project_id=? "
            "ORDER BY created_at, subject_kind, subject_id, role",
            (project,),
        ).fetchall()
        lifecycle_events = c.execute(
            "SELECT * FROM project_lifecycle_events WHERE project_id=? "
            "ORDER BY created_at, event_id", (project,),
        ).fetchall()
        purge_intents = c.execute(
            "SELECT * FROM project_purge_intents WHERE project_id=? "
            "ORDER BY created_at, intent_id", (project,)).fetchall()
        purge_tombstones = c.execute(
            "SELECT * FROM project_purge_tombstones WHERE project_id=? "
            "ORDER BY created_at, tombstone_id", (project,)).fetchall()
        cleanup_reviews = c.execute(
            "SELECT * FROM project_cleanup_reviews WHERE project_id=? "
            "ORDER BY created_at, review_id", (project,)).fetchall()
        orgs = []
        users = []
        memberships = []
        org_id = project_access["org_id"] if project_access and project_access["org_id"] else ""
        if org_id:
            orgs = c.execute("SELECT * FROM orgs WHERE id=? ORDER BY id", (org_id,)).fetchall()
            memberships = c.execute(
                "SELECT * FROM org_memberships WHERE org_id=? ORDER BY created_at, org_id, user_id",
                (org_id,),
            ).fetchall()
            user_ids = sorted({m["user_id"] for m in memberships})
            if user_ids:
                placeholders = ",".join("?" for _ in user_ids)
                users = c.execute(
                    f"SELECT * FROM users WHERE id IN ({placeholders}) ORDER BY id",
                    user_ids,
                ).fetchall()
    lifecycle_event_records = []
    for row in lifecycle_events:
        item = dict(row)
        item["validation"] = _json_payload(item.pop("validation_json", ""))
        lifecycle_event_records.append(item)
    purge_intent_records = []
    for row in purge_intents:
        item = dict(row)
        item["intent"] = _json_payload(item.pop("intent_json", ""))
        item["failure"] = _json_payload(item.pop("failure_json", ""))
        purge_intent_records.append(item)
    purge_tombstone_records = []
    for row in purge_tombstones:
        item = dict(row)
        item["registry_record"] = _json_payload(item.pop("registry_record_json", ""))
        item["audit_receipt"] = _json_payload(item.pop("audit_receipt_json", ""))
        purge_tombstone_records.append(item)
    cleanup_review_records = []
    for row in cleanup_reviews:
        item = dict(row)
        item["impact_report_receipt"] = _json_payload(item.pop("impact_receipt_json", ""))
        cleanup_review_records.append(item)
    return _audit_redact({
        "project_access": dict(project_access) if project_access else None,
        "project_role_grants": [dict(r) for r in role_grants],
        "project_lifecycle_events": lifecycle_event_records,
        "project_purge_intents": purge_intent_records,
        "project_purge_tombstones": purge_tombstone_records,
        "project_cleanup_reviews": cleanup_review_records,
        "orgs": [dict(r) for r in orgs],
        "users": [dict(r) for r in users],
        "org_memberships": [dict(r) for r in memberships],
    })


def execute(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Versioned enterprise evidence bundle for audit/retention.

    The bundle preserves the evidence graph needed to answer who acted, under whose authority, at
    what cost, and with what proof, without exposing bearer token hashes, password hashes, session
    hashes, or raw secrets.
    """
    store = _store()
    store.init_db(project)
    generated_at = time.time()
    with store._conn(project) as c:
        tasks = _audit_tasks(c, project)
        activity = _audit_activity_rows(c)
        evidence_claim_reports = evidence_claims.evaluate_activities(
            activity, _canonical_repo_root())
        claims = _audit_table_rows(c, "task_claims", order_by="claimed_at, id")
        messages = _audit_table_rows(c, "agent_messages", order_by="sent_at, id")
        monitors = _audit_json_rows(
            c, "coordination_monitors",
            ("condition_json", "on_timeout_json", "result_json"),
            order_by="created_at, id",
        )
        principals = [
            store.public_principal_record(store._principal_from_row(row), project=project)
            for row in c.execute("SELECT * FROM principals ORDER BY created_at, id").fetchall()
        ]
        access_sessions = _audit_table_rows(
            c, "auth_sessions", order_by="created_at, session_id")
        presence = [store._presence_row(row, now=generated_at)
                    for row in c.execute(
                        "SELECT * FROM agent_presence ORDER BY registered_at, agent_id"
                    ).fetchall()]
        resource_leases = _audit_json_rows(c, "resource_leases", ("names",),
                                           order_by="claimed_at, id")
        wake_intents = _audit_json_rows(
            c, "wake_intents", ("selector_json", "policy_json", "result_json"),
            order_by="requested_at, wake_id",
        )
        runner_sessions = [
            store._runner_session_row(row, now=generated_at, include_claim=True, c=c)
            for row in c.execute(
                "SELECT * FROM runner_sessions ORDER BY updated_at, runner_session_id"
            ).fetchall()
        ]
        runner_controls = _audit_json_rows(
            c, "runner_control_requests",
            ("snapshot_json", "result_json", "options_json"),
            order_by="requested_at, request_id",
        )
        side_effects = _audit_json_rows(
            c, "external_side_effects",
            ("payload_json", "readback_json"),
            order_by="requested_at, effect_key",
        )
        external_ci_runs = _audit_json_rows(
            c, "external_ci_runs",
            ("artifacts_json", "request_json", "result_json"),
            order_by="requested_at, run_id",
        )
        publication_evidence = _audit_json_rows(
            c, "publication_evidence",
            ("guard_json",),
            order_by="published_at, publication_id",
        )
        git_state = [store._git_state_row(row) for row in c.execute(
            "SELECT * FROM task_git_state ORDER BY updated_at, task_id"
        ).fetchall()]
        spend = [store._spend_row(row) for row in c.execute(
            "SELECT * FROM llm_spend ORDER BY created_at, id"
        ).fetchall()]
        outcomes = [store._outcome_row(row) for row in c.execute(
            "SELECT * FROM outcomes ORDER BY created_at, id"
        ).fetchall()]
        kpis = [dict(row) for row in c.execute(
            "SELECT * FROM kpis ORDER BY created_at, id"
        ).fetchall()]
        outcome_links = [dict(row) for row in c.execute(
            "SELECT * FROM outcome_kpi_links ORDER BY created_at, id"
        ).fetchall()]
        project_boards = [store._project_board_row(row, project=project) for row in c.execute(
            "SELECT * FROM project_boards ORDER BY updated_at, id"
        ).fetchall()]
        deliverables = [store._deliverable_row(row) for row in c.execute(
            "SELECT * FROM deliverables ORDER BY updated_at, id"
        ).fetchall()]
        deliverable_milestones = [store._deliverable_milestone_row(row) for row in c.execute(
            "SELECT * FROM deliverable_milestones ORDER BY sort_order, created_at, id"
        ).fetchall()]
        deliverable_task_links = [store._deliverable_link_row(row) for row in c.execute(
            "SELECT * FROM deliverable_task_links ORDER BY created_at, id"
        ).fetchall()]
        archived_tasks = _audit_json_rows(
            c, "archived_tasks", ("snapshot_json",), order_by="created_at, archive_id")
        work_sessions = [store._work_session_row(row) for row in c.execute(
            "SELECT * FROM work_sessions ORDER BY updated_at, work_session_id"
        ).fetchall()]
    bundle = {
        "schema": "switchboard.audit_export.v1",
        "project": project,
        "generated_at": generated_at,
        "summary": {
            "task_count": len(tasks),
            "activity_count": len(activity),
            "evidence_claim_count": len(evidence_claim_reports),
            "evidence_claim_status_counts": evidence_claims.summarize_reports(
                evidence_claim_reports)["status_counts"],
            "claim_count": len(claims),
            "message_count": len(messages),
            "principal_count": len(principals),
            "runner_session_count": len(runner_sessions),
            "side_effect_count": len(side_effects),
            "external_ci_run_count": len(external_ci_runs),
            "publication_evidence_count": len(publication_evidence),
            "outcome_count": len(outcomes),
            "spend_count": len(spend),
            "project_board_count": len(project_boards),
            "deliverable_count": len(deliverables),
            "work_session_count": len(work_sessions),
        },
        "access": {
            "principals": _audit_redact(principals),
            "sessions": access_sessions,
            **_audit_registry_scope(project),
        },
        "tasks": tasks,
        "activity": activity,
        "evidence_claims": _audit_redact(evidence_claim_reports),
        "claims": claims,
        "messages": messages,
        "monitors": monitors,
        "agent_presence": _audit_redact(presence),
        "resource_leases": resource_leases,
        "wake_intents": wake_intents,
        "runner_sessions": _audit_redact(runner_sessions),
        "work_sessions": _audit_redact(work_sessions),
        "runner_control_requests": runner_controls,
        "external_side_effects": _audit_redact(side_effects),
        "external_ci_runs": _audit_redact(external_ci_runs),
        "publication_evidence": _audit_redact(publication_evidence),
        "git_state": _audit_redact(git_state),
        "economics": {
            "project_tally": _audit_redact(store.project_tally(project=project)),
            "spend_rows": _audit_redact(spend),
            "outcomes": _audit_redact(outcomes),
            "kpis": _audit_redact(kpis),
            "outcome_kpi_links": _audit_redact(outcome_links),
        },
        "deliverables": {
            "boards": project_boards,
            "records": deliverables,
            "milestones": deliverable_milestones,
            "task_links": deliverable_task_links,
        },
        "archives": {"tasks": archived_tasks},
    }
    return _audit_redact(bundle)


def execute_mapping_result(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    return execute(project=project)


audit_export = execute
