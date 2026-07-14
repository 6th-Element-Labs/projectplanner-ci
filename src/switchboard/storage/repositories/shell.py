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
import subprocess
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
publication_repository = default_publication_repository()
projects_repository = default_projects_repository()
kpis_economics_repository = default_kpis_economics_repository()
access_repository = default_access_repository()

BUG_INTAKE_POLICY = {
    "scope": "write:bug_intake",
    "agent_role": (
        "Receive agent-discovered bugs, normalize them into reproducible BUG reports, "
        "dedupe them, score severity, and prepare approval-ready conversion proposals."
    ),
    "allowed_without_human_approval": [
        "create or update BUG intake records through the dedicated bug-intake surface",
        "link duplicate BUG reports to a canonical BUG task",
        "request missing reproduction evidence from the reporting agent",
        "assign severity_hint and affected_surface on BUG intake records",
    ],
    "forbidden_without_human_approval": [
        "create implementation work outside the BUG lane",
        "mark converted implementation work Ready or claimable",
        "change priority, sort_order, is_blocking, or dependency-critical fields",
        "dispatch, claim, wake, or otherwise start implementation work",
        "hide the original failing signal behind a green fallback",
    ],
    "conversion_gate": {
        "state_key": "human_gate",
        "required_fields": [
            "required",
            "source_bug_task_id",
            "target_workstream",
            "severity",
            "approval_reason",
            "approved_by",
            "approved_at",
        ],
        "unapproved_status": "human_approval_required",
        "approved_statuses": ["approved", "accepted", "waived"],
    },
    "approval_authority": (
        "A human operator or explicit coordinator policy may approve conversion. "
        "The approver, target lane, source BUG task, evidence, and rationale must be audited."
    ),
}
BUG_REPORT_REQUIRED_FIELDS = [
    "source_task",
    "observed_behavior",
    "expected_behavior",
    "repro_steps",
    "evidence",
    "severity_hint",
    "affected_surface",
]
BUG_SEVERITIES = {"low": "Low", "medium": "Medium", "high": "High", "critical": "High"}
FAIL_FIX_REQUIRED_FIELDS = [
    "source",
    "failure_class",
    "severity",
    "affected_surface",
    "observed_behavior",
    "expected_behavior",
    "repro_steps",
    "evidence",
    "task_id",
]
FAIL_FIX_FAILURE_CLASSES = {
    "missing_data": {
        "label": "Missing data",
        "default_severity": "medium",
        "description": "A required field, artifact, status, or provenance signal is absent.",
        "expected_signal": "Required data is present before workflow execution continues.",
    },
    "broken_connection": {
        "label": "Broken connection",
        "default_severity": "medium",
        "description": "A network, GitHub, MCP, provider, or service dependency cannot be reached.",
        "expected_signal": "The dependency returns a structured response or a loud connection error.",
    },
    "invalid_input": {
        "label": "Invalid input",
        "default_severity": "medium",
        "description": "A caller supplied a known field with an invalid value or unsafe state transition.",
        "expected_signal": "The invalid value is rejected before downstream state changes.",
    },
    "stale_branch": {
        "label": "Stale branch",
        "default_severity": "high",
        "description": "Git or board state points at a stale, missing, or unreachable branch/SHA.",
        "expected_signal": "The current branch, head SHA, and canonical main proof are reachable.",
    },
    "absent_permission": {
        "label": "Absent permission",
        "default_severity": "high",
        "description": "A principal lacks the scope, token, approval, or policy authority for an action.",
        "expected_signal": "The action is denied with the missing authority named.",
    },
    "malformed_payload": {
        "label": "Malformed payload",
        "default_severity": "medium",
        "description": "A request or stored payload is syntactically malformed or cannot be decoded.",
        "expected_signal": "Payload shape is validated and malformed input fails closed.",
    },
    "failed_gate": {
        "label": "Failed gate",
        "default_severity": "high",
        "description": "A CI, QA, review, human gate, or lifecycle gate failed or was bypassed.",
        "expected_signal": "The gate failure is visible and blocks release/dispatch until repaired.",
    },
    "unreachable_agent": {
        "label": "Unreachable agent",
        "default_severity": "medium",
        "description": "A directed agent, runtime, or host could not be reached or did not ack.",
        "expected_signal": "Delivery, mailbox, wakeability, and fallback state are explicit.",
    },
    "unbound_identity": {
        "label": "Unbound identity",
        "default_severity": "high",
        "description": "Work was written by a shared/system principal without a bound active runtime.",
        "expected_signal": "The runtime identity is registered, bound, and visible to operators.",
    },
    "hidden_fallback": {
        "label": "Hidden fallback",
        "default_severity": "critical",
        "description": "A fallback, placeholder, or optimistic status masks the original failure.",
        "expected_signal": "Fallbacks are named and preserve a red/yellow auditable signal.",
    },
}
BUG_FAILURE_CLASSES = set(FAIL_FIX_FAILURE_CLASSES)

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


def _activity_cursor(project: str = DEFAULT_PROJECT) -> int:
    with _control_plane_conn(project) as c:
        return int(c.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0] or 0)


def bug_intake_policy() -> Dict[str, Any]:
    return json.loads(json.dumps(BUG_INTAKE_POLICY))


def _bug_report_value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def fail_fix_signal_schema() -> Dict[str, Any]:
    return {
        "schema": "fail_fix_signal.v1",
        "required_fields": list(FAIL_FIX_REQUIRED_FIELDS),
        "failure_classes": {
            key: dict(value)
            for key, value in sorted(FAIL_FIX_FAILURE_CLASSES.items())
        },
        "reporting_rule": (
            "Preserve the original failing signal. Do not replace it with a placeholder, "
            "silent default, optimistic status, or hidden fallback."
        ),
        "visible_fallback_rule": (
            "Fallbacks are allowed only when they are named and leave an auditable "
            "red/yellow signal such as a BUG report, reconcile finding, monitor event, "
            "task comment, or blocker."
        ),
    }


def _failure_class_detail(failure_class: str) -> Optional[Dict[str, Any]]:
    detail = FAIL_FIX_FAILURE_CLASSES.get(_slug_token(failure_class or ""))
    return dict(detail) if detail else None


def _bug_title(surface: str, observed: str, explicit_title: str = "") -> str:
    if explicit_title and explicit_title.strip():
        return explicit_title.strip()[:160]
    summary = " ".join((observed or "").strip().split())
    if not summary:
        summary = "agent-submitted bug"
    if len(summary) > 96:
        summary = summary[:93].rstrip() + "..."
    surface = (surface or "unknown surface").strip()
    return f"{surface}: {summary}"[:160]


def _bug_report_description(report: Dict[str, Any]) -> str:
    evidence = report.get("evidence")
    if isinstance(evidence, (dict, list)):
        evidence_text = json.dumps(evidence, indent=2, sort_keys=True)
    else:
        evidence_text = str(evidence or "")
    failure_detail = _failure_class_detail(str(report.get("failure_class") or "")) or {}
    failure_label = failure_detail.get("label") or report.get("failure_class") or "(unspecified)"
    return "\n".join([
        f"Bug submitted by: {report.get('source_agent')}",
        f"Source task: {report.get('source_task')}",
        f"Affected surface: {report.get('affected_surface')}",
        f"Severity hint: {report.get('severity_hint')}",
        f"Failure class: {failure_label}",
        f"Expected fail-fix signal: {failure_detail.get('expected_signal') or '(unspecified)'}",
        f"Duplicate of: {report.get('duplicate_of') or '(none)'}",
        "",
        "Observed behavior:",
        str(report.get("observed_behavior") or ""),
        "",
        "Expected behavior:",
        str(report.get("expected_behavior") or ""),
        "",
        "Repro steps:",
        str(report.get("repro_steps") or ""),
        "",
        "Evidence:",
        evidence_text,
    ])


def add_comment(task_id: str, actor: str, text: str, kind: str = "comment",
                project: str = DEFAULT_PROJECT,
                hydrate_task: bool = True) -> Optional[Dict[str, Any]]:
    """Append task activity, optionally skipping the expensive task-detail readback.

    REST comment creation returns the updated task and keeps the default hydration.
    Acknowledgement-only callers such as the MCP ``add_comment`` tool can pass
    ``hydrate_task=False``: they still validate the task and commit the exact same
    activity/audit rows, but avoid loading provenance, sessions, CI, publication,
    and project context only to discard them.
    """
    now = time.time()
    with _conn(project) as c:
        if not c.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
            return None
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, kind, json.dumps({"text": text}), now))
        if is_unbound_system_actor(actor):
            active_agents = _active_agent_ids_for_task(c, task_id, now)
            if not active_agents:
                payload = {
                    "actor": actor,
                    "failure_class": "unbound_identity",
                    "expected_signal": FAIL_FIX_FAILURE_CLASSES["unbound_identity"]["expected_signal"],
                    "reason": "system_principal_write_without_active_agent",
                    "message": (
                        "This write came from a shared system token, but no active "
                        "agent session is registered on this task. Directed inbox "
                        "delivery to a named agent may not reach the runtime until "
                        "that runtime handshakes and drains its inbox."
                    ),
                }
                c.execute(
                    "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (task_id, "switchboard/identity", "principal.unbound_write",
                     json.dumps(payload, sort_keys=True), now),
                )
    return get_task(task_id, project) if hydrate_task else {"task_id": task_id}


