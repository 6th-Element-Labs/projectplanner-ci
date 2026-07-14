"""Residual store implementation moved out of ``store.py`` (ARCH-MS-45).

Phase 1 exit requires ``store.py`` to be a thin compatibility façade. This module
owns the remaining persistence/control-plane helpers that have not yet been
split into dedicated repositories. ``store.py`` re-exports these symbols.
"""
import json
import hashlib
import copy
import os
import re
import shutil
import sqlite3
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

import evidence_claims
import deliverable_gates
import deliverable_policy
import narration_outbox
import push_verification
import scripts.switchboard_path  # noqa: F401 — make src/switchboard importable

# Module-level constants + static config live in constants.py (ARCH-2); re-exported
# here so `import store` callers keep seeing store.DEFAULT_PROJECT, store.BUILTIN_PROJECTS, etc.
from constants import *  # noqa: F401,F403
from db.core import *  # noqa: F401,F403 — Layer-0 primitives extracted to db/core.py (ARCH-3)
from db.schema import *  # noqa: F401,F403 — schema/DDL extracted to db/schema.py (ARCH-4)
from db.connection import *  # noqa: F401,F403 — conn/resolve extracted to db/connection.py (ARCH-5)
from rag_store import *        # noqa: F401,F403
from digests_store import *    # noqa: F401,F403
from inbox_store import *      # noqa: F401,F403
from summaries_store import *  # noqa: F401,F403
from decisions_store import *  # noqa: F401,F403
from jobs_store import *       # noqa: F401,F403
from switchboard.storage.repositories.runner import (
    _runner_session_row,
    _upsert_runner_session_in,
    claim_runner_control_request,
    complete_runner_control_request,
    get_runner_session,
    list_runner_control_requests,
    list_runner_sessions,
    request_runner_control,
    upsert_runner_session,
)
from switchboard.storage.repositories.access import *  # noqa: F401,F403 — ARCH-MS-24/30
from switchboard.storage.repositories.tasks import *  # noqa: F401,F403 — ARCH-MS-31
from switchboard.storage.repositories.claims import *  # noqa: F401,F403 — ARCH-MS-32
from switchboard.storage.repositories.coordination import *  # noqa: F401,F403 — ARCH-MS-33
from switchboard.storage.repositories.provenance import *  # noqa: F401,F403 — ARCH-MS-34
from switchboard.storage.repositories.provenance import (  # noqa: F401
    StoreProvenanceRepository,
    default_provenance_repository,
)  # ARCH-MS-27/34
from switchboard.storage.repositories.deliverables import *  # noqa: F401,F403 — ARCH-MS-35
from switchboard.storage.repositories.work_sessions import *  # noqa: F401,F403 — ARCH-MS-46
from switchboard.storage.repositories.work_sessions import (  # noqa: F401
    StoreWorkSessionsRepository,
    default_work_sessions_repository,
)  # ARCH-MS-46
from switchboard.storage.repositories.external_ci import *  # noqa: F401,F403 — ARCH-MS-47
from switchboard.storage.repositories.external_ci import (  # noqa: F401
    StoreExternalCiRepository,
    default_external_ci_repository,
)  # ARCH-MS-47
from switchboard.storage.repositories.external_effects import *  # noqa: F401,F403 — ARCH-MS-54
from switchboard.storage.repositories.external_effects import (  # noqa: F401
    StoreExternalEffectsRepository,
    default_external_effects_repository,
)  # ARCH-MS-54
from switchboard.storage.repositories.activity import *  # noqa: F401,F403 — ARCH-MS-55
from switchboard.storage.repositories.activity import (  # noqa: F401
    StoreActivityRepository,
    default_activity_repository,
)  # ARCH-MS-55
from switchboard.storage.repositories.lifecycle_cleanup import *  # noqa: F401,F403 — ARCH-MS-62
from switchboard.storage.repositories.lifecycle_cleanup import (  # noqa: F401
    StoreLifecycleCleanupRepository,
    default_lifecycle_cleanup_repository,
)  # ARCH-MS-62
from switchboard.storage.repositories.narration import *  # noqa: F401,F403 — ARCH-MS-56
from switchboard.storage.repositories.narration import (  # noqa: F401
    StoreNarrationRepository,
    default_narration_repository,
)  # ARCH-MS-56
from switchboard.storage.repositories.plan_chat import *  # noqa: F401,F403 — ARCH-MS-57
from switchboard.storage.repositories.plan_chat import (  # noqa: F401
    StorePlanChatRepository,
    default_plan_chat_repository,
)  # ARCH-MS-57
from switchboard.storage.repositories.publication import *  # noqa: F401,F403 — ARCH-MS-47
from switchboard.storage.repositories.publication import (  # noqa: F401
    StorePublicationRepository,
    default_publication_repository,
)  # ARCH-MS-47
from switchboard.storage.repositories.projects import *  # noqa: F401,F403 — ARCH-MS-48
from switchboard.storage.repositories.projects import (  # noqa: F401
    StoreProjectsRepository,
    default_projects_repository,
)  # ARCH-MS-48
from switchboard.storage.repositories.kpis_economics import *  # noqa: F401,F403 — ARCH-MS-49
from switchboard.storage.repositories.kpis_economics import (  # noqa: F401
    StoreKpisEconomicsRepository,
    default_kpis_economics_repository,
)  # ARCH-MS-49
from switchboard.storage.repositories.review_verdicts import *  # noqa: F401,F403 — COORD-18
from switchboard.storage.repositories.review_verdicts import (  # noqa: F401
    ReviewVerdictRepository,
    default_review_verdict_repository,
)  # COORD-18
from switchboard.storage.repositories.deliverables import (  # noqa: F401
    CLOSURE_REPORT_HISTORY_LIMIT,
    PROOF_REQUIREMENTS_SCHEMA,
    StoreDeliverablesRepository,
    default_deliverables_repository,
)  # ARCH-MS-35
from switchboard.storage.repositories.coordination import (  # noqa: F401
    StoreCoordinationRepository,
    default_coordination_repository,
)  # ARCH-MS-27/33
from switchboard.storage.repositories.claims import (  # noqa: F401
    StoreClaimsRepository,
    default_claims_repository,
)  # ARCH-MS-27/32
from switchboard.storage.repositories.tasks import (  # noqa: F401
    StoreTaskRepository,
    default_task_repository,
)  # ARCH-MS-27/31
from switchboard.domain.access.identity import (
    binding_for_principal,
    binding_for_registered_agent,
    binding_for_system_actor,
    is_unbound_system_actor,
    shared_token_binding_error,
    validate_system_actor_fields,
    write_binding_activity_payload,
)
from switchboard.domain.provenance.preflight import (  # noqa: F401 — ARCH-MS-58
    _repo_git,
    _repo_git_dir,
    _repo_list_candidate_files,
    _repo_merge_state,
    _repo_parse_status,
    _repo_preflight_finding,
    _repo_remote_slug,
    _repo_scan_conflict_markers,
    _repo_worktree_collisions,
    repo_preflight,
)
from switchboard.domain.provenance.preflight import *  # noqa: F401,F403 — ARCH-MS-58
from switchboard.application.commands.pre_tool_check import (  # noqa: F401 — ARCH-MS-60
    _pre_tool_classify,
    _pre_tool_decision,
    _pre_tool_input,
    _pre_tool_relpath,
    _pre_tool_requested_profile,
    _pre_tool_target_path,
    _record_pre_tool_activity,
    pre_tool_check,
)
from switchboard.application.commands.merge_gate import (  # noqa: F401 — ARCH-MS-61
    _merge_gate_bool,
    _merge_gate_context_passed,
    _merge_gate_context_rows,
    _merge_gate_finding,
    _merge_gate_pr_evidence,
    _merge_gate_pr_number,
    _merge_gate_pr_ref,
    _merge_gate_required_contexts,
    _merge_gate_status_contexts,
    merge_gate,
)
from switchboard.domain.board.tasks import (
    EDITABLE_TASK_FIELDS,
    READY_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    apply_terminal_done_view as _apply_terminal_done_view,
    block_done_without_provenance,
    build_dependency_state,
    dependency_rows_from_lookup,
    is_terminal_done_task as _is_terminal_done_task,
    normalize_depends_on as _normalize_depends_on,
    rationale_state as _rationale_state,
)
from switchboard.domain.coordination.delivery import (
    build_message_delivery_receipt,
    classify_agent_delivery,
    infer_runtime_for_agent,
    runtime_matches_selector,
)
from switchboard.domain.coordination.terminal import (
    TERMINAL_RUNNER_STATUSES,
    TERMINAL_WAKE_STATUSES,
)
from switchboard.domain.deliverables.lifecycle import (
    BREAKDOWN_PROPOSAL_STATUSES,
    DELIVERABLE_ID_RE,
    DELIVERABLE_MILESTONE_STATUSES,
    DELIVERABLE_STATUSES,
    PROJECT_BOARD_ID_RE,
    PROJECT_BOARD_KINDS,
    PROJECT_BOARD_STATUSES,
    normalize_deliverable_id,
    normalize_project_board_id,
    validate_deliverable_status,
)
from switchboard.domain.provenance.git import (
    EVIDENCE_HASH_RE,
    has_done_provenance as _has_done_provenance,
    offline_evidence_from_state as _offline_evidence_from_state,
    provenance_summary as _provenance_summary,
    valid_evidence_hash as _valid_evidence_hash,
)


# Fields a PATCH may change (everything an editor touches in an Asana-style board).
EDITABLE = list(EDITABLE_TASK_FIELDS)
task_repository = default_task_repository()
claims_repository = default_claims_repository()
coordination_repository = default_coordination_repository()
provenance_repository = default_provenance_repository()
deliverables_repository = default_deliverables_repository()
work_sessions_repository = default_work_sessions_repository()
external_ci_repository = default_external_ci_repository()
external_effects_repository = default_external_effects_repository()
activity_repository = default_activity_repository()
lifecycle_cleanup_repository = default_lifecycle_cleanup_repository()
narration_repository = default_narration_repository()
plan_chat_repository = default_plan_chat_repository()
publication_repository = default_publication_repository()
projects_repository = default_projects_repository()
kpis_economics_repository = default_kpis_economics_repository()
review_verdict_repository = default_review_verdict_repository
access_repository = default_access_repository()

from switchboard.domain.bug_intake.policy import (  # noqa: F401 — ARCH-MS-59
    BUG_FAILURE_CLASSES,
    BUG_INTAKE_POLICY,
    BUG_REPORT_REQUIRED_FIELDS,
    BUG_SEVERITIES,
    FAIL_FIX_FAILURE_CLASSES,
    FAIL_FIX_REQUIRED_FIELDS,
    _bug_report_description,
    _bug_report_value_present,
    _bug_title,
    _failure_class_detail,
    bug_intake_policy,
    fail_fix_signal_schema,
)