def submit_bug(data: Dict[str, Any], actor: str = "agent",
               project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    payload = dict(data or {})
    missing = [field for field in BUG_REPORT_REQUIRED_FIELDS
               if not _bug_report_value_present(payload.get(field))]
    source_agent = (payload.get("source_agent") or actor or "").strip()
    if not source_agent:
        missing.append("source_agent")
    if missing:
        return {
            "error": "missing_required_fields",
            "missing": sorted(set(missing)),
            "message": "submit_bug requires a complete report; no BUG task was created.",
        }

    source_task = str(payload.get("source_task") or "").strip().upper()
    duplicate_of = str(payload.get("duplicate_of") or "").strip().upper()
    severity = str(payload.get("severity_hint") or "").strip().lower()
    if severity not in BUG_SEVERITIES:
        return {
            "error": "invalid_severity_hint",
            "allowed": sorted(BUG_SEVERITIES),
            "message": "severity_hint must be low, medium, high, or critical.",
        }
    failure_class = _slug_token(str(payload.get("failure_class") or ""))
    if failure_class and failure_class not in BUG_FAILURE_CLASSES:
        return {
            "error": "invalid_failure_class",
            "allowed": sorted(BUG_FAILURE_CLASSES),
            "schema": fail_fix_signal_schema(),
            "message": "failure_class is optional, but supplied values must match fail_fix_signal.v1.",
        }
    failure_detail = _failure_class_detail(failure_class) if failure_class else None

    with _conn(project) as c:
        source = c.execute("SELECT * FROM tasks WHERE task_id=?", (source_task,)).fetchone()
        if not source:
            return {
                "error": "unknown_source_task",
                "source_task": source_task,
                "message": "source_task must exist on this project; no BUG task was created.",
            }
        if duplicate_of:
            dup = c.execute("SELECT * FROM tasks WHERE task_id=?", (duplicate_of,)).fetchone()
            if not dup:
                return {
                    "error": "unknown_duplicate_of",
                    "duplicate_of": duplicate_of,
                    "message": "duplicate_of must name an existing BUG task; no BUG task was created.",
                }
            if (dup["workstream_id"] or "").upper() != "BUG":
                return {
                    "error": "duplicate_of_not_bug",
                    "duplicate_of": duplicate_of,
                    "message": "duplicate_of must point at a BUG task.",
                }

    now = time.time()
    report = {
        "schema": "bug_report.v1",
        "intake_status": "new",
        "source_task": source_task,
        "source_agent": source_agent,
        "reported_by": actor,
        "reported_at": now,
        "observed_behavior": str(payload.get("observed_behavior") or "").strip(),
        "expected_behavior": str(payload.get("expected_behavior") or "").strip(),
        "repro_steps": payload.get("repro_steps"),
        "evidence": _parse_jsonish(payload.get("evidence")),
        "severity_hint": severity,
        "affected_surface": str(payload.get("affected_surface") or "").strip(),
        "failure_class": failure_class or None,
        "failure_class_detail": failure_detail,
        "fail_fix_signal": {
            "schema": "fail_fix_signal.v1",
            "source": "submit_bug",
            "failure_class": failure_class or None,
            "severity": severity,
            "affected_surface": str(payload.get("affected_surface") or "").strip(),
            "observed_behavior": str(payload.get("observed_behavior") or "").strip(),
            "expected_behavior": str(payload.get("expected_behavior") or "").strip(),
            "repro_steps": payload.get("repro_steps"),
            "evidence": _parse_jsonish(payload.get("evidence")),
            "task_id": source_task,
            "expected_signal": (
                failure_detail or {}
            ).get("expected_signal") or str(payload.get("expected_behavior") or "").strip(),
        },
        "duplicate_of": duplicate_of or None,
    }
    task = create_task({
        "workstream_id": "BUG",
        "workstream_name": "BUG",
        "title": _bug_title(report["affected_surface"], report["observed_behavior"],
                            str(payload.get("title") or "")),
        "description": _bug_report_description(report),
        "status": "Triage",
        "phase": "Agent Intake P0",
        "owner_org": "6th Element Labs",
        "owner_person_or_role": "Bug Intake",
        "risk_level": BUG_SEVERITIES[severity],
        "depends_on": [],
    }, actor=actor, project=project)
    if not task:
        return {"error": "bug_task_not_created", "message": "BUG task creation failed."}

    full_state = set_agent_state(task["task_id"], "bug_report", report, project=project)
    report_event = {
        "bug_task_id": task["task_id"],
        "source_task": source_task,
        "source_agent": source_agent,
        "severity_hint": severity,
        "affected_surface": report["affected_surface"],
        "failure_class": report["failure_class"],
        "duplicate_of": duplicate_of or None,
        "evidence": report["evidence"],
    }
    append_activity("bug.submitted", actor, report_event,
                    task_id=task["task_id"], project=project)
    append_activity("bug.reported_from_task", actor, report_event,
                    task_id=source_task, project=project)
    bug = get_task(task["task_id"], project=project)
    return {"submitted": True, "bug": bug, "bug_report": report,
            "agent_state": full_state}


def get_activity_delta(since_cursor: int = 0, lane: str = "",
                       project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Return activity newer than since_cursor (activity.id rowid — monotonic, clock-skew-safe).
    lane filters to one workstream (e.g. 'ENGINE'). Returns
    {cursor, updates: [{task_id, status, title, workstream_id, kinds}]}.
    Use this for polling instead of list_tasks/board_summary — empty updates = zero tokens wasted."""
    lane_upper = lane.strip().upper() if lane else ""
    with _conn(project) as c:
        if lane_upper:
            rows = c.execute(
                """SELECT a.id, a.task_id, a.kind, a.actor,
                          t.status, t.title, t.workstream_id
                   FROM activity a
                   JOIN tasks t ON t.task_id = a.task_id
                   WHERE a.id > ? AND t.workstream_id = ?
                   ORDER BY a.id""",
                (since_cursor, lane_upper),
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT a.id, a.task_id, a.kind, a.actor,
                          t.status, t.title, t.workstream_id
                   FROM activity a
                   JOIN tasks t ON t.task_id = a.task_id
                   WHERE a.id > ?
                   ORDER BY a.id""",
                (since_cursor,),
            ).fetchall()
        git_states = {r["task_id"]: _load_git_state(c, r["task_id"]) for r in rows}
    if not rows:
        return {"cursor": since_cursor, "updates": []}
    new_cursor = rows[-1]["id"]
    by_task: Dict[str, Any] = {}
    for row in rows:
        tid = row["task_id"]
        if tid not in by_task:
            by_task[tid] = {"task_id": tid, "status": row["status"],
                            "title": row["title"], "workstream_id": row["workstream_id"],
                            "kinds": [], "git_state": git_states.get(tid, {})}
        by_task[tid]["status"] = row["status"]
        if row["kind"] not in by_task[tid]["kinds"]:
            by_task[tid]["kinds"].append(row["kind"])
    return {"cursor": new_cursor, "updates": list(by_task.values())}


def _merge_gate_finding(code: str, message: str, failure_class: str,
                        severity: str = "high", blocking: bool = True,
                        details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "failure_class": failure_class,
        "severity": severity,
        "blocking": bool(blocking),
        **(details or {}),
    }


def _merge_gate_pr_number(pr_url: str, pr_number: Any = None) -> int:
    if pr_number not in (None, ""):
        try:
            return int(pr_number)
        except (TypeError, ValueError):
            return 0
    match = GITHUB_PR_URL_RE.search((pr_url or "").strip())
    if not match:
        return 0
    try:
        return int(match.group(2))
    except (TypeError, ValueError):
        return 0


def _merge_gate_context_rows(value: Any) -> List[Dict[str, Any]]:
    if not value:
        return []
    rows: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        if any(k in value for k in ("context", "name", "state", "status", "conclusion")):
            rows.append(value)
        else:
            for context, state in value.items():
                rows.append({"context": context, "state": state})
        return rows
    if isinstance(value, list):
        for item in value:
            rows.extend(_merge_gate_context_rows(item))
    return rows


def _merge_gate_status_contexts(*sources: Any) -> Dict[str, str]:
    contexts: Dict[str, str] = {}
    for source in sources:
        for row in _merge_gate_context_rows(source):
            name = str(row.get("context") or row.get("name") or row.get("check_name") or "").strip()
            if not name:
                continue
            state = str(
                row.get("state")
                or row.get("status")
                or row.get("conclusion")
                or row.get("result")
                or ""
            ).strip().lower()
            contexts[name] = state
    return contexts


def _merge_gate_context_passed(state: str) -> bool:
    return (state or "").strip().lower() in {"success", "passed", "pass", "ok", "neutral", "skipped"}


def _merge_gate_required_contexts(topology: Dict[str, Any],
                                  evidence: Dict[str, Any]) -> List[str]:
    roles = topology.get("roles") or {}
    required: List[str] = []
    for role_name in ("canonical", "public_ci"):
        required.extend(_coerce_str_list((roles.get(role_name) or {}).get("required_status_contexts")))
    required.extend(_coerce_str_list(evidence.get("required_status_contexts")))
    required.extend(_coerce_str_list(evidence.get("required_contexts")))
    return list(dict.fromkeys([c for c in required if c]))


def _merge_gate_pr_evidence(pr_url: str, pr_number: int,
                            evidence: Dict[str, Any],
                            repo: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    supplied = evidence.get("github_pr") or evidence.get("pr_state") or evidence.get("pr") or {}
    if isinstance(supplied, dict) and supplied:
        return copy.deepcopy(supplied), {"source": "supplied_evidence"}
    if not repo or not pr_number:
        return {}, {"source": "missing", "reason": "pr_url_or_number_missing"}
    pr = _github_pr(repo, pr_number, _github_token())
    if pr:
        return pr, {"source": "github_api"}
    return {}, {"source": "github_api", "reason": "unavailable"}


def _merge_gate_pr_ref(pr: Dict[str, Any], side: str, field: str) -> str:
    obj = pr.get(side) or {}
    return str(obj.get(field) or "").strip()


def _merge_gate_bool(value: Any, default: Optional[bool] = None) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "ok", "pass", "passed", "clean"}:
        return True
    if text in {"0", "false", "no", "n", "fail", "failed", "dirty", "blocked"}:
        return False
    return default


def merge_gate(payload: Dict[str, Any], actor: str = "system",
               principal_id: str = "", project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Evaluate whether an agent may safely request/perform a PR merge.

    This is a gate, not a merge executor. It never marks a task Done; GitHub webhooks or
    reconcile remain the only code-merge provenance path.
    """
    now = time.time()
    payload = dict(payload or {})
    evidence = _parse_evidence(payload.get("evidence") or {})
    merged_payload = {**payload, **evidence}
    task_id = str(merged_payload.get("task_id") or "").strip().upper()
    agent_id = str(merged_payload.get("agent_id") or "").strip()
    claim_id = str(merged_payload.get("claim_id") or "").strip()
    work_session_id = str(merged_payload.get("work_session_id") or "").strip()
    pr_url = str(merged_payload.get("pr_url") or "").strip()
    pr_number = _merge_gate_pr_number(pr_url, merged_payload.get("pr_number"))
    repo = (
        str(merged_payload.get("repo") or "").strip()
        or _github_repo_from_pr_url(pr_url)
        or get_project_github_repo(project)
    )
    target_branch = str(merged_payload.get("target_branch") or "").strip()
    findings: List[Dict[str, Any]] = []
    if not has_project(project):
        findings.append(_merge_gate_finding(
            "unknown_project", f"Unknown project: {project}", "invalid_input"))
        return {"schema": MERGE_GATE_SCHEMA, "ok": False, "status": "blocked",
                "project": project, "task_id": task_id, "findings": findings}
    topology = get_project_repo_topology(project)
    roles = topology.get("roles") or {}
    canonical = roles.get("canonical") or {}
    default_branch = (canonical.get("default_branch") or "master").strip() or "master"
    if not target_branch:
        target_branch = default_branch
    task = get_task(task_id, project=project) if task_id else None
    if not task:
        findings.append(_merge_gate_finding(
            "task_not_found", "Merge gate requires a known task_id.", "missing_data",
            details={"task_id": task_id}))
        task = {"task_id": task_id, "agent_state": {}}
    role_info = get_project_repo_role(repo, project=project)
    if not role_info.get("canonical"):
        findings.append(_merge_gate_finding(
            "repo_role_cannot_merge",
            "Only the project canonical repo can be merged as code truth.",
            "failed_gate",
            details={"repo": repo, "repo_role": role_info.get("role"),
                     "evidence_only": role_info.get("evidence_only")}))
    if not topology.get("code_repo_gate", {}).get("passed"):
        findings.append(_merge_gate_finding(
            "canonical_repo_missing",
            "Project canonical repo is not configured; merge provenance cannot be trusted.",
            "missing_data",
            details={"code_repo_gate": topology.get("code_repo_gate")}))
    if target_branch != default_branch:
        findings.append(_merge_gate_finding(
            "wrong_target_branch",
            f"Merge target {target_branch!r} does not match canonical default branch {default_branch!r}.",
            "failed_gate",
            details={"target_branch": target_branch, "default_branch": default_branch}))

    pr, pr_source = _merge_gate_pr_evidence(pr_url, pr_number, merged_payload, repo)
    if not pr:
        findings.append(_merge_gate_finding(
            "github_pr_state_unavailable",
            "Merge gate requires GitHub PR state or supplied PR evidence.",
            "broken_connection" if pr_source.get("source") == "github_api" else "missing_data",
            details={"pr_url": pr_url, "pr_number": pr_number, "source": pr_source}))
    else:
        if not pr_url:
            pr_url = str(pr.get("html_url") or "").strip()
        if not pr_number:
            pr_number = int(pr.get("number") or 0)
        base_ref = _merge_gate_pr_ref(pr, "base", "ref")
        head_ref = _merge_gate_pr_ref(pr, "head", "ref")
        head_sha = _merge_gate_pr_ref(pr, "head", "sha")
        if base_ref and base_ref != target_branch:
            findings.append(_merge_gate_finding(
                "wrong_target_branch",
                f"PR base {base_ref!r} does not match requested target {target_branch!r}.",
                "failed_gate",
                details={"pr_base": base_ref, "target_branch": target_branch}))
        if pr.get("draft") is True:
            findings.append(_merge_gate_finding(
                "draft_pr", "Draft PRs cannot pass the merge gate.", "failed_gate"))
        mergeable = _merge_gate_bool(pr.get("mergeable"), default=True)
        merge_state = str(
            pr.get("mergeable_state")
            or pr.get("mergeStateStatus")
            or pr.get("merge_state")
            or ""
        ).strip().lower()
        if mergeable is False or merge_state in {"dirty", "blocked", "behind", "unstable", "unknown"}:
            findings.append(_merge_gate_finding(
                "pr_not_mergeable",
                "GitHub PR state is not cleanly mergeable.",
                "failed_gate",
                details={"mergeable": pr.get("mergeable"), "merge_state": merge_state}))
        expected_head = str(
            merged_payload.get("head_sha")
            or (task.get("git_state") or {}).get("head_sha")
            or ""
        ).strip()
        if expected_head and head_sha and expected_head != head_sha:
            findings.append(_merge_gate_finding(
                "stale_head_sha",
                "PR head SHA does not match task/session evidence.",
                "stale_branch",
                details={"expected_head_sha": expected_head, "pr_head_sha": head_sha}))
        expected_branch = str(merged_payload.get("branch") or (task.get("git_state") or {}).get("branch") or "").strip()
        if expected_branch and head_ref and expected_branch != head_ref:
            findings.append(_merge_gate_finding(
                "stale_branch",
                "PR branch does not match task/session evidence.",
                "stale_branch",
                details={"expected_branch": expected_branch, "pr_branch": head_ref}))
        behind = pr.get("behind_by", pr.get("behind_count", 0))
        try:
            behind_count = int(behind or 0)
        except (TypeError, ValueError):
            behind_count = 0
        if behind_count > 0 or _merge_gate_bool(merged_payload.get("branch_up_to_date"), default=True) is False:
            findings.append(_merge_gate_finding(
                "stale_branch",
                "PR branch is behind target branch and needs rebase/merge.",
                "stale_branch",
                details={"behind": behind_count, "target_branch": target_branch}))
        if _merge_gate_bool(merged_payload.get("safe_rebase_required"), default=False) and not (
                merged_payload.get("safe_rebase_evidence") or merged_payload.get("rebased_at")):
            findings.append(_merge_gate_finding(
                "missing_safe_rebase_evidence",
                "Merge gate requires safe rebase evidence before merge.",
                "missing_data"))

    required_contexts = _merge_gate_required_contexts(topology, merged_payload)
    pr_contexts = _merge_gate_status_contexts(
        pr.get("status_contexts") if pr else None,
        pr.get("statusCheckRollup") if pr else None,
        pr.get("checks") if pr else None,
        merged_payload.get("status_contexts"),
        merged_payload.get("check_runs"),
        merged_payload.get("checks"),
    )
    external_ci = _external_ci_review_gate(task, evidence=merged_payload, project=project)
    missing_contexts = [
        context for context in required_contexts
        if not _merge_gate_context_passed(pr_contexts.get(context, ""))
    ]
    if missing_contexts and not external_ci.get("passed"):
        findings.append(_merge_gate_finding(
            "missing_required_status_contexts",
            "Required CI/status contexts are missing or not successful.",
            "failed_gate",
            details={"missing_contexts": missing_contexts,
                     "required_contexts": required_contexts,
                     "status_contexts": pr_contexts}))
    if external_ci.get("required") and not external_ci.get("passed"):
        findings.append(_merge_gate_finding(
            "external_ci_required",
            "External CI mirror evidence is required before merge.",
            "failed_gate",
            details={"external_ci": external_ci}))

    profile = _task_work_session_profile(
        task,
        str(merged_payload.get("session_policy_profile") or merged_payload.get("policy_profile") or ""),
        project=project,
    )
    profile_rules = _session_policy_profile_rules(profile, project=project)
    if not profile_rules:
        findings.append(_merge_gate_finding(
            "unknown_policy_profile",
            f"Unknown session policy profile: {profile or '<empty>'}.",
            "invalid_input",
            details={"known_profiles": sorted((get_session_policy_profiles(project).get("profiles") or {}).keys())}))

    session = None
    if work_session_id:
        session = get_work_session(work_session_id, project=project)
        if not session:
            findings.append(_merge_gate_finding(
                "work_session_not_found",
                "Merge gate work_session_id was not found.",
                "missing_data",
                details={"work_session_id": work_session_id}))
    elif claim_id:
        with _conn(project) as c:
            row = c.execute(
                "SELECT * FROM work_sessions WHERE claim_id=? ORDER BY updated_at DESC LIMIT 1",
                (claim_id,),
            ).fetchone()
            session = _work_session_row(row) if row else None
    require_session = (
        _merge_gate_bool(merged_payload.get("require_work_session"), default=False)
        or bool(profile_rules.get("merge_requires_work_session"))
    )
    if session:
        session_profile = _normalize_session_policy_profile(
            session.get("policy_profile") or profile or "")
        session_rules = _session_policy_profile_rules(session_profile, project=project) or profile_rules
        if session.get("repo_role") != "canonical":
            findings.append(_merge_gate_finding(
                "wrong_work_session_repo_role",
                "Merge gate requires a canonical Work Session.",
                "failed_gate",
                details={"work_session_id": session.get("work_session_id"),
                         "repo_role": session.get("repo_role")}))
        if session.get("dirty_status") == "dirty" and "dirty_work_session" in set(
                session_rules.get("deny_hygiene") or []):
            findings.append(_merge_gate_finding(
                "dirty_work_session",
                "Work Session is dirty; run repo preflight and commit or clean changes before merge.",
                "failed_gate",
                details={"work_session_id": session.get("work_session_id")}))
        if int(session.get("conflict_marker_count") or 0) > 0 and "conflict_markers" in set(
                session_rules.get("deny_hygiene") or []):
            findings.append(_merge_gate_finding(
                "conflict_markers",
                "Work Session reports conflict markers.",
                "failed_gate",
                details={"work_session_id": session.get("work_session_id")}))
        preflight = ((session.get("hygiene") or {}).get("repo_preflight") or {})
        if not preflight:
            findings.append(_merge_gate_finding(
                "missing_work_session_preflight",
                "Merge gate requires a recorded clean Work Session preflight.",
                "missing_data",
                details={"work_session_id": session.get("work_session_id")}))
        elif preflight.get("verdict") == "deny" or preflight.get("ok") is False:
            findings.append(_merge_gate_finding(
                "work_session_preflight_failed",
                "Work Session preflight is not clean.",
                "failed_gate",
                details={"work_session_id": session.get("work_session_id"),
                         "preflight": preflight}))
    elif require_session:
        findings.append(_merge_gate_finding(
            "work_session_required",
            f"Policy profile {profile} requires a Work Session for merge intent.",
            "missing_data",
            details={"policy_profile": profile}))
    if profile_rules.get("requires_executed_tests"):
        executed_test_gate = _executed_test_run_gate(merged_payload, session)
        if not executed_test_gate.get("ok"):
            findings.append(_merge_gate_finding(
                executed_test_gate.get("reason") or "missing_executed_test_run",
                "Merge gate requires a passing executed test run with output/log hash.",
                "missing_data",
                details={"executed_test_gate": executed_test_gate,
                         "policy_profile": profile}))

    # Shared "is this task backed by board process" check (ADR-0006) — the same
    # definition the SESSION-12 claim gate enforces at the CI chokepoint. merge_gate
    # layers its stricter work-session hygiene above; this guards the base case (a task
    # with no claim, Work Session, In-Review/Done state, or provenance must not merge).
    backing = pr_backed_by_process(task, project=project)
    if task.get("status") and not backing.get("backed"):
        findings.append(_merge_gate_finding(
            "task_not_backed",
            "Task has no board backing: no active claim, Work Session, or In-Review/Done state.",
            "missing_data",
            details={"backing": backing}))

    blocking = [f for f in findings if f.get("blocking")]
    ok = not blocking
    result = {
        "schema": MERGE_GATE_SCHEMA,
        "project": project,
        "task_id": task_id,
        "backed": bool(backing.get("backed")),
        "backing_signal": backing.get("signal"),
        "claim_id": claim_id or None,
        "agent_id": agent_id or None,
        "work_session_id": (session or {}).get("work_session_id") or work_session_id or None,
        "pr_url": pr_url or None,
        "pr_number": pr_number or None,
        "repo": repo,
        "repo_role": role_info,
        "target_branch": target_branch,
        "policy_profile": profile,
        "policy": profile_rules,
        "work_session_required": require_session,
        "ok": ok,
        "status": "passed" if ok else "blocked",
        "findings": findings,
        "required_status_contexts": required_contexts,
        "status_contexts": pr_contexts,
        "external_ci": external_ci,
        "github_pr_source": pr_source,
        "done_authority": "github_webhook_or_reconcile",
        "done_controlled_by_merge_provenance": True,
        "checked_at": now,
    }
    append_activity(
        "merge.gate",
        actor,
        {k: v for k, v in result.items() if k not in {"external_ci"}},
        task_id=task_id or None,
        project=project,
    )
    return result


def append_activity(kind: str, actor: str, payload: Optional[Dict[str, Any]] = None,
                    task_id: Optional[str] = None,
                    project: str = DEFAULT_PROJECT) -> int:
    with _conn(project) as c:
        cur = c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (task_id, actor, kind, json.dumps(payload or {}, sort_keys=True), time.time()))
        return cur.lastrowid




def _repo_preflight_finding(code: str, message: str, failure_class: str,
                            severity: str = "high", blocking: bool = True,
                            details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "code": code,
        "failure_class": failure_class,
        "severity": severity,
        "blocking": bool(blocking),
        "message": message,
        **(details or {}),
    }


def _repo_git(repo_path: str, args: List[str], timeout_seconds: int = 10) -> Dict[str, Any]:
    try:
        completed = subprocess.run(
            ["git", "-C", repo_path, *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": (completed.stdout or "").strip(),
            "stderr": (completed.stderr or "").strip(),
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": str(exc)}


def _repo_remote_slug(remote_url: str) -> str:
    text = (remote_url or "").strip()
    if not text:
        return ""
    match = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", text)
    if match:
        return match.group(1).removesuffix(".git")
    if GITHUB_REPO_RE.match(text):
        return text.removesuffix(".git")
    return ""


def _repo_parse_status(lines: List[str]) -> Tuple[List[str], List[str]]:
    dirty: List[str] = []
    untracked: List[str] = []
    for line in lines:
        if not line.strip():
            continue
        path = line[3:] if len(line) > 3 else line.strip()
        if line.startswith("?? "):
            untracked.append(path)
        else:
            dirty.append(path)
    return dirty, untracked


def _repo_git_dir(repo_path: str) -> str:
    git_dir = _repo_git(repo_path, ["rev-parse", "--git-dir"])
    if not git_dir.get("ok"):
        return ""
    raw = git_dir.get("stdout") or ""
    if os.path.isabs(raw):
        return raw
    return os.path.abspath(os.path.join(repo_path, raw))


def _repo_merge_state(git_dir: str) -> Dict[str, Any]:
    if not git_dir:
        return {"active": False, "states": []}
    checks = {
        "merge": "MERGE_HEAD",
        "rebase_merge": "rebase-merge",
        "rebase_apply": "rebase-apply",
        "cherry_pick": "CHERRY_PICK_HEAD",
        "revert": "REVERT_HEAD",
    }
    active = [name for name, rel in checks.items() if os.path.exists(os.path.join(git_dir, rel))]
    return {"active": bool(active), "states": active}


def _repo_list_candidate_files(repo_path: str, max_files: int) -> List[str]:
    listed = _repo_git(repo_path, ["ls-files", "-co", "--exclude-standard"], timeout_seconds=20)
    if not listed.get("ok"):
        return []
    return [line for line in (listed.get("stdout") or "").splitlines() if line.strip()][:max_files]


def _repo_scan_conflict_markers(repo_path: str, max_files: int = 4000,
                                max_file_bytes: int = 1024 * 1024) -> List[Dict[str, Any]]:
    markers: List[Dict[str, Any]] = []
    for rel in _repo_list_candidate_files(repo_path, max_files=max_files):
        full = os.path.abspath(os.path.join(repo_path, rel))
        if not full.startswith(os.path.abspath(repo_path) + os.sep):
            continue
        try:
            if not os.path.isfile(full) or os.path.getsize(full) > max_file_bytes:
                continue
            with open(full, "rb") as fh:
                raw = fh.read(max_file_bytes + 1)
            if b"\0" in raw:
                continue
            text = raw.decode("utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("<<<<<<<", ">>>>>>>")):
                markers.append({"path": rel, "line": lineno, "marker": stripped[:16]})
                break
    return markers


def _repo_worktree_collisions(path: str, agent_id: str,
                              project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    if not has_project(project):
        return []
    path_real = os.path.realpath(os.path.abspath(path))
    collisions: List[Dict[str, Any]] = []
    now = time.time()
    with _conn(project) as c:
        for lease in _active_resource_leases_in(c, now, "worktree"):
            if lease.get("agent_id") == agent_id:
                continue
            names = _json_obj(lease.get("names") or "[]", [])
            for name in names:
                if os.path.realpath(os.path.abspath(str(name))) == path_real:
                    collisions.append({
                        "lease_id": lease.get("id"),
                        "agent_id": lease.get("agent_id"),
                        "task_id": lease.get("task_id"),
                        "name": str(name),
                        "expires_at": lease.get("claimed_at", 0) + lease.get("ttl_seconds", 0),
                    })
    return collisions


def repo_preflight(worktree_path: str, project: str = DEFAULT_PROJECT,
                   task_id: str = "", agent_id: str = "",
                   repo_role: str = "canonical", expected_branch: str = "",
                   expected_base_ref: str = "", scan_conflicts: bool = True,
                   max_scan_files: int = 4000) -> Dict[str, Any]:
    """Inspect a local git worktree before agents edit, claim, complete, or merge.

    The report is side-effect-free and returns pass/warn/deny plus typed findings
    that adapters and hosts can enforce without inferring from prose.
    """
    now = time.time()
    path = os.path.abspath(os.path.expanduser(str(worktree_path or "").strip()))
    findings: List[Dict[str, Any]] = []
    topology = get_project_repo_topology(project) if has_project(project) else {}
    roles = topology.get("roles") or {}
    role = roles.get(repo_role) or {}
    default_branch = (role.get("default_branch") or "").strip()
    base_ref = (expected_base_ref or (f"origin/{default_branch}" if default_branch else "")).strip()
    report: Dict[str, Any] = {
        "schema": REPO_PREFLIGHT_SCHEMA,
        "project": project,
        "task_id": (task_id or "").strip().upper(),
        "agent_id": (agent_id or "").strip(),
        "repo_role": (repo_role or "").strip() or "canonical",
        "repo_path": path,
        "expected_branch": (expected_branch or "").strip(),
        "expected_base_ref": base_ref,
        "created_at": now,
        "verdict": "deny",
        "ok": False,
        "findings": findings,
    }
    if not has_project(project):
        findings.append(_repo_preflight_finding(
            "unknown_project", f"Unknown project: {project}", "wrong_repo"))
        return report
    if not os.path.isdir(path):
        findings.append(_repo_preflight_finding(
            "worktree_missing", f"Worktree path does not exist: {path}", "wrong_repo"))
        return report
    inside = _repo_git(path, ["rev-parse", "--is-inside-work-tree"])
    if not inside.get("ok") or inside.get("stdout") != "true":
        findings.append(_repo_preflight_finding(
            "not_git_worktree", "Path is not inside a git worktree.", "wrong_repo",
            details={"stderr": inside.get("stderr") or ""}))
        return report

    root = _repo_git(path, ["rev-parse", "--show-toplevel"])
    repo_path = os.path.abspath(root.get("stdout") or path)
    report["repo_path"] = repo_path
    git_dir = _repo_git_dir(repo_path)
    report["git_dir"] = git_dir

    remote = _repo_git(repo_path, ["remote", "get-url", "origin"])
    remote_url = remote.get("stdout") if remote.get("ok") else ""
    remote_slug = _repo_remote_slug(remote_url)
    expected_repo = (role.get("repo") or "").strip()
    expected_slug = _repo_remote_slug(expected_repo)
    report["remote"] = {"name": "origin", "url": remote_url, "repo": remote_slug}
    report["expected_repo"] = expected_repo
    if expected_slug and remote_slug and remote_slug.lower() != expected_slug.lower():
        findings.append(_repo_preflight_finding(
            "wrong_repo",
            f"origin repo {remote_slug} does not match project {project} {repo_role} repo {expected_slug}.",
            "wrong_repo",
            details={"actual_repo": remote_slug, "expected_repo": expected_slug}))

    branch = _repo_git(repo_path, ["branch", "--show-current"])
    current_branch = branch.get("stdout") if branch.get("ok") else ""
    head = _repo_git(repo_path, ["rev-parse", "HEAD"])
    report["branch"] = current_branch
    report["head_sha"] = head.get("stdout") if head.get("ok") else ""
    if not current_branch:
        findings.append(_repo_preflight_finding(
            "detached_head", "Worktree is in detached HEAD state.", "detached_head"))

    expected = (expected_branch or "").strip()
    if expected and current_branch != expected:
        findings.append(_repo_preflight_finding(
            "wrong_branch",
            f"Current branch {current_branch or '(detached)'} does not match expected branch {expected}.",
            "wrong_branch",
            details={"actual_branch": current_branch, "expected_branch": expected}))
    elif not expected and task_id and agent_id and current_branch and not _branch_matches_task(
            agent_id, task_id, current_branch):
        findings.append(_repo_preflight_finding(
            "wrong_branch",
            f"Current branch {current_branch} is not task-scoped for {task_id}.",
            "wrong_branch",
            details={"actual_branch": current_branch, "task_id": task_id}))

    upstream = _repo_git(repo_path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    upstream_ref = upstream.get("stdout") if upstream.get("ok") else ""
    report["upstream"] = upstream_ref
    if not upstream_ref:
        findings.append(_repo_preflight_finding(
            "missing_upstream", "Branch has no upstream tracking ref.", "missing_upstream",
            severity="medium", blocking=False, details={"stderr": upstream.get("stderr") or ""}))
    else:
        upstream_sha = _repo_git(repo_path, ["rev-parse", f"{upstream_ref}^{{commit}}"])
        report["upstream_sha"] = upstream_sha.get("stdout") if upstream_sha.get("ok") else ""
        counts = _repo_git(repo_path, ["rev-list", "--left-right", "--count", f"HEAD...{upstream_ref}"])
        if counts.get("ok"):
            try:
                ahead, behind = [int(x) for x in counts.get("stdout", "0 0").split()]
                report["upstream_distance"] = {"ahead": ahead, "behind": behind}
            except ValueError:
                findings.append(_repo_preflight_finding(
                    "upstream_distance_unavailable",
                    "Could not parse ahead/behind distance to upstream.",
                    "git_signal_unavailable", severity="medium", blocking=False))

    if base_ref:
        base_sha = _repo_git(repo_path, ["rev-parse", f"{base_ref}^{{commit}}"])
        if base_sha.get("ok"):
            report["base_ref"] = base_ref
            report["base_sha"] = base_sha.get("stdout")
            merge_base = _repo_git(repo_path, ["merge-base", "HEAD", base_ref])
            report["merge_base"] = merge_base.get("stdout") if merge_base.get("ok") else ""
            base_counts = _repo_git(repo_path, ["rev-list", "--left-right", "--count", f"HEAD...{base_ref}"])
            if base_counts.get("ok"):
                try:
                    ahead_base, behind_base = [int(x) for x in base_counts.get("stdout", "0 0").split()]
                    report["base_distance"] = {"ahead": ahead_base, "behind": behind_base}
                    if behind_base > 0:
                        findings.append(_repo_preflight_finding(
                            "stale_base",
                            f"Branch is {behind_base} commit(s) behind {base_ref}.",
                            "stale_base",
                            details={"base_ref": base_ref, "behind": behind_base}))
                except ValueError:
                    findings.append(_repo_preflight_finding(
                        "base_distance_unavailable",
                        "Could not parse ahead/behind distance to base ref.",
                        "git_signal_unavailable", severity="medium", blocking=False))
        else:
            findings.append(_repo_preflight_finding(
                "missing_base_ref",
                f"Base ref {base_ref!r} is not reachable in this checkout.",
                "missing_base_ref", severity="medium", blocking=False,
                details={"stderr": base_sha.get("stderr") or ""}))

    status = _repo_git(repo_path, ["status", "--porcelain=v1", "-uall"], timeout_seconds=20)
    status_lines = (status.get("stdout") or "").splitlines() if status.get("ok") else []
    dirty_files, untracked_files = _repo_parse_status(status_lines)
    report["git_status"] = {"porcelain": status_lines[:200], "count": len(status_lines)}
    report["dirty"] = bool(status_lines)
    report["dirty_files"] = dirty_files[:100]
    report["untracked_files"] = untracked_files[:100]
    if status_lines:
        findings.append(_repo_preflight_finding(
            "dirty_worktree",
            f"Worktree has {len(status_lines)} dirty or untracked file(s).",
            "dirty_worktree",
            details={"dirty_count": len(dirty_files), "untracked_count": len(untracked_files)}))

    merge_state = _repo_merge_state(git_dir)
    report["merge_state"] = merge_state
    if merge_state.get("active"):
        findings.append(_repo_preflight_finding(
            "merge_or_rebase_in_progress",
            "Worktree has an active merge/rebase/cherry-pick/revert state.",
            "merge_or_rebase_in_progress",
            details={"states": merge_state.get("states") or []}))

    conflict_markers = _repo_scan_conflict_markers(repo_path, max_files=max_scan_files) if scan_conflicts else []
    report["conflict_markers"] = conflict_markers[:100]
    report["conflict_marker_count"] = len(conflict_markers)
    if conflict_markers:
        findings.append(_repo_preflight_finding(
            "conflict_markers",
            f"Found conflict markers in {len(conflict_markers)} file(s).",
            "conflict_markers",
            details={"paths": [m.get("path") for m in conflict_markers[:20]]}))

    collisions = _repo_worktree_collisions(repo_path, report["agent_id"], project=project)
    report["resource_collisions"] = collisions
    if collisions:
        findings.append(_repo_preflight_finding(
            "shared_worktree_collision",
            "Worktree path is already leased by another active agent.",
            "shared_worktree_collision",
            details={"collisions": collisions}))

    blocking = [f for f in findings if f.get("blocking")]
    report["verdict"] = "deny" if blocking else ("warn" if findings else "pass")
    report["ok"] = report["verdict"] == "pass"
    return report


def _pre_tool_input(value: Any) -> Dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {"value": value}


def _pre_tool_classify(tool_name: str, tool_input: Dict[str, Any],
                       action: str = "") -> Dict[str, Any]:
    raw_action = (action or "").strip().lower()
    name = (tool_name or "").strip()
    lowered = name.lower()
    ti = tool_input or {}
    if raw_action:
        effect = raw_action
    elif name in {"Edit", "Write", "NotebookEdit"}:
        effect = "file_write"
    elif "complete_claim" in lowered or lowered.endswith("/complete_claim"):
        effect = "complete_claim"
    elif "pr create" in str(ti.get("command") or "").lower() or "gh pr create" in str(ti.get("command") or "").lower():
        effect = "pr_create"
    elif name == "Bash":
        cmd = str(ti.get("command") or "").lower()
        if re.search(r"\bgit\s+(merge|rebase|cherry-pick|commit|push|reset|checkout|switch)\b", cmd):
            effect = "git_command"
        elif re.search(r"\b(gh\s+pr\s+merge|gh\s+pr\s+create)\b", cmd):
            effect = "pr_or_merge"
        elif re.search(r"\b(systemctl|uvicorn|npm\s+run|python3?\s+.*app\.py|kill|pkill)\b", cmd):
            effect = "runtime_control"
        else:
            effect = "shell"
    elif lowered.endswith(("update_task", "claim_task", "claim_next")):
        effect = "board_write"
    else:
        effect = "unknown"
    side_effect = effect not in {"read", "noop", "unknown"}
    requires_work_session = effect in {
        "file_write", "git_command", "pr_create", "pr_or_merge", "complete_claim",
        "merge", "server_start", "server_kill", "runtime_control", "external_effect",
        "board_write",
    }
    return {
        "tool_name": name,
        "action": effect,
        "side_effect": side_effect,
        "requires_work_session": requires_work_session,
    }


def _pre_tool_target_path(tool_input: Dict[str, Any]) -> str:
    ti = tool_input or {}
    return str(ti.get("file_path") or ti.get("path") or ti.get("notebook_path") or "").strip()


def _pre_tool_relpath(path: str, session: Dict[str, Any]) -> str:
    path = (path or "").strip()
    if not path:
        return ""
    if not os.path.isabs(path):
        return path.replace(os.sep, "/")
    root = (session.get("worktree_path") or session.get("clone_path") or "").strip()
    if root:
        try:
            return os.path.relpath(path, root).replace(os.sep, "/")
        except ValueError:
            pass
    return os.path.basename(path)


def _pre_tool_decision(decision: str, reason: str, failure_class: str = "",
                       severity: str = "", remediation: Optional[List[str]] = None,
                       **extra: Any) -> Dict[str, Any]:
    return {
        "schema": PRE_TOOL_CHECK_SCHEMA,
        "decision": decision,
        "reason": reason,
        "failure_class": failure_class,
        "severity": severity,
        "remediation": remediation or [],
        **extra,
    }


def _pre_tool_requested_profile(payload: Dict[str, Any], classification: Dict[str, Any],
                                session: Optional[Dict[str, Any]] = None) -> str:
    requested = str(payload.get("session_policy_profile") or payload.get("policy_profile") or "").strip()
    if requested:
        return requested
    if session and session.get("policy_profile"):
        return str(session.get("policy_profile") or "")
    if classification.get("action") in {
        "git_command", "pr_create", "pr_or_merge", "complete_claim", "merge",
        "server_start", "server_kill", "runtime_control",
    }:
        return "code_strict"
    return ""


def _record_pre_tool_activity(task_id: str, actor: str, kind: str,
                              payload: Dict[str, Any],
                              project: str = DEFAULT_PROJECT) -> None:
    if not task_id:
        return
    append_activity(kind, actor, payload, task_id=task_id, project=project)


def pre_tool_check(payload: Dict[str, Any], actor: str = "system",
                   principal_id: str = "",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Validate a pending side-effectful tool call against Work Session state.

    This is the server-side contract adapters call before file writes, git/PR/merge
    actions, claim completion, and runner/server controls. It intentionally fails closed for
    risky effects when no active Work Session is bound, while read/noop checks remain allowed.
    """
    if not has_project(project):
        return _pre_tool_decision(
            "deny", f"unknown project: {project}", "invalid_input", "high",
            ["Call prepare_agent_session and pass the selected project explicitly."],
            project=project, ok=False)

    payload = dict(payload or {})
    tool_input = _pre_tool_input(payload.get("tool_input") or payload.get("input") or {})
    agent_id = str(payload.get("agent_id") or "").strip()
    task_id = str(payload.get("task_id") or payload.get("task") or "").strip().upper()
    work_session_id = str(payload.get("work_session_id") or "").strip()
    claim_id = str(payload.get("claim_id") or "").strip()
    control_mode = str(payload.get("control_mode") or payload.get("control_fidelity") or "").strip()
    classification = _pre_tool_classify(
        str(payload.get("tool_name") or payload.get("tool") or ""),
        tool_input,
        str(payload.get("action") or ""),
    )
    base = {
        "project": project,
        "task_id": task_id or None,
        "agent_id": agent_id or None,
        "work_session_id": work_session_id or None,
        "claim_id": claim_id or None,
        "classification": classification,
        "control_mode": control_mode or None,
    }
    if not classification["side_effect"] and not classification["requires_work_session"]:
        return _pre_tool_decision("allow", "", **base, ok=True)

    binding = resolve_write_actor(
        actor,
        project=project,
        task_id=task_id,
        agent_id=agent_id,
        principal_id=principal_id,
    )
    if not binding.get("ok"):
        event = {
            **base,
            "reason": binding.get("error") or "unbound_write",
            "failure_class": "unbound_identity",
            "principal_actor": binding.get("principal_actor") or actor,
            "principal_id": principal_id,
            "remediation": binding.get("remediation") or [],
        }
        _record_pre_tool_activity(task_id, "switchboard/identity",
                                  "principal.unbound_write", event, project=project)
        return _pre_tool_decision(
            "deny",
            binding.get("message") or "Tool side effect requires a bound active agent identity.",
            "unbound_identity",
            "high",
            binding.get("remediation") or [],
            **base,
            binding=binding,
            activity_kind="principal.unbound_write",
            ok=False,
        )

    actor_name = binding.get("actor") or actor
    if not task_id:
        event = {**base, "reason": "task_id_required", "failure_class": "missing_data"}
        _record_pre_tool_activity("", actor_name, "work_session.unsafe_session", event, project=project)
        return _pre_tool_decision(
            "deny",
            "Side-effectful tools must name task_id so the Work Session can be validated.",
            "missing_data",
            "high",
            ["Pass task_id and work_session_id from the active claim/session."],
            **base,
            activity_kind="work_session.unsafe_session",
            ok=False,
        )
    task = get_task(task_id, project=project)
    if not task:
        return _pre_tool_decision(
            "deny", "task_id does not exist in this project.", "invalid_input", "high",
            ["Refresh the board and use a task from the selected project."],
            **base, ok=False)
    profile = _task_work_session_profile(
        task,
        _pre_tool_requested_profile(payload, classification),
        project=project,
    )
    rules = _session_policy_profile_rules(profile, project=project)
    base["policy_profile"] = profile
    base["policy_action"] = rules.get("pre_tool_missing_session") if rules else None
    if not rules:
        verdict = _unknown_session_policy_profile(profile, project)
        return _pre_tool_decision(
            "deny",
            verdict.get("message") or "Unknown session policy profile.",
            verdict.get("failure_class") or "invalid_input",
            verdict.get("severity") or "high",
            ["Use one of the project's session_policy_profiles.known_profiles."],
            **base,
            known_profiles=verdict.get("known_profiles") or [],
            ok=False,
        )

    now = time.time()
    with _conn(project) as c:
        row = _active_work_session_row_in(
            c, work_session_id=work_session_id, task_id=task_id, agent_id=agent_id,
            now=now)
        if not row:
            action = str(rules.get("pre_tool_missing_session") or "deny").strip().lower()
            strict_missing = bool(rules.get("work_session_required")) or action == "deny"
            event = {
                **base,
                "reason": "work_session_required" if strict_missing else "work_session_missing_allowed_by_policy",
                "failure_class": "missing_data",
                "binding": write_binding_activity_payload(binding),
                "policy": rules,
            }
            _record_pre_tool_activity(task_id, actor_name,
                                      "work_session.unsafe_session" if strict_missing else
                                      "work_session.policy_warning",
                                      event, project=project)
            if not strict_missing:
                return _pre_tool_decision(
                    "warn" if action == "warn" else "allow",
                    f"Policy profile {profile} allows this side effect without a bound Work Session.",
                    "missing_data" if action == "warn" else "",
                    "medium" if action == "warn" else "",
                    [
                        "Bind a Work Session for stronger provenance when this touches code.",
                        "Use code_strict for repo/code changes.",
                    ] if action == "warn" else [],
                    **base,
                    binding=write_binding_activity_payload(binding),
                    activity_kind="work_session.policy_warning",
                    ok=True,
                )
            return _pre_tool_decision(
                "deny",
                f"Policy profile {profile} requires a valid active Work Session before this tool side effect.",
                "missing_data",
                "high",
                [
                    "Create or bind a Work Session for this task and repo role.",
                    "Run repo_preflight/preflight_work_session and retry from the task branch.",
                    "Advisory runtimes must surface this deny and mark reduced control fidelity.",
                ],
                **base,
                activity_kind="work_session.unsafe_session",
                ok=False,
            )
        session = _work_session_row(row)
        profile = _task_work_session_profile(
            task,
            _pre_tool_requested_profile(payload, classification, session),
            project=project,
        )
        rules = _session_policy_profile_rules(profile, project=project)
        if not rules:
            verdict = _unknown_session_policy_profile(profile, project)
            rules = {}
        else:
            verdict = _validate_work_session_claim_state(
                session, task, agent_id, project,
                required=bool(rules.get("work_session_required")),
                profile=profile,
                source="pre_tool_check", normalized_payload=None, now=now)
        base["policy_profile"] = profile
        base["policy_action"] = rules.get("pre_tool_missing_session") if rules else None
        base["work_session_id"] = session.get("work_session_id")
        if claim_id and session.get("claim_id") and claim_id != session.get("claim_id"):
            verdict = _work_session_failure(
                "wrong_claim",
                "Work Session claim_id does not match the pending tool claim.",
                "invalid_input",
                details={"problems": [{"reason": "wrong_claim",
                                        "failure_class": "invalid_input",
                                        "message": "claim_id mismatch"}],
                         "work_session_id": session.get("work_session_id"),
                         "policy_profile": profile},
            )
        if not verdict.get("ok"):
            event = {
                **base,
                "reason": verdict.get("reason") or "unsafe_session",
                "failure_class": verdict.get("failure_class") or "failed_gate",
                "problems": verdict.get("problems") or [],
                "binding": write_binding_activity_payload(binding),
            }
            _record_pre_tool_activity(task_id, actor_name, "work_session.unsafe_session",
                                      event, project=project)
            return _pre_tool_decision(
                "deny",
                verdict.get("message") or "Work Session is unsafe for this tool side effect.",
                verdict.get("failure_class") or "failed_gate",
                verdict.get("severity") or "high",
                [
                    "Repair the Work Session hygiene failure.",
                    "Run preflight_work_session before retrying.",
                    "Do not proceed through a hidden fallback.",
                ],
                **base,
                problems=verdict.get("problems") or [],
                activity_kind="work_session.unsafe_session",
                ok=False,
            )

    target_path = _pre_tool_target_path(tool_input)
    if classification["action"] == "file_write" and target_path:
        relpath = _pre_tool_relpath(target_path, session)
        held = check_resources("file", [relpath], project=project)
        conflicts = [h for h in held if h.get("name") == relpath and
                     h.get("held_by") and h.get("held_by") != agent_id]
        if conflicts:
            event = {
                **base,
                "target_path": relpath,
                "reason": "file_lease_conflict",
                "failure_class": "failed_gate",
                "conflicts": conflicts,
            }
            _record_pre_tool_activity(task_id, actor_name, "work_session.unsafe_session",
                                      event, project=project)
            return _pre_tool_decision(
                "deny",
                f"'{relpath}' is leased by another active agent.",
                "failed_gate",
                "high",
                ["Coordinate through Switchboard or wait for the lease to release."],
                **base,
                target_path=relpath,
                conflicts=conflicts,
                activity_kind="work_session.unsafe_session",
                ok=False,
            )

    return _pre_tool_decision(
        "allow",
        "Work Session validated for this tool side effect.",
        **base,
        binding=write_binding_activity_payload(binding),
        ok=True,
    )




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






# --- NARRATE-2: CEO-voice task narration (docs/CEO-NARRATOR-CONTRACT.md) ---

def _max_activity_cursor(task: Dict[str, Any]) -> int:
    return max((a.get("id", 0) for a in (task.get("activity") or [])), default=0)


def task_narration_fingerprint(task: Dict[str, Any]) -> str:
    """Stable stamp of the source state a narration was written from. Recomputed on read;
    a mismatch means the narration is stale (see _narration_state). Shared by the narrator
    (write) and get_task (read) so both sides agree."""
    prov = task.get("provenance") or {}
    parts = [
        str(task.get("status") or ""),
        str(prov.get("type") or ""),
        str(_max_activity_cursor(task)),
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def _narration_state(stored: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
    """Flag a narration stale when current task state has moved past the fingerprint it was
    written from. Discipline carried over from BUG-13/BUG-17/HARDEN-30: derived prose is never
    shown as current truth once it contradicts the fingerprint."""
    current_fp = task_narration_fingerprint(task)
    stored_fp = stored.get("source_fingerprint")
    stale = bool(stored_fp) and stored_fp != current_fp
    state = {
        "stale": stale,
        "source_fingerprint": current_fp,
        "stored_fingerprint": stored_fp,
        "message": (
            "CEO narration is regenerating; trust status, provenance, and progress."
        ) if stale else None,
    }
    if stale:
        state["failure_class"] = "missing_data"
        state["expected_signal"] = "Narration should be regenerated from current task state."
    return state


def set_task_narration(task_id: str, narration: str, activity_cursor: int,
                       source_fingerprint: str = "", model: str = "",
                       project: str = DEFAULT_PROJECT) -> None:
    """Upsert the CEO-voice narration for a task (separate store from task_summaries)."""
    with _conn(project) as c:
        c.execute(
            "INSERT OR REPLACE INTO task_narrations"
            "(task_id, narration, generated_at, activity_cursor, source_fingerprint, model) "
            "VALUES (?,?,?,?,?,?)",
            (task_id, narration, time.time(), activity_cursor, source_fingerprint, model),
        )


def get_task_narration(task_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        r = c.execute("SELECT * FROM task_narrations WHERE task_id=?", (task_id,)).fetchone()
        return dict(r) if r else None


def enqueue_narration(task_id: str, status: str = "", reason: str = "",
                      project: str = DEFAULT_PROJECT) -> None:
    """Mark a task for (re)narration after a meaningful transition. Idempotent per task —
    a burst of transitions collapses into one pending row. Called post-commit from the write
    path; never triggers a synchronous LLM call."""
    with _conn(project) as c:
        c.execute(
            "INSERT OR REPLACE INTO pending_narrations(task_id, status, reason, enqueued_at) "
            "VALUES (?,?,?,?)",
            (task_id, status or "", reason or "", time.time()),
        )


def list_pending_narrations(project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    with _conn(project) as c:
        rows = c.execute(
            "SELECT task_id, status, reason, enqueued_at FROM pending_narrations "
            "ORDER BY enqueued_at"
        ).fetchall()
        return [dict(r) for r in rows]


def clear_pending_narration(task_id: str, project: str = DEFAULT_PROJECT) -> None:
    with _conn(project) as c:
        c.execute("DELETE FROM pending_narrations WHERE task_id=?", (task_id,))




def _cleanup_age_seconds(now: float, timestamp: Optional[float]) -> Optional[float]:
    if timestamp in (None, ""):
        return None
    try:
        return max(0.0, now - float(timestamp))
    except (TypeError, ValueError):
        return None


def _cleanup_candidate(kind: str, target_id: str, action: str, reason: str,
                       now: float, task_id: str = "",
                       timestamp: Optional[float] = None,
                       severity: str = "low",
                       snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "id": f"{kind}:{target_id}",
        "kind": kind,
        "target_id": target_id,
        "task_id": task_id or None,
        "action": action,
        "reason": reason,
        "severity": severity,
        "age_seconds": _cleanup_age_seconds(now, timestamp),
        "safe_to_apply": True,
        "snapshot": snapshot or {},
    }


def _cleanup_summary(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_kind: Dict[str, int] = {}
    by_action: Dict[str, int] = {}
    for item in candidates:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
        by_action[item["action"]] = by_action.get(item["action"], 0) + 1
    return {"total": len(candidates), "by_kind": by_kind, "by_action": by_action}


def cleanup_candidates(project: str = DEFAULT_PROJECT,
                       now: Optional[float] = None,
                       proof_task_age_days: float = 14,
                       include_kinds: Optional[List[str]] = None) -> Dict[str, Any]:
    """Return read-only lifecycle cleanup candidates.

    Candidates are intentionally conservative: only expired/stale rows or old terminal
    proof/sentinel tasks with no active claims/leases are returned. Applying a cleanup
    writes `cleanup.*` activity before changing live rows.
    """
    if not has_project(project):
        return {"error": f"unknown project: {project}", "project": project}
    now = time.time() if now is None else float(now)
    wanted = {k.strip() for k in (include_kinds or []) if k.strip()}
    min_proof_age = max(0.0, float(proof_task_age_days or 0)) * 86400.0
    out: List[Dict[str, Any]] = []

    def accept(kind: str) -> bool:
        return not wanted or kind in wanted

    with _conn(project) as c:
        task_ids = {r["task_id"] for r in c.execute("SELECT task_id FROM tasks").fetchall()}

        if accept("agent_presence"):
            for row in c.execute("SELECT * FROM agent_presence ORDER BY heartbeat_at").fetchall():
                presence = _presence_row(row, now=now)
                if not presence.get("stale"):
                    continue
                out.append(_cleanup_candidate(
                    "agent_presence", presence["agent_id"], "remove_stale_presence",
                    "agent heartbeat expired", now,
                    task_id=presence.get("task_id") or "",
                    timestamp=presence.get("expires_at"),
                    snapshot=presence,
                ))

        if accept("runner_session"):
            for row in c.execute("SELECT * FROM runner_sessions ORDER BY heartbeat_at").fetchall():
                session = _runner_session_row(row, now=now, include_claim=True, c=c)
                if not session.get("stale"):
                    continue
                status = str(session.get("status") or "").lower()
                if status in TERMINAL_RUNNER_STATUSES:
                    continue
                out.append(_cleanup_candidate(
                    "runner_session", session["runner_session_id"], "expire_runner_session",
                    "runner heartbeat expired", now,
                    task_id=session.get("task_id") or "",
                    timestamp=session.get("expires_at"),
                    snapshot=session,
                ))

        if accept("task_claim"):
            rows = c.execute(
                "SELECT * FROM task_claims WHERE status='active' "
                "AND (expires_at<=? OR task_id NOT IN (SELECT task_id FROM tasks)) "
                "ORDER BY expires_at, id",
                (now,),
            ).fetchall()
            for row in rows:
                claim = dict(row)
                orphaned = claim["task_id"] not in task_ids
                reason = "claim task is missing" if orphaned else "claim lease expired"
                out.append(_cleanup_candidate(
                    "task_claim", claim["id"], "abandon_expired_claim", reason, now,
                    task_id=claim.get("task_id") or "",
                    timestamp=claim.get("expires_at"),
                    severity="medium",
                    snapshot=claim,
                ))

        if accept("file_lease"):
            for row in c.execute("SELECT * FROM file_leases WHERE released_at IS NULL "
                                 "ORDER BY claimed_at").fetchall():
                lease = dict(row)
                expires_at = float(lease.get("claimed_at") or 0) + int(lease.get("ttl_minutes") or 0) * 60
                if expires_at > now:
                    continue
                lease["expires_at"] = expires_at
                out.append(_cleanup_candidate(
                    "file_lease", str(lease["id"]), "release_expired_lease",
                    "file lease expired", now,
                    task_id=lease.get("task_id") or "",
                    timestamp=expires_at,
                    severity="medium",
                    snapshot=lease,
                ))

        if accept("resource_lease"):
            for row in c.execute("SELECT * FROM resource_leases WHERE released_at IS NULL "
                                 "ORDER BY claimed_at").fetchall():
                lease = dict(row)
                expires_at = float(lease.get("claimed_at") or 0) + int(lease.get("ttl_seconds") or 0)
                if expires_at > now:
                    continue
                lease["expires_at"] = expires_at
                out.append(_cleanup_candidate(
                    "resource_lease", lease["id"], "release_expired_lease",
                    f"{lease.get('resource_type') or 'resource'} lease expired", now,
                    task_id=lease.get("task_id") or "",
                    timestamp=expires_at,
                    severity="medium",
                    snapshot=lease,
                ))

        if accept("wake_intent"):
            for row in c.execute("SELECT * FROM wake_intents ORDER BY requested_at").fetchall():
                wake = _wake_row(row)
                status = wake.get("status")
                if status in TERMINAL_WAKE_STATUSES:
                    continue
                deadline = wake.get("deadline")
                old_without_deadline = (
                    deadline is None and
                    _cleanup_age_seconds(now, wake.get("requested_at") or 0) is not None and
                    _cleanup_age_seconds(now, wake.get("requested_at") or 0) >= 86400
                )
                if deadline is None and not old_without_deadline:
                    continue
                if deadline is not None and float(deadline) > now:
                    continue
                out.append(_cleanup_candidate(
                    "wake_intent", wake["wake_id"], "cancel_old_wake",
                    "wake intent deadline expired" if deadline else "wake intent is older than 24h",
                    now,
                    task_id=wake.get("task_id") or "",
                    timestamp=deadline or wake.get("requested_at"),
                    snapshot=wake,
                ))

        if accept("monitor"):
            for row in c.execute("SELECT * FROM coordination_monitors ORDER BY created_at").fetchall():
                mon = _monitor_row(row) or {}
                action = ""
                reason = ""
                if mon.get("status") == "fired":
                    action = "resolve_fired_monitor"
                    reason = "monitor already fired and needs operator resolution"
                elif mon.get("status") == "pending" and mon.get("target_type") == "agent_message":
                    msg = c.execute("SELECT 1 FROM agent_messages WHERE id=?",
                                    (int(mon.get("target_id") or 0),)).fetchone()
                    if not msg:
                        action = "cancel_orphan_monitor"
                        reason = "monitor target message is missing"
                if not action:
                    continue
                out.append(_cleanup_candidate(
                    "monitor", mon["id"], action, reason, now,
                    task_id=mon.get("task_id") or "",
                    timestamp=mon.get("fired_at") or mon.get("deadline") or mon.get("created_at"),
                    snapshot=mon,
                ))

        if accept("proof_task"):
            rows = c.execute(
                "SELECT * FROM tasks WHERE status IN ('Done','Cancelled','Canceled') "
                "ORDER BY updated_at, task_id"
            ).fetchall()
            for row in rows:
                task = _task_row(row)
                if not _is_cleanup_proof_task(task):
                    continue
                age = _cleanup_age_seconds(now, task.get("updated_at"))
                if age is None or age < min_proof_age:
                    continue
                active = _active_task_state_in(c, task["task_id"], now)
                if active["claims"] or active["resource_leases"] or active["file_leases"]:
                    continue
                out.append(_cleanup_candidate(
                    "proof_task", task["task_id"], "archive_terminal_proof_task",
                    "old terminal proof/sentinel task", now,
                    task_id=task["task_id"],
                    timestamp=task.get("updated_at"),
                    snapshot=task,
                ))

    return {"project": project, "generated_at": now, "candidates": out,
            "summary": _cleanup_summary(out)}


def _cleanup_candidate_ids(candidates: List[Dict[str, Any]]) -> set:
    return {c["id"] for c in candidates}


def apply_cleanup(project: str = DEFAULT_PROJECT,
                  candidate_ids: Optional[List[str]] = None,
                  dry_run: bool = True,
                  actor: str = "switchboard/operator",
                  reason: str = "",
                  now: Optional[float] = None,
                  proof_task_age_days: float = 14,
                  include_kinds: Optional[List[str]] = None) -> Dict[str, Any]:
    """Apply selected lifecycle cleanups, or return the dry-run plan.

    The function recomputes candidates inside the request and only applies current candidate ids.
    Every mutation writes a `cleanup.*` activity row with the candidate snapshot.
    """
    now = time.time() if now is None else float(now)
    reason = (reason or "lifecycle cleanup").strip()
    plan = cleanup_candidates(project=project, now=now,
                              proof_task_age_days=proof_task_age_days,
                              include_kinds=include_kinds)
    if plan.get("error"):
        return plan
    candidates = plan["candidates"]
    requested = {cid.strip() for cid in (candidate_ids or []) if cid.strip()}
    if requested:
        candidates = [c for c in candidates if c["id"] in requested]
    if dry_run:
        return {"project": project, "dry_run": True, "generated_at": now,
                "candidates": candidates, "summary": _cleanup_summary(candidates)}

    results: List[Dict[str, Any]] = []
    available = _cleanup_candidate_ids(candidates)
    missing = sorted(requested - available) if requested else []

    with _conn(project) as c:
        for candidate in candidates:
            kind = candidate["kind"]
            target_id = candidate["target_id"]
            payload = {"candidate": candidate, "reason": reason}
            try:
                if kind == "agent_presence":
                    c.execute("DELETE FROM agent_presence WHERE agent_id=?", (target_id,))
                    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                              "VALUES (?,?,?,?,?)",
                              (candidate.get("task_id"), actor,
                               "cleanup.agent_presence_resolved",
                               json.dumps(payload, sort_keys=True), now))
                    results.append({"id": candidate["id"], "applied": True,
                                    "action": candidate["action"]})
                elif kind == "runner_session":
                    c.execute("UPDATE runner_sessions SET status='expired', updated_at=? "
                              "WHERE runner_session_id=?", (now, target_id))
                    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                              "VALUES (?,?,?,?,?)",
                              (candidate.get("task_id"), actor,
                               "cleanup.runner_session_expired",
                               json.dumps(payload, sort_keys=True), now))
                    results.append({"id": candidate["id"], "applied": True,
                                    "action": candidate["action"]})
                elif kind == "task_claim":
                    claim = candidate.get("snapshot") or {}
                    c.execute("UPDATE task_claims SET status='abandoned', completed_at=?, "
                              "abandon_reason=? WHERE id=? AND status='active'",
                              (now, f"cleanup: {reason}", target_id))
                    c.execute("UPDATE resource_leases SET released_at=? WHERE resource_type='task' "
                              "AND task_id=? AND agent_id=? AND released_at IS NULL",
                              (now, claim.get("task_id"), claim.get("agent_id")))
                    c.execute("UPDATE tasks SET status='Not Started', "
                              "assignee=CASE WHEN assignee=? THEN NULL ELSE assignee END, "
                              "updated_at=? WHERE task_id=? AND status='In Progress'",
                              (claim.get("agent_id"), now, claim.get("task_id")))
                    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                              "VALUES (?,?,?,?,?)",
                              (claim.get("task_id"), actor,
                               "cleanup.task_claim_abandoned",
                               json.dumps(payload, sort_keys=True), now))
                    results.append({"id": candidate["id"], "applied": True,
                                    "action": candidate["action"]})
                elif kind == "file_lease":
                    c.execute("UPDATE file_leases SET released_at=? WHERE id=?",
                              (now, target_id))
                    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                              "VALUES (?,?,?,?,?)",
                              (candidate.get("task_id"), actor,
                               "cleanup.lease_released",
                               json.dumps(payload, sort_keys=True), now))
                    results.append({"id": candidate["id"], "applied": True,
                                    "action": candidate["action"]})
                elif kind == "resource_lease":
                    c.execute("UPDATE resource_leases SET released_at=? WHERE id=?",
                              (now, target_id))
                    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                              "VALUES (?,?,?,?,?)",
                              (candidate.get("task_id"), actor,
                               "cleanup.lease_released",
                               json.dumps(payload, sort_keys=True), now))
                    results.append({"id": candidate["id"], "applied": True,
                                    "action": candidate["action"]})
                elif kind == "wake_intent":
                    wake = candidate.get("snapshot") or {}
                    result = dict(wake.get("result") or {})
                    result.update({"reason": reason, "cancelled_by": actor,
                                   "cleanup_candidate_id": candidate["id"]})
                    c.execute("UPDATE wake_intents SET status='cancelled', completed_at=?, "
                              "result_json=? WHERE wake_id=?",
                              (now, json.dumps(result, sort_keys=True), target_id))
                    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                              "VALUES (?,?,?,?,?)",
                              (candidate.get("task_id"), actor, "cleanup.wake_cancelled",
                               json.dumps(payload, sort_keys=True), now))
                    results.append({"id": candidate["id"], "applied": True,
                                    "action": candidate["action"]})
                elif kind == "monitor":
                    mon = candidate.get("snapshot") or {}
                    status = "resolved" if candidate["action"] == "resolve_fired_monitor" else "cancelled"
                    result = dict(mon.get("result") or {})
                    result.update({"reason": reason, "resolved_by": actor,
                                   "cleanup_candidate_id": candidate["id"]})
                    c.execute("UPDATE coordination_monitors SET status=?, resolved_at=?, "
                              "updated_at=?, last_checked_at=?, result_json=? WHERE id=?",
                              (status, now, now, now, json.dumps(result, sort_keys=True),
                               target_id))
                    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                              "VALUES (?,?,?,?,?)",
                              (candidate.get("task_id"), actor,
                               "cleanup.monitor_resolved" if status == "resolved"
                               else "cleanup.monitor_cancelled",
                               json.dumps(payload, sort_keys=True), now))
                    results.append({"id": candidate["id"], "applied": True,
                                    "action": candidate["action"]})
                elif kind == "proof_task":
                    snapshot = _task_snapshot_in(c, target_id)
                    if not snapshot:
                        results.append({"id": candidate["id"], "applied": False,
                                        "error": "task not found"})
                        continue
                    active = _active_task_state_in(c, target_id, now)
                    if active["claims"] or active["resource_leases"] or active["file_leases"]:
                        results.append({"id": candidate["id"], "applied": False,
                                        "error": "task has active claims or leases",
                                        "active": active})
                        continue
                    archive_id = _insert_archive_in(c, target_id, "cleanup_archive",
                                                    actor, reason, project, "",
                                                    snapshot, now)
                    _delete_task_related_in(c, target_id, snapshot)
                    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                              "VALUES (?,?,?,?,?)",
                              (target_id, actor, "cleanup.task_archived",
                               json.dumps(payload | {"archive_id": archive_id},
                                          sort_keys=True), now))
                    results.append({"id": candidate["id"], "applied": True,
                                    "action": candidate["action"],
                                    "archive_id": archive_id})
            except Exception as exc:
                results.append({"id": candidate["id"], "applied": False,
                                "error": type(exc).__name__, "message": str(exc)})

    applied = [r for r in results if r.get("applied")]
    return {"project": project, "dry_run": False, "generated_at": now,
            "requested_ids": sorted(requested), "missing_ids": missing,
            "results": results, "applied_count": len(applied),
            "summary": _cleanup_summary(candidates)}