# Plan-level sections that are not per-task (kept verbatim from the seed snapshot).

# ARCH-MS-43: canonical protocol envelope lives in domain/ixp; re-export for
# `import store` callers that still read store.PROTOCOL_ENVELOPE.
from switchboard.domain.ixp.protocol import PROTOCOL_ENVELOPE  # noqa: E402


def _control_plane_timeout_s() -> float:
    return _sqlite_timeout_s("PM_CONTROL_PLANE_SQLITE_TIMEOUT_S", 2.0)


def _control_plane_conn(project: str = DEFAULT_PROJECT):
    return _conn(project, timeout_s=_control_plane_timeout_s())


def _control_plane_unavailable(operation: str, project: str, started_at: float,
                               exc: Exception) -> Dict[str, Any]:
    return {
        "error": "control_plane_unavailable",
        "reason": "sqlite_busy",
        "operation": operation,
        "project": project,
        "elapsed_ms": int((time.time() - started_at) * 1000),
        "timeout_ms": int(_control_plane_timeout_s() * 1000),
        "message": str(exc),
    }


def submit_bug(data: Dict[str, Any], actor: str = "agent",
               project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Thin store façade — orchestration lives in application/commands/submit_bug."""
    from switchboard.application.commands.submit_bug import execute_mapping_result
    return execute_mapping_result(data, actor=actor, project=project)



def control_plane_probe(project: str = DEFAULT_PROJECT, lane: str = "",
                        include_heavy: bool = False) -> Dict[str, Any]:
    """Tiny read-only timing probe for separating server work from bridge/client time."""
    started = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    lane_filter = (lane or "").strip()

    def measure(name: str, fn):
        op_started = time.perf_counter()
        try:
            summary = fn()
            ok = not (isinstance(summary, dict) and summary.get("error"))
        except sqlite3.OperationalError as exc:
            if _sqlite_busy(exc):
                summary = _control_plane_unavailable(name, project, time.time(), exc)
                ok = False
            else:
                raise
        except Exception as exc:
            summary = {"error": type(exc).__name__, "message": str(exc)}
            ok = False
        checks.append({
            "name": name,
            "ok": ok,
            "elapsed_ms": round((time.perf_counter() - op_started) * 1000, 3),
            "payload_bytes": _json_size_bytes(summary),
            "summary": summary,
        })
        return summary

    cursor_summary = measure("activity_cursor", lambda: {"cursor": _activity_cursor(project)})
    cursor = int(cursor_summary.get("cursor") or 0) if isinstance(cursor_summary, dict) else 0

    def host_summary() -> Dict[str, Any]:
        hosts = list_agent_hosts(project=project)
        if hosts and isinstance(hosts[0], dict) and hosts[0].get("error"):
            return hosts[0]
        return {
            "host_count": len(hosts),
            "stale_count": sum(1 for h in hosts if h.get("stale")),
        }

    measure("list_agent_hosts", host_summary)

    def delta_summary() -> Dict[str, Any]:
        delta = get_activity_delta(since_cursor=cursor, lane=lane_filter, project=project)
        return {
            "cursor": delta.get("cursor"),
            "update_count": len(delta.get("updates") or []),
            "lane": lane_filter,
        }

    measure("get_lane_delta_empty", delta_summary)

    if include_heavy:
        def board_summary_probe() -> Dict[str, Any]:
            payload = board_payload(project=project)
            return {
                "task_count": payload.get("rollups", {}).get("total_tasks"),
                "workstream_count": payload.get("rollups", {}).get("total_workstreams"),
                "payload_under_test_bytes": _json_size_bytes(payload),
            }

        measure("board_payload_heavy", board_summary_probe)

    result = {
        "project": project,
        "lane": lane_filter,
        "include_heavy": include_heavy,
        "server_elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "checks": checks,
        "interpretation": (
            "Compare client wall time to server_elapsed_ms. If client wall time is much larger, "
            "the excess is outside Switchboard Python/SQLite: TLS/network, MCP bridge dispatch, "
            "response framing, payload transfer, or client-side scheduling."
        ),
    }
    result["approx_response_bytes"] = _json_size_bytes(result)
    return result


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
    """Repo root for evidence/path resolution after store.py moved under src/."""
    configured = (os.environ.get("PM_REPO_PATH") or "").strip()
    if configured:
        return os.path.abspath(configured)
    # shell.py lives at src/switchboard/storage/repositories/shell.py → four parents up.
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
    )


def _evidence_claim_reports(c: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = _audit_table_rows(c, "activity", order_by="created_at, id")
    for row in rows:
        row["payload"] = _json_payload(row.get("payload") or "")
    return evidence_claims.evaluate_activities(rows, _canonical_repo_root())


def _audit_tasks(c: sqlite3.Connection, project: str) -> List[Dict[str, Any]]:
    tasks: List[Dict[str, Any]] = []
    rows = c.execute("SELECT * FROM tasks ORDER BY sort_order, task_id").fetchall()
    for row in rows:
        task = _task_row(row)
        task["git_state"] = _load_git_state(c, task["task_id"])
        task["provenance"] = _provenance_summary(task["git_state"])
        task["active_claims"] = _active_task_claims_in(c, task["task_id"])
        task["tally"] = task_tally(task["task_id"], project=project)
        tasks.append(_audit_redact(task))
    return tasks


def _audit_registry_scope(project: str) -> Dict[str, Any]:
    init_project_registry()
    with _registry_conn() as c:
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


def audit_export(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Versioned enterprise evidence bundle for audit/retention.

    The bundle preserves the evidence graph needed to answer who acted, under whose authority, at
    what cost, and with what proof, without exposing bearer token hashes, password hashes, session
    hashes, or raw secrets.
    """
    init_db(project)
    generated_at = time.time()
    with _conn(project) as c:
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
            public_principal_record(_principal_from_row(row), project=project)
            for row in c.execute("SELECT * FROM principals ORDER BY created_at, id").fetchall()
        ]
        access_sessions = _audit_table_rows(
            c, "auth_sessions", order_by="created_at, session_id")
        presence = [_presence_row(row, now=generated_at)
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
            _runner_session_row(row, now=generated_at, include_claim=True, c=c)
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
        git_state = [_git_state_row(row) for row in c.execute(
            "SELECT * FROM task_git_state ORDER BY updated_at, task_id"
        ).fetchall()]
        spend = [_spend_row(row) for row in c.execute(
            "SELECT * FROM llm_spend ORDER BY created_at, id"
        ).fetchall()]
        outcomes = [_outcome_row(row) for row in c.execute(
            "SELECT * FROM outcomes ORDER BY created_at, id"
        ).fetchall()]
        kpis = [dict(row) for row in c.execute(
            "SELECT * FROM kpis ORDER BY created_at, id"
        ).fetchall()]
        outcome_links = [dict(row) for row in c.execute(
            "SELECT * FROM outcome_kpi_links ORDER BY created_at, id"
        ).fetchall()]
        project_boards = [_project_board_row(row, project=project) for row in c.execute(
            "SELECT * FROM project_boards ORDER BY updated_at, id"
        ).fetchall()]
        deliverables = [_deliverable_row(row) for row in c.execute(
            "SELECT * FROM deliverables ORDER BY updated_at, id"
        ).fetchall()]
        deliverable_milestones = [_deliverable_milestone_row(row) for row in c.execute(
            "SELECT * FROM deliverable_milestones ORDER BY sort_order, created_at, id"
        ).fetchall()]
        deliverable_task_links = [_deliverable_link_row(row) for row in c.execute(
            "SELECT * FROM deliverable_task_links ORDER BY created_at, id"
        ).fetchall()]
        archived_tasks = _audit_json_rows(
            c, "archived_tasks", ("snapshot_json",), order_by="created_at, archive_id")
        work_sessions = [_work_session_row(row) for row in c.execute(
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
            "project_tally": _audit_redact(project_tally(project=project)),
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




def get_working_agreement(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Canonical connect-time rules for agents in this workspace."""
    override = get_meta("working_agreement", {}, project=project) or {}
    access = project_access(project)
    repo_topology = get_project_repo_topology(project)
    default = {
        "project": project,
        "project_hierarchy": repo_topology.get("project_hierarchy"),
        "project_boundary": access.get("boundary") or f"Only work belonging to project={project} belongs here.",
        "project_purpose": access.get("purpose") or f"{project} work control plane",
        "project_owner": access.get("owner_user_id") or access.get("org_id") or "",
        "repo_topology": repo_topology,
        "repo_role_guide": repo_topology_role_guide(project),
        "session_policy_profiles": get_session_policy_profiles(project),
        "work_session_contract": work_session_contract(project),
        "code_repo_gate": repo_topology.get("code_repo_gate"),
        "protocol": protocol_envelope(),
        "canonical_main_sha": get_meta("canonical_main_sha", None, project=project),
        "branch_convention": "claude/<TASK-ID>-<slug>",
        "definition_of_done": "Done means merged/rebased into the intended branch with recorded GitHub/default-branch provenance, or verified non-code work with recorded offline evidence provenance; implemented work with branch/head_sha/PR evidence is In Review.",
        "done_policy": {
            "mode": "git_merge_verified",
            "agent_may_set_done": False,
            "requires_evidence": True,
            "requires_merge_provenance": True,
            "code_tasks_should_include_git_evidence": True,
            "implemented_pr_status": "In Review",
            "done_sources": ["github_pr_merged", "default_branch_backfill", "offline_evidence_verified"],
        },
        "push_before_claiming_progress": True,
        "agent_call_patterns": {
            "writes": (
                "Serialize MCP writes to Switchboard: issue one write at a time. "
                "If SQLite reports 'database is locked', wait 5-15 seconds and retry "
                "the same write; do not start a parallel write burst."
            ),
            "heavy_reads": (
                "Never fan out parallel search_tasks, list_deliverables, or board_summary "
                "calls. Run these heavier reads one at a time."
            ),
            "polling": (
                "Prefer get_lane_delta for polling. Call board_summary at most once per "
                "agent session unless the operator explicitly requests a fresh full snapshot."
            ),
            "diagnostics": (
                "Use control_plane_probe to separate Switchboard server latency from "
                "network, MCP bridge, transfer, or client-side latency."
            ),
        },
        "claim_before_starting": (
            "Before building anything, search_tasks for the feature area and claim (or create) "
            "the board task — this prevents two agents shipping the same work. Fleet PRs on the "
            "canonical repo are checked by the 'Switchboard / claim gate' commit status: a PR that "
            "references no claimed task or Work Session is flagged (SESSION-12)."
        ),
        "merge_strategy": "squash",
        "main_writes": "PR only — never push main directly",
        "github_lifecycle": [
            "push the task branch",
            "open or update the PR against the intended branch",
            "include branch, head_sha, pr_number/pr_url in complete_claim evidence",
            "complete_claim moves the task to In Review and releases the claim",
            "after merge/rebase reaches the intended branch, the GitHub webhook or default-branch backfill stamps merged_sha and marks Done",
            "for non-PR/offline work, a verifier uses the offline-evidence path after In Review to stamp provenance and mark Done",
        ],
        "safe_merge_protocol": {
            "merge_authority": "Agents may merge only when their control registration, task instructions, or the human operator explicitly allow it.",
            "target_branch_rule": "Merge into the intended branch from the task/PR; do not assume master/main if the board or PR says otherwise.",
            "pre_merge": [
                "fetch origin and inspect the current target branch head",
                "rebase or merge the task branch onto the current target branch",
                "resolve conflicts intentionally; never overwrite unrelated user/agent work",
                "rerun the relevant tests/checks after the rebase or conflict resolution",
                "verify git status is clean except for intentional committed changes",
                "push the updated branch and ensure the PR points at the pushed head",
            ],
            "merge": [
                "merge through GitHub or the configured merge queue when available",
                "prefer the repository's configured squash/merge strategy",
                "do not force-merge red checks, missing reviews, or unexpected file changes",
            ],
            "post_merge": [
                "fetch/pull the target branch after merge",
                "record the resulting merged_sha or target branch head in evidence",
                "verify the task's changed files/content are present on the intended branch",
                "let the GitHub webhook or default-branch provenance path mark Done",
                "if the webhook is unavailable, run or request reconcile/backfill rather than setting Done manually",
            ],
        },
        "fail_fix_early_policy": {
            "summary": "Surface real failures immediately and repair them before they spread.",
            "schema": fail_fix_signal_schema(),
            "surface_immediately": [
                "missing data",
                "broken connections",
                "invalid inputs",
                "stale branches",
                "absent permissions",
                "malformed payloads",
                "failed checks",
            ],
            "do_not_hide_with": [
                "placeholder values",
                "silent defaults",
                "optimistic status updates",
                "fallbacks that make the workflow look green",
            ],
            "fallback_rule": (
                "Fallbacks are allowed only when they are visible, named, and preserve the "
                "original failing signal with an auditable red/yellow status, monitor event, "
                "reconcile finding, task comment, or blocker."
            ),
            "agent_rule": (
                "When a gate uncovers an environment, ingestion, normalization, protocol, "
                "auth, or workflow problem, treat the discovered problem as part of the task "
                "until it is repaired or deliberately handed off."
            ),
            "bug_reporting": (
                "If the failure is product-level or repeated, file it through submit_bug with "
                "one of the fail_fix_signal.v1 failure_class values and complete evidence."
            ),
        },
        "bug_intake_policy": bug_intake_policy(),
        "ports_doc": "docs/PORTS.md",
        "byo_data": True,
        "session_start_sequence": [
            "get_working_agreement(project)",
            "register_agent",
            "inbox(unacked)",
            "check+claim before first write",
        ],
        "deliverable_first_startup": {
            "doc": "docs/DELIVERABLE-FIRST-STARTUP.md",
            "ownership": {
                "projects": "repo/trust/policy/access/CI/model/budget/Done authority",
                "boards_missions": "live outcome cockpits; boards own execution routing",
                "deliverables": "shipped-value definition, end_state, milestones, cross-board proof rollup",
                "tasks": "execution units on exactly one project workstream",
            },
            "mission_home_project": (
                "The project database that owns the deliverable record. Pass this as project= "
                "on mission tools even when linked tasks live on other projects."
            ),
            "boot_sequence": [
                "prepare_agent_session(project=<mission_home>, deliverable_id=... | board_id=... | mission_id=...)",
                "get_mission_status(project=<mission_home>, deliverable_id=...)",
                "Read end_state, acceptance_criteria, policy_constraints, milestones, linked_tasks, blockers, next_actions",
                "Workers: claim_next(agent_id, project=<mission_home>, deliverable_id=..., milestone_id=...)",
                "Workers: complete_claim(..., project=<task_project>, evidence={mission_project, deliverable_id, milestone_id, branch, head_sha, pr_url})",
            ],
            "coordinator_sequence": [
                "get_mission_status",
                "run_mission_coordinator(deliverable_id=..., coordinator_agent_id=..., worker_agent_id=...)",
                "Follow next_actions (approve_breakdown, claim_task, verify_merge_provenance, request_human_approval)",
                "claim_next(deliverable_id=...) or approve_deliverable_breakdown",
                "update_mission_narrative when material state changes",
            ],
        },
        "session_start_sequence_deliverable": [
            "prepare_agent_session(project, deliverable_id|board_id|mission_id)",
            "get_mission_status",
            "register_agent",
            "inbox(unacked)",
            "claim_next(deliverable_id=...) or claim_task on an explicit linked task",
        ],
        "agent_completion_rule": "complete_claim(evidence=...) records branch/head_sha/PR/offline evidence and moves to In Review; agents cannot mark Done. Done is reserved for GitHub/default-branch merge provenance or verifier-stamped offline evidence.",
    }
    agreement = {**default, **override, "project": project}
    if "done_policy" not in override:
        agreement["done_policy"] = default["done_policy"]
        agreement["definition_of_done"] = default["definition_of_done"]
        agreement["agent_completion_rule"] = default["agent_completion_rule"]
    return agreement


SEVERITY_VALUE = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _severity_value(severity: str) -> int:
    return SEVERITY_VALUE.get((severity or "").strip().lower(), 0)


# ---- incremental RAG corpus (Phase 5) — ingested artifacts, persisted + shared --------
# ---- Live Inbox queue (Phase 5.5) — triaged inbound artifacts awaiting review ----------
# Hot-read cache (lite board, plan signals, mission status/dependency-graph) extracted to
# read_cache.py per ADR-0006 — it's a self-contained leaf (only runs a builder callback,
# no store dependency). Serve-stale-while-revalidate + the stamp/TTL invalidation contract
# live there. Re-exported so store.ttl_read_cache / store._READ_CACHE keep working for the
# callers below (and signals.py, the perf tests).
from read_cache import _READ_CACHE, ttl_read_cache  # noqa: E402,F401