def get_meta(key: str, default=None, project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default


def set_meta(key: str, value, project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, json.dumps(value)))


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


# ---- dev dispatches (Claude Code runner) — so the UI can show the latest run per task ----
def add_dispatch(task_id: str, job_id: str):
    with _conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS dispatches (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "task_id TEXT, job_id TEXT, created_at REAL)")
        c.execute("INSERT INTO dispatches(task_id, job_id, created_at) VALUES (?,?,?)",
                  (task_id, job_id, time.time()))


def latest_dispatch(task_id: str) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        try:
            r = c.execute("SELECT job_id, created_at FROM dispatches WHERE task_id=? ORDER BY id DESC LIMIT 1",
                          (task_id,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return {"job_id": r["job_id"], "created_at": r["created_at"]} if r else None


# ---- contacts (email -> display name) for inbound-reply routing ----------
# Seeded with the known TEEP participants so the email agent can resolve "Sahir",
# "Darko", "Steve" -> the right address; auto-learned from every inbound From/To/Cc.
_SEED_CONTACTS = {
    "steve@taikunai.com": "Steve Ridder",
    "sahir.shah@totalenergies.com": "Sahir Shah",
    "darko.jankovic@totalenergies.com": "Darko Jankovic",
}


def get_contacts() -> Dict[str, str]:
    c = get_meta("contacts")
    if not c:
        c = dict(_SEED_CONTACTS)
        set_meta("contacts", c)
    return c


def upsert_contact(email: str, name: Optional[str] = None):
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return
    c = get_contacts()
    name = (name or "").strip()
    if email not in c or (name and not c.get(email)):
        c[email] = name or c.get(email) or email
        set_meta("contacts", c)


# ---- plan-wide chat (the global "Ask Taikun" session) --------------------
def add_chat(session: str, role: str, content: str, payload: Optional[Dict[str, Any]] = None,
             project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        c.execute("INSERT INTO chat(session, role, content, payload, created_at) VALUES (?,?,?,?,?)",
                  (session, role, content, json.dumps(payload or {}), time.time()))


def clear_chat(session: str, project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        c.execute("DELETE FROM chat WHERE session=?", (session,))


def recent_chat(session: str, limit: int = 20, project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    with _conn(project) as c:
        rows = c.execute(
            "SELECT role, content, payload, created_at FROM chat WHERE session=? ORDER BY id DESC LIMIT ?",
            (session, limit)).fetchall()
    out = [{"role": r["role"], "content": r["content"],
            "payload": json.loads(r["payload"] or "{}"), "created_at": r["created_at"]} for r in rows]
    out.reverse()
    return out


# ---- activity deltas + digests (Phase 3.5) -------------------------------
def activity_since(ts: float) -> List[Dict[str, Any]]:
    """Every activity event across all tasks since `ts` — the delta substrate."""
    with _conn() as c:
        rows = c.execute(
            "SELECT task_id, actor, kind, payload, created_at FROM activity WHERE created_at > ? ORDER BY created_at",
            (ts,)).fetchall()
    return [{"task_id": r["task_id"], "actor": r["actor"], "kind": r["kind"],
             "payload": json.loads(r["payload"] or "{}"), "created_at": r["created_at"]} for r in rows]


# ---- incremental RAG corpus (Phase 5) — ingested artifacts, persisted + shared --------
# ---- Live Inbox queue (Phase 5.5) — triaged inbound artifacts awaiting review ----------
# Hot-read cache (lite board, plan signals, mission status/dependency-graph) extracted to
# read_cache.py per ADR-0006 — it's a self-contained leaf (only runs a builder callback,
# no store dependency). Serve-stale-while-revalidate + the stamp/TTL invalidation contract
# live there. Re-exported so store.ttl_read_cache / store._READ_CACHE keep working for the
# callers below (and signals.py, the perf tests).
from read_cache import _READ_CACHE, ttl_read_cache  # noqa: E402,F401

