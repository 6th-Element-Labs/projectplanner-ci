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


def init_db(project: str = DEFAULT_PROJECT):
    if project_lifecycle_status(project) == "archived":
        return False
    with _conn(project) as c:
        apply_schema(c)
    return True


def seed_if_empty(project: str = DEFAULT_PROJECT):
    if project_lifecycle_status(project) == "archived":
        return False
    with _conn(project) as c:
        return seed_from_plan(c, _resolve(project)["seed"])


# Core tables that apply_schema() creates and every request path assumes exist.
# A board db missing any of these is not safely serveable, so readiness fails closed.
READINESS_REQUIRED_TABLES = ("tasks", "activity", "meta")


def probe_project_db(project: str) -> Optional[str]:
    """Cheap liveness+schema check for one board db. Returns None when the db is
    accessible and carries the required schema, else a SHORT reason string that
    NEVER embeds task/project data (safe to surface on an unauthenticated probe)."""
    try:
        with _conn(project) as c:
            present = {
                r["name"]
                for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
    except Exception as e:  # locked / missing / corrupt db, permission error, etc.
        return type(e).__name__
    missing = [t for t in READINESS_REQUIRED_TABLES if t not in present]
    if missing:
        return "missing_tables:" + ",".join(missing)
    return None


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


EXTERNAL_EFFECT_TERMINAL_STATUSES = {"verified", "failed", "dead_letter", "void"}
EXTERNAL_CI_STATUSES = {
    "requested", "mirrored", "triggered", "running", "success", "failure", "cancelled", "error"
}
EXTERNAL_CI_TERMINAL_STATUSES = {"success", "failure", "cancelled", "error"}
EXTERNAL_CI_FAILURE_CLASSES = {
    "mirror_sync_failed": "stale_branch",
    "workflow_trigger_failed": "broken_connection",
    "workflow_poll_failed": "broken_connection",
    "workflow_failed": "failed_gate",
}
GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
WORKFLOW_REF_RE = re.compile(r"^[A-Za-z0-9_.@:/-]+$")


def _effect_window_key(now: float, idempotency_window_seconds: int = 0) -> str:
    window = int(idempotency_window_seconds or 0)
    return f"window:{window}:{int(now // window)}" if window > 0 else "permanent"


def default_external_ci_mirror_branch(task_id: str, source_sha: str) -> str:
    task = re.sub(r"[^A-Za-z0-9_.-]+", "-", (task_id or "task").strip()).strip("-") or "task"
    sha = (source_sha or "").strip()[:12] or "unknown"
    return f"ci/{task}/{sha}"


def _external_ci_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    d = dict(row)
    d["artifacts"] = _json_obj(d.pop("artifacts_json", "[]"), [])
    d["request"] = _json_obj(d.pop("request_json", "{}"), {})
    d["result"] = _json_obj(d.pop("result_json", "{}"), {})
    d["ci_repo"] = d.get("mirror_repo")
    d["status_context"] = (
        d.get("status_context")
        or (d.get("request") or {}).get("status_context")
        or (d.get("result") or {}).get("status_context")
        or None
    )
    d["required_status_contexts"] = (
        (d.get("request") or {}).get("required_status_contexts")
        or ([d["status_context"]] if d.get("status_context") else [])
    )
    d["repo_role"] = "public_ci"
    d["evidence_only"] = True
    return d


def _validate_external_ci_status(status: str) -> str:
    clean = (status or "requested").strip().lower()
    return clean if clean in EXTERNAL_CI_STATUSES else ""


def _validate_external_ci_failure_class(value: str) -> str:
    clean = (value or "").strip().lower()
    return clean if not clean or clean in EXTERNAL_CI_FAILURE_CLASSES else ""


def _external_ci_topology_contract(source_project: str,
                                   data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Resolve the canonical source repo and public CI role for external proof."""
    data = data or {}
    topology = get_project_repo_topology(source_project)
    roles = topology.get("roles") or {}
    canonical = roles.get("canonical") or {}
    public_ci = roles.get("public_ci") or {}
    source_repo = (canonical.get("repo") or "").strip()
    ci_repo = (public_ci.get("repo") or "").strip()
    required_contexts = _coerce_str_list(public_ci.get("required_status_contexts"))
    requested_context = (
        data.get("status_context")
        or data.get("required_status_context")
        or data.get("required_status_contexts")
        or ""
    )
    if isinstance(requested_context, (list, tuple)):
        requested_context = requested_context[0] if requested_context else ""
    status_context = str(requested_context or "").strip()
    if not status_context and required_contexts:
        status_context = required_contexts[0]
    return {
        "schema": "switchboard.external_ci_topology_contract.v1",
        "source_project": source_project,
        "source_repo": source_repo,
        "ci_repo": ci_repo,
        "status_context": status_context or None,
        "required_status_contexts": required_contexts,
        "repo_topology_schema": topology.get("schema"),
        "repo_topology_valid": topology.get("valid"),
        "code_repo_gate": topology.get("code_repo_gate"),
        "public_ci_role": public_ci,
        "canonical_role": canonical,
        "evidence_only": True,
        "authority": "verification_only",
    }


def _repo_mismatch(got: str, expected: str) -> bool:
    return bool(got and expected and _normalize_repo_slug(got) != _normalize_repo_slug(expected))


def _external_ci_request_payload(data: Dict[str, Any], project: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source_project = (data.get("source_project") or project or DEFAULT_PROJECT).strip()
    if not has_project(source_project):
        return {}, {"error": f"unknown source project: {source_project}"}
    task_id = (data.get("task_id") or "").strip().upper()
    if task_id and not get_task(task_id, project=project):
        return {}, {"error": "unknown task", "task_id": task_id, "project": project}
    contract = _external_ci_topology_contract(source_project, data)
    if not (contract.get("code_repo_gate") or {}).get("passed"):
        return {}, {"error": "canonical source repo is not configured",
                    "source_project": source_project,
                    "code_repo_gate": contract.get("code_repo_gate")}
    source_repo, source_repo_error = _validate_github_repo(
        data.get("source_repo") or contract.get("source_repo") or get_project_github_repo(source_project))
    if source_repo_error:
        return {}, {"error": source_repo_error, "repo": source_repo, "field": "source_repo"}
    if not source_repo:
        return {}, {"error": "source_repo required", "source_project": source_project}
    if _repo_mismatch(source_repo, contract.get("source_repo") or ""):
        return {}, {"error": "source_repo must match repo_topology.roles.canonical.repo",
                    "repo": source_repo, "expected": contract.get("source_repo"),
                    "field": "source_repo", "source_project": source_project}
    mirror_repo, mirror_repo_error = _validate_github_repo(
        data.get("mirror_repo") or data.get("ci_repo") or contract.get("ci_repo") or "")
    if mirror_repo_error:
        return {}, {"error": mirror_repo_error, "repo": mirror_repo, "field": "mirror_repo"}
    if not mirror_repo:
        return {}, {"error": "mirror_repo required",
                    "hint": "configure repo_topology.roles.public_ci.repo or pass mirror_repo"}
    if _repo_mismatch(mirror_repo, contract.get("ci_repo") or ""):
        return {}, {"error": "mirror_repo must match repo_topology.roles.public_ci.repo",
                    "repo": mirror_repo, "expected": contract.get("ci_repo"),
                    "field": "mirror_repo", "source_project": source_project}
    source_sha = (data.get("source_sha") or "").strip()
    if not GIT_SHA_RE.match(source_sha):
        return {}, {"error": "source_sha must be a 7-64 character hex Git SHA"}
    workflow = (data.get("workflow") or "").strip()
    if not workflow:
        return {}, {"error": "workflow required"}
    if not WORKFLOW_REF_RE.match(workflow):
        return {}, {"error": "workflow contains unsupported characters"}
    mirror_branch = (data.get("mirror_branch") or
                     default_external_ci_mirror_branch(task_id, source_sha)).strip()
    if not mirror_branch.startswith("ci/"):
        return {}, {"error": "mirror_branch must be under ci/"}
    status = _validate_external_ci_status(data.get("status") or "requested")
    if not status:
        return {}, {"error": "invalid external CI status",
                    "allowed": sorted(EXTERNAL_CI_STATUSES)}
    failure_class = _validate_external_ci_failure_class(data.get("failure_class") or "")
    if (data.get("failure_class") or "") and not failure_class:
        return {}, {"error": "invalid external CI failure_class",
                    "allowed": sorted(EXTERNAL_CI_FAILURE_CLASSES)}
    return {
        "source_project": source_project,
        "source_repo": source_repo,
        "source_branch": (data.get("source_branch") or "").strip() or None,
        "source_sha": source_sha.lower(),
        "mirror_repo": mirror_repo,
        "mirror_branch": mirror_branch,
        "workflow": workflow,
        "status_context": contract.get("status_context"),
        "required_status_contexts": contract.get("required_status_contexts") or [],
        "status": status,
        "conclusion": (data.get("conclusion") or "").strip() or None,
        "run_url": (data.get("run_url") or "").strip() or None,
        "logs_url": (data.get("logs_url") or "").strip() or None,
        "artifacts": data.get("artifacts") or [],
        "failure_class": failure_class or None,
        "failure_reason": (data.get("failure_reason") or "").strip() or None,
        "task_id": task_id or None,
        "claim_id": (data.get("claim_id") or "").strip() or None,
        "agent_id": (data.get("agent_id") or "").strip() or None,
        "principal_id": (data.get("principal_id") or "").strip() or None,
        "request": data.get("request") or {},
        "result": data.get("result") or {},
        "topology_contract": contract,
    }, {}


def create_external_ci_run(data: Dict[str, Any], actor: str = "system",
                           project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    normalized, error = _external_ci_request_payload(data or {}, project)
    if error:
        return error
    now = time.time()
    run_id = (data.get("run_id") or "ecir-" + uuid.uuid4().hex[:16]).strip()
    side_payload = {
        "source_project": normalized["source_project"],
        "source_repo": normalized["source_repo"],
        "source_branch": normalized["source_branch"],
        "source_sha": normalized["source_sha"],
        "mirror_repo": normalized["mirror_repo"],
        "mirror_branch": normalized["mirror_branch"],
        "workflow": normalized["workflow"],
        "status_context": normalized["status_context"],
        "required_status_contexts": normalized["required_status_contexts"],
        "ci_repo": normalized["mirror_repo"],
        "evidence_only": True,
        "task_id": normalized["task_id"],
        "claim_id": normalized["claim_id"],
    }
    request_payload = {
        **(normalized["request"] or {}),
        "source_repo": normalized["source_repo"],
        "source_sha": normalized["source_sha"],
        "ci_repo": normalized["mirror_repo"],
        "mirror_repo": normalized["mirror_repo"],
        "status_context": normalized["status_context"],
        "required_status_contexts": normalized["required_status_contexts"],
        "repo_topology": {
            "schema": normalized["topology_contract"].get("repo_topology_schema"),
            "source_project": normalized["source_project"],
            "source_repo": normalized["source_repo"],
            "ci_repo": normalized["mirror_repo"],
            "status_context": normalized["status_context"],
            "evidence_only": True,
        },
    }
    with _conn(project) as c:
        effect = _claim_external_effect_in(
            c,
            "external_ci_mirror",
            normalized["mirror_repo"],
            normalized["mirror_branch"],
            side_payload,
            task_id=normalized["task_id"],
            claim_id=normalized["claim_id"] or "",
            agent_id=normalized["agent_id"] or "",
            idem_key=(data.get("idem_key") or ""),
            actor=actor,
            principal_id=normalized["principal_id"] or "",
            project=project,
            now=now,
        )
        effect_key = effect["effect_key"]
        existing = c.execute("SELECT * FROM external_ci_runs WHERE effect_key=?",
                             (effect_key,)).fetchone()
        if existing:
            out = _external_ci_row(existing)
            out["idempotent"] = True
            out["side_effect"] = effect
            return out
        c.execute(
            """INSERT INTO external_ci_runs
               (run_id, source_project, source_repo, source_branch, source_sha,
                mirror_repo, mirror_branch, workflow, status_context, status, conclusion, run_url,
                logs_url, artifacts_json, failure_class, failure_reason, task_id,
                claim_id, agent_id, actor, principal_id, effect_key, request_json,
                result_json, requested_at, mirrored_at, triggered_at, completed_at,
                updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, normalized["source_project"], normalized["source_repo"],
                normalized["source_branch"], normalized["source_sha"], normalized["mirror_repo"],
                normalized["mirror_branch"], normalized["workflow"], normalized["status_context"],
                normalized["status"],
                normalized["conclusion"], normalized["run_url"], normalized["logs_url"],
                json.dumps(normalized["artifacts"], sort_keys=True),
                normalized["failure_class"], normalized["failure_reason"], normalized["task_id"],
                normalized["claim_id"], normalized["agent_id"], actor,
                normalized["principal_id"], effect_key,
                json.dumps(request_payload, sort_keys=True),
                json.dumps(normalized["result"], sort_keys=True),
                now,
                now if normalized["status"] in {"mirrored", "triggered", "running", "success", "failure"} else None,
                now if normalized["status"] in {"triggered", "running", "success", "failure"} else None,
                now if normalized["status"] in EXTERNAL_CI_TERMINAL_STATUSES else None,
                now,
            ),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (normalized["task_id"], actor, "external_ci.requested",
                   json.dumps({"run_id": run_id, "effect_key": effect_key,
                               "source_project": normalized["source_project"],
                               "source_repo": normalized["source_repo"],
                               "source_sha": normalized["source_sha"],
                               "ci_repo": normalized["mirror_repo"],
                               "mirror_repo": normalized["mirror_repo"],
                               "mirror_branch": normalized["mirror_branch"],
                               "workflow": normalized["workflow"],
                               "status_context": normalized["status_context"],
                               "evidence_only": True}, sort_keys=True), now))
        row = c.execute("SELECT * FROM external_ci_runs WHERE run_id=?", (run_id,)).fetchone()
    out = _external_ci_row(row)
    out["side_effect"] = effect
    return out


def update_external_ci_run(run_id: str, fields: Dict[str, Any], actor: str = "system",
                           project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    allowed = {"status", "conclusion", "run_url", "logs_url", "artifacts",
               "failure_class", "failure_reason", "result"}
    updates = {k: v for k, v in (fields or {}).items() if k in allowed}
    if not updates:
        return get_external_ci_run(run_id, project=project) or {"error": "external_ci_run not found"}
    status = _validate_external_ci_status(updates.get("status") or "")
    if "status" in updates and not status:
        return {"error": "invalid external CI status", "allowed": sorted(EXTERNAL_CI_STATUSES)}
    failure_class = _validate_external_ci_failure_class(updates.get("failure_class") or "")
    if (updates.get("failure_class") or "") and not failure_class:
        return {"error": "invalid external CI failure_class",
                "allowed": sorted(EXTERNAL_CI_FAILURE_CLASSES)}
    now = time.time()
    sets: List[str] = ["updated_at=?"]
    vals: List[Any] = [now]
    if "status" in updates:
        sets.append("status=?"); vals.append(status)
        if status in {"mirrored", "triggered", "running", "success", "failure"}:
            sets.append("mirrored_at=COALESCE(mirrored_at, ?)")
            vals.append(now)
        if status in {"triggered", "running", "success", "failure"}:
            sets.append("triggered_at=COALESCE(triggered_at, ?)")
            vals.append(now)
        if status in EXTERNAL_CI_TERMINAL_STATUSES:
            sets.append("completed_at=COALESCE(completed_at, ?)")
            vals.append(now)
    for key, column in (("conclusion", "conclusion"), ("run_url", "run_url"),
                        ("logs_url", "logs_url"), ("failure_reason", "failure_reason")):
        if key in updates:
            sets.append(f"{column}=?"); vals.append((updates.get(key) or "").strip() or None)
    if "failure_class" in updates:
        sets.append("failure_class=?"); vals.append(failure_class or None)
    if "artifacts" in updates:
        sets.append("artifacts_json=?")
        vals.append(json.dumps(updates.get("artifacts") or [], sort_keys=True))
    if "result" in updates:
        sets.append("result_json=?")
        vals.append(json.dumps(updates.get("result") or {}, sort_keys=True))
    vals.append(run_id)
    with _conn(project) as c:
        row = c.execute("SELECT * FROM external_ci_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return {"error": "external_ci_run not found", "run_id": run_id}
        c.execute(f"UPDATE external_ci_runs SET {', '.join(sets)} WHERE run_id=?", vals)
        if "status" in updates:
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"], actor, "external_ci.status",
                       json.dumps({"run_id": run_id, "status": status,
                                   "conclusion": updates.get("conclusion")},
                                  sort_keys=True), now))
        updated = c.execute("SELECT * FROM external_ci_runs WHERE run_id=?",
                            (run_id,)).fetchone()
    return _external_ci_row(updated)


def get_external_ci_run(run_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    init_db(project)
    with _conn(project) as c:
        return _external_ci_row(c.execute(
            "SELECT * FROM external_ci_runs WHERE run_id=?", (run_id,)).fetchone())


def list_external_ci_runs(task_id: str = "", source_project: str = "",
                          source_sha: str = "", status: str = "",
                          project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    init_db(project)
    q = "SELECT * FROM external_ci_runs WHERE 1=1"
    params: List[Any] = []
    if task_id:
        q += " AND task_id=?"; params.append(task_id.strip().upper())
    if source_project:
        q += " AND source_project=?"; params.append(source_project.strip())
    if source_sha:
        q += " AND source_sha=?"; params.append(source_sha.strip().lower())
    if status:
        q += " AND status=?"; params.append(status.strip().lower())
    q += " ORDER BY updated_at DESC, run_id"
    with _conn(project) as c:
        return [_external_ci_row(row) for row in c.execute(q, params).fetchall()]


def _sha_matches(candidate: str, target: str) -> bool:
    cand = (candidate or "").strip().lower()
    want = (target or "").strip().lower()
    if not cand or not want:
        return False
    return cand.startswith(want) or want.startswith(cand)


def _external_ci_summary(rows: List[Dict[str, Any]], source_sha: str = "",
                         project: str = DEFAULT_PROJECT,
                         contract: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if source_sha:
        rows = [r for r in rows if _sha_matches(r.get("source_sha") or "", source_sha)]
    success = [r for r in rows if r.get("status") == "success" and r.get("conclusion") == "success"]
    failures = [r for r in rows if r.get("status") in {"failure", "error", "cancelled"}]
    pending = [r for r in rows if r.get("status") in {"requested", "mirrored", "triggered", "running"}]
    latest = rows[0] if rows else None
    passed = bool(success)
    if passed:
        status = "passed"
        selected = success[0]
    elif pending:
        status = "pending"
        selected = latest
    elif failures:
        status = "failed"
        selected = latest
    else:
        status = "missing"
        selected = None
    contract = contract or _external_ci_topology_contract(project)
    source_repo = (
        (selected or {}).get("source_repo")
        or (rows[0].get("source_repo") if rows else None)
        or contract.get("source_repo")
    )
    ci_repo = (
        (selected or {}).get("ci_repo")
        or (selected or {}).get("mirror_repo")
        or (rows[0].get("ci_repo") if rows else None)
        or (rows[0].get("mirror_repo") if rows else None)
        or contract.get("ci_repo")
    )
    status_context = (
        (selected or {}).get("status_context")
        or (rows[0].get("status_context") if rows else None)
        or contract.get("status_context")
    )
    run_url = (selected or {}).get("run_url") or (rows[0].get("run_url") if rows else None)
    return {
        "status": status,
        "passed": passed,
        "required": False,
        "source_repo": source_repo,
        "source_sha": source_sha or ((selected or {}).get("source_sha") if selected else None),
        "ci_repo": ci_repo,
        "mirror_repo": ci_repo,
        "run_url": run_url,
        "status_context": status_context,
        "required_status_contexts": (
            (selected or {}).get("required_status_contexts")
            or (rows[0].get("required_status_contexts") if rows else None)
            or contract.get("required_status_contexts")
            or []
        ),
        "repo_role": "public_ci",
        "evidence_only": True,
        "run_count": len(rows),
        "success_count": len(success),
        "failure_count": len(failures),
        "pending_count": len(pending),
        "latest": selected,
        "runs": rows[:5],
    }


def _task_external_ci_summary_in(c: sqlite3.Connection, task_id: str,
                                 source_sha: str = "",
                                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    rows = [
        _external_ci_row(row)
        for row in c.execute(
            "SELECT * FROM external_ci_runs WHERE task_id=? "
            "ORDER BY updated_at DESC, run_id",
            (task_id,),
        ).fetchall()
    ]
    return _external_ci_summary(rows, source_sha=source_sha, project=project)


def task_external_ci_summary(task_id: str, source_sha: str = "",
                             project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    with _conn(project) as c:
        return _task_external_ci_summary_in(c, task_id, source_sha=source_sha, project=project)


def _external_ci_required_from(task: Dict[str, Any],
                               evidence: Optional[Dict[str, Any]] = None) -> bool:
    evidence = evidence or {}
    if evidence.get("external_ci_required") is True:
        return True
    gates = evidence.get("required_gates") or evidence.get("review_gates") or []
    if isinstance(gates, str):
        gates = coerce_csv_list(gates)
    if any(str(g).strip().lower() in {"external_ci", "external_ci_passed"} for g in gates):
        return True
    state = task.get("agent_state") or {}
    for key in ("review_gate", "review_gates", "proof_requirements"):
        value = state.get(key) or {}
        if isinstance(value, dict) and (
                value.get("external_ci_required") or value.get("external_ci_passed")):
            return True
    text = "\n".join(str(task.get(k) or "") for k in (
        "entry_criteria", "exit_criteria", "deliverable"))
    return "external_ci_passed" in text or "external ci passed" in text.lower()


def _external_ci_review_gate(task: Dict[str, Any],
                             evidence: Optional[Dict[str, Any]] = None,
                             c: Optional[sqlite3.Connection] = None,
                             project: str = DEFAULT_PROJECT,
                             summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    evidence = evidence or {}
    source_sha = (
        evidence.get("external_ci_source_sha")
        or evidence.get("source_sha")
        or evidence.get("head_sha")
        or (task.get("git_state") or {}).get("head_sha")
        or ""
    )
    if summary is not None:
        summary = dict(summary)
    elif c is None:
        with _conn(project) as own:
            summary = _task_external_ci_summary_in(
                own, task["task_id"], source_sha=source_sha, project=project)
    else:
        summary = _task_external_ci_summary_in(
            c, task["task_id"], source_sha=source_sha, project=project)
    required = _external_ci_required_from(task, evidence)
    summary["required"] = required
    summary["gate"] = {
        "name": "external_ci_passed",
        "required": required,
        "passed": summary["passed"],
        "status": (
            "passed" if summary["passed"] else
            "blocked" if required else
            "not_required"
        ),
        "message": (
            "External CI mirror passed for this source SHA."
            if summary["passed"] else
            "External CI mirror evidence is required before review/merge."
            if required else
            "External CI mirror evidence is optional for this task."
        ),
    }
    return summary


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


PUBLICATION_GUARD_STATUSES = {"passed", "failed", "warning", "unknown"}


def _publication_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    d = dict(row)
    d["guard"] = _json_obj(d.pop("guard_json", "{}"), {})
    d["repo_role"] = "public"
    d["evidence_only"] = True
    return d


def _validate_publication_guard_status(value: str) -> str:
    clean = (value or "unknown").strip().lower()
    return clean if clean in PUBLICATION_GUARD_STATUSES else ""


def _repo_mismatch(got: str, expected: str) -> bool:
    return bool(got and expected and _normalize_repo_slug(got) != _normalize_repo_slug(expected))


def _publication_topology_contract(source_project: str,
                                   data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = data or {}
    topology = get_project_repo_topology(source_project)
    roles = topology.get("roles") or {}
    canonical = roles.get("canonical") or {}
    public = roles.get("public") or {}
    script = (data.get("script") or data.get("publish_script") or "").strip()
    if not script:
        scripts = _coerce_str_list(public.get("publish_scripts"))
        script = scripts[0] if scripts else ""
    return {
        "schema": "switchboard.publication_topology_contract.v1",
        "source_project": source_project,
        "source_repo": (canonical.get("repo") or "").strip(),
        "public_repo": (public.get("repo") or data.get("public_repo") or "").strip(),
        "publish_scripts": _coerce_str_list(public.get("publish_scripts")),
        "script": script or None,
        "repo_topology_schema": topology.get("schema"),
        "repo_topology_valid": topology.get("valid"),
        "code_repo_gate": topology.get("code_repo_gate"),
        "public_role": public,
        "canonical_role": canonical,
        "evidence_only": True,
        "authority": "publish_evidence_only",
    }


def _publication_request_payload(data: Dict[str, Any], project: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source_project = (data.get("source_project") or project or DEFAULT_PROJECT).strip()
    if not has_project(source_project):
        return {}, {"error": f"unknown source project: {source_project}"}
    task_id = (data.get("task_id") or "").strip().upper()
    if task_id and not get_task(task_id, project=project):
        return {}, {"error": "unknown task", "task_id": task_id, "project": project}
    contract = _publication_topology_contract(source_project, data)
    if not (contract.get("code_repo_gate") or {}).get("passed"):
        return {}, {"error": "canonical source repo is not configured",
                    "source_project": source_project,
                    "code_repo_gate": contract.get("code_repo_gate")}
    source_repo, source_repo_error = _validate_github_repo(
        data.get("source_repo") or contract.get("source_repo") or get_project_github_repo(source_project))
    if source_repo_error:
        return {}, {"error": source_repo_error, "repo": source_repo, "field": "source_repo"}
    if not source_repo:
        return {}, {"error": "source_repo required", "source_project": source_project}
    if _repo_mismatch(source_repo, contract.get("source_repo") or ""):
        return {}, {"error": "source_repo must match repo_topology.roles.canonical.repo",
                    "repo": source_repo, "expected": contract.get("source_repo"),
                    "field": "source_repo", "source_project": source_project}
    public_repo, public_repo_error = _validate_github_repo(
        data.get("public_repo") or contract.get("public_repo") or "")
    if public_repo_error:
        return {}, {"error": public_repo_error, "repo": public_repo, "field": "public_repo"}
    if not public_repo:
        return {}, {"error": "public_repo required",
                    "hint": "configure repo_topology.roles.public.repo or pass public_repo"}
    configured_public = ((contract.get("public_role") or {}).get("repo") or "").strip()
    if _repo_mismatch(public_repo, configured_public):
        return {}, {"error": "public_repo must match repo_topology.roles.public.repo",
                    "repo": public_repo, "expected": configured_public,
                    "field": "public_repo", "source_project": source_project}
    source_sha = (data.get("source_sha") or "").strip()
    if not GIT_SHA_RE.match(source_sha):
        return {}, {"error": "source_sha must be a 7-64 character hex Git SHA"}
    public_sha = (data.get("public_sha") or "").strip()
    if public_sha and not GIT_SHA_RE.match(public_sha):
        return {}, {"error": "public_sha must be a 7-64 character hex Git SHA",
                    "field": "public_sha"}
    public_ref = (data.get("public_ref") or data.get("ref") or "").strip()
    public_tag = (data.get("public_tag") or data.get("tag") or "").strip() or None
    if not public_ref and public_tag:
        public_ref = f"refs/tags/{public_tag}"
    if not public_ref:
        return {}, {"error": "public_ref required"}
    guard_status = _validate_publication_guard_status(
        data.get("guard_status") or (data.get("guard") or {}).get("status") or "unknown")
    if not guard_status:
        return {}, {"error": "invalid publication guard_status",
                    "allowed": sorted(PUBLICATION_GUARD_STATUSES)}
    published_at = data.get("published_at") or data.get("timestamp")
    try:
        published_at = float(published_at) if published_at not in (None, "") else time.time()
    except (TypeError, ValueError):
        return {}, {"error": "published_at must be a unix timestamp"}
    return {
        "source_project": source_project,
        "source_repo": source_repo,
        "source_sha": source_sha.lower(),
        "public_repo": public_repo,
        "public_ref": public_ref,
        "public_sha": public_sha.lower() or None,
        "public_tag": public_tag,
        "script": (data.get("script") or data.get("publish_script") or contract.get("script") or "").strip() or None,
        "guard_status": guard_status,
        "guard": data.get("guard") or data.get("guard_result") or {},
        "artifact_url": (data.get("artifact_url") or "").strip() or None,
        "task_id": task_id or None,
        "claim_id": (data.get("claim_id") or "").strip() or None,
        "agent_id": (data.get("agent_id") or "").strip() or None,
        "principal_id": (data.get("principal_id") or "").strip() or None,
        "published_at": published_at,
        "topology_contract": contract,
    }, {}


def create_publication_evidence(data: Dict[str, Any], actor: str = "system",
                                project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    normalized, error = _publication_request_payload(data or {}, project)
    if error:
        return error
    now = time.time()
    publication_id = (data.get("publication_id") or "pub-" + uuid.uuid4().hex[:16]).strip()
    with _conn(project) as c:
        existing = c.execute(
            "SELECT * FROM publication_evidence WHERE publication_id=?",
            (publication_id,),
        ).fetchone()
        if existing:
            out = _publication_row(existing)
            out["idempotent"] = True
            return out
        c.execute(
            """INSERT INTO publication_evidence
               (publication_id, source_project, source_repo, source_sha, public_repo,
                public_ref, public_sha, public_tag, script, guard_status, guard_json,
                artifact_url, task_id, claim_id, agent_id, actor, principal_id,
                published_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                publication_id, normalized["source_project"], normalized["source_repo"],
                normalized["source_sha"], normalized["public_repo"], normalized["public_ref"],
                normalized["public_sha"], normalized["public_tag"], normalized["script"],
                normalized["guard_status"], json.dumps(normalized["guard"], sort_keys=True),
                normalized["artifact_url"], normalized["task_id"], normalized["claim_id"],
                normalized["agent_id"], actor, normalized["principal_id"],
                normalized["published_at"], now, now,
            ),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (normalized["task_id"], actor, "publication.recorded",
                   json.dumps({"publication_id": publication_id,
                               "source_project": normalized["source_project"],
                               "source_repo": normalized["source_repo"],
                               "source_sha": normalized["source_sha"],
                               "public_repo": normalized["public_repo"],
                               "public_ref": normalized["public_ref"],
                               "public_sha": normalized["public_sha"],
                               "public_tag": normalized["public_tag"],
                               "script": normalized["script"],
                               "guard_status": normalized["guard_status"],
                               "artifact_url": normalized["artifact_url"],
                               "evidence_only": True}, sort_keys=True), now))
        row = c.execute("SELECT * FROM publication_evidence WHERE publication_id=?",
                        (publication_id,)).fetchone()
    return _publication_row(row)


def list_publication_evidence(task_id: str = "", source_project: str = "",
                              source_sha: str = "", public_repo: str = "",
                              project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    init_db(project)
    q = "SELECT * FROM publication_evidence WHERE 1=1"
    params: List[Any] = []
    if task_id:
        q += " AND task_id=?"; params.append(task_id.strip().upper())
    if source_project:
        q += " AND source_project=?"; params.append(source_project.strip())
    if source_sha:
        q += " AND source_sha=?"; params.append(source_sha.strip().lower())
    if public_repo:
        q += " AND public_repo=?"; params.append(public_repo.strip())
    q += " ORDER BY updated_at DESC, publication_id"
    with _conn(project) as c:
        return [_publication_row(row) for row in c.execute(q, params).fetchall()]


def _publication_summary(rows: List[Dict[str, Any]],
                         source_sha: str = "") -> Dict[str, Any]:
    matched = rows
    if source_sha:
        matched = [r for r in rows if _sha_matches(r.get("source_sha") or "", source_sha)]
    passed = [r for r in matched if r.get("guard_status") == "passed"]
    failed = [r for r in matched if r.get("guard_status") == "failed"]
    latest = matched[0] if matched else (rows[0] if rows else None)
    if passed:
        status = "published"
        selected = passed[0]
    elif matched and failed:
        status = "failed"
        selected = latest
    elif matched:
        status = "unknown"
        selected = latest
    elif source_sha and rows:
        status = "stale"
        selected = latest
    else:
        status = "missing"
        selected = None
    return {
        "status": status,
        "passed": bool(passed),
        "required": False,
        "source_repo": (selected or {}).get("source_repo"),
        "source_sha": source_sha or ((selected or {}).get("source_sha") if selected else None),
        "public_repo": (selected or {}).get("public_repo"),
        "public_ref": (selected or {}).get("public_ref"),
        "public_sha": (selected or {}).get("public_sha"),
        "public_tag": (selected or {}).get("public_tag"),
        "script": (selected or {}).get("script"),
        "guard_status": (selected or {}).get("guard_status"),
        "artifact_url": (selected or {}).get("artifact_url"),
        "published_at": (selected or {}).get("published_at"),
        "publication_count": len(matched),
        "total_publication_count": len(rows),
        "latest": selected,
        "runs": rows[:5],
        "repo_role": "public",
        "evidence_only": True,
    }


def _task_publication_summary_in(c: sqlite3.Connection, task_id: str,
                                 source_sha: str = "") -> Dict[str, Any]:
    rows = [
        _publication_row(row)
        for row in c.execute(
            "SELECT * FROM publication_evidence WHERE task_id=? "
            "ORDER BY updated_at DESC, publication_id",
            (task_id,),
        ).fetchall()
    ]
    return _publication_summary(rows, source_sha=source_sha)


def task_publication_summary(task_id: str, source_sha: str = "",
                             project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    with _conn(project) as c:
        return _task_publication_summary_in(c, task_id, source_sha=source_sha)


def _publication_required_from(task: Dict[str, Any],
                               evidence: Optional[Dict[str, Any]] = None) -> bool:
    evidence = evidence or {}
    if evidence.get("publication_required") is True or evidence.get("publish_required") is True:
        return True
    gates = evidence.get("required_gates") or evidence.get("review_gates") or []
    if isinstance(gates, str):
        gates = coerce_csv_list(gates)
    wanted = {"publication", "publication_evidence", "publish_evidence",
              "public_mirror_published", "release_evidence"}
    if any(str(g).strip().lower() in wanted for g in gates):
        return True
    state = task.get("agent_state") or {}
    for key in ("review_gate", "review_gates", "proof_requirements"):
        value = state.get(key) or {}
        if isinstance(value, dict) and (
                value.get("publication_required")
                or value.get("publication_evidence")
                or value.get("publish_evidence")):
            return True
    text = "\n".join(str(task.get(k) or "") for k in (
        "entry_criteria", "exit_criteria", "deliverable"))
    lowered = text.lower()
    return (
        "publication_evidence" in lowered
        or "public_mirror_published" in lowered
        or "publish evidence" in lowered
        or "release evidence" in lowered
    )


def _publication_review_gate(task: Dict[str, Any],
                             evidence: Optional[Dict[str, Any]] = None,
                             c: Optional[sqlite3.Connection] = None,
                             project: str = DEFAULT_PROJECT,
                             summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    evidence = evidence or {}
    git_state = task.get("git_state") or {}
    source_sha = (
        evidence.get("publication_source_sha")
        or evidence.get("source_sha")
        or evidence.get("head_sha")
        or git_state.get("merged_sha")
        or git_state.get("head_sha")
        or ""
    )
    if summary is not None:
        summary = dict(summary)
    elif c is None:
        with _conn(project) as own:
            summary = _task_publication_summary_in(own, task["task_id"], source_sha=source_sha)
    else:
        summary = _task_publication_summary_in(c, task["task_id"], source_sha=source_sha)
    required = _publication_required_from(task, evidence)
    summary["required"] = required
    summary["gate"] = {
        "name": "publication_evidence",
        "required": required,
        "passed": summary["passed"],
        "status": (
            "passed" if summary["passed"] else
            "blocked" if required else
            "not_required"
        ),
        "message": (
            "Public mirror publication evidence passed for this source SHA."
            if summary["passed"] else
            "Public mirror publication evidence is required before publish/release review."
            if required else
            "Public mirror publication evidence is optional for this task."
        ),
    }
    return summary


def make_external_effect_key(effect_type: str, target: str, resource: str,
                             payload: Optional[Dict[str, Any]] = None,
                             idempotency_window_seconds: int = 0,
                             now: Optional[float] = None,
                             project: str = DEFAULT_PROJECT) -> Dict[str, str]:
    """Deterministic key for external effects that must not double-fire."""
    now = time.time() if now is None else float(now)
    effect_type = (effect_type or "").strip().lower()
    target = (target or "").strip()
    resource = (resource or "").strip()
    payload_hash = _payload_hash(payload)
    window_key = _effect_window_key(now, idempotency_window_seconds)
    basis = {
        "project": project,
        "effect_type": effect_type,
        "target": target,
        "resource": resource,
        "payload_hash": payload_hash,
        "window_key": window_key,
    }
    digest = hashlib.sha256(json.dumps(basis, sort_keys=True).encode("utf-8")).hexdigest()
    return {"effect_key": "effect-" + digest[:32],
            "payload_hash": payload_hash, "window_key": window_key}


def _external_effect_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    d = dict(row)
    d["payload"] = _json_obj(d.pop("payload_json", "{}"), {})
    d["readback"] = _json_obj(d.pop("readback_json", "{}"), {})
    return d


def _claim_external_effect_in(c: sqlite3.Connection, effect_type: str, target: str,
                              resource: str, payload: Optional[Dict[str, Any]] = None,
                              task_id: Optional[str] = None, claim_id: str = "",
                              agent_id: str = "", idem_key: str = "",
                              idempotency_window_seconds: int = 0,
                              actor: str = "system", principal_id: str = "",
                              project: str = DEFAULT_PROJECT,
                              now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else float(now)
    payload = _canonical_payload(payload)
    key = make_external_effect_key(
        effect_type, target, resource, payload,
        idempotency_window_seconds=idempotency_window_seconds, now=now, project=project)
    effect_key = key["effect_key"]
    row = c.execute("SELECT * FROM external_side_effects WHERE effect_key=?",
                    (effect_key,)).fetchone()
    if row:
        effect = _external_effect_row(row)
        out = {"claimed": False, "effect": effect, "effect_key": effect_key,
               "idempotent": effect["status"] == "verified"}
        if effect["status"] == "verified":
            out["verified"] = True
            out["proof"] = effect.get("readback") or {}
        elif effect["status"] in EXTERNAL_EFFECT_TERMINAL_STATUSES:
            out["reason"] = f"effect is {effect['status']}"
        else:
            out["reason"] = f"effect already {effect['status']}"
            out["readback_required"] = True
        return out
    c.execute(
        "INSERT INTO external_side_effects(effect_key, project, effect_type, target, "
        "resource, task_id, claim_id, agent_id, status, payload_hash, payload_json, "
        "idem_key, window_key, requested_by, claimed_by, principal_id, requested_at, "
        "claimed_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            effect_key, project, (effect_type or "").strip().lower(), target, resource,
            task_id, claim_id or None, agent_id or None, "claimed", key["payload_hash"],
            json.dumps(payload, sort_keys=True), idem_key or None, key["window_key"],
            actor, actor, principal_id or None, now, now, now,
        ),
    )
    event = {"effect_key": effect_key, "effect_type": (effect_type or "").strip().lower(),
             "target": target, "resource": resource, "payload_hash": key["payload_hash"],
             "status": "claimed", "claim_id": claim_id or None, "agent_id": agent_id or None}
    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
              (task_id, actor, "side_effect.claimed", json.dumps(event, sort_keys=True), now))
    row = c.execute("SELECT * FROM external_side_effects WHERE effect_key=?",
                    (effect_key,)).fetchone()
    return {"claimed": True, "effect": _external_effect_row(row), "effect_key": effect_key}


def claim_external_effect(effect_type: str, target: str, resource: str,
                          payload: Optional[Dict[str, Any]] = None,
                          task_id: Optional[str] = None, claim_id: str = "",
                          agent_id: str = "", idem_key: str = "",
                          idempotency_window_seconds: int = 0,
                          actor: str = "system", principal_id: str = "",
                          project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    with _conn(project) as c:
        return _claim_external_effect_in(
            c, effect_type, target, resource, payload, task_id=task_id,
            claim_id=claim_id, agent_id=agent_id, idem_key=idem_key,
            idempotency_window_seconds=idempotency_window_seconds, actor=actor,
            principal_id=principal_id, project=project)


def _update_external_effect_in(c: sqlite3.Connection, effect_key: str, status: str,
                               readback: Optional[Dict[str, Any]] = None,
                               last_error: str = "", actor: str = "system",
                               task_id: Optional[str] = None,
                               project: str = DEFAULT_PROJECT,
                               now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else float(now)
    row = c.execute("SELECT * FROM external_side_effects WHERE effect_key=?",
                    (effect_key,)).fetchone()
    if not row:
        return {"error": "effect_not_found", "effect_key": effect_key}
    effect = _external_effect_row(row)
    status = (status or "").strip().lower()
    if status not in {"issued", "verified", "failed", "dead_letter", "void"}:
        return {"error": "unsupported_effect_status", "status": status}
    readback_obj = _canonical_payload(readback if readback is not None else effect.get("readback"))
    sets = ["status=?", "readback_json=?", "updated_at=?"]
    vals: List[Any] = [status, json.dumps(readback_obj, sort_keys=True), now]
    if status == "issued":
        sets.extend(["issued_at=COALESCE(issued_at, ?)", "issued_by=COALESCE(issued_by, ?)"])
        vals.extend([now, actor])
    if status == "verified":
        sets.extend(["verified_at=COALESCE(verified_at, ?)", "verified_by=COALESCE(verified_by, ?)"])
        vals.extend([now, actor])
    if last_error:
        sets.append("last_error=?")
        vals.append(last_error)
    elif status in {"issued", "verified"}:
        sets.append("last_error=NULL")
    if status in {"failed", "dead_letter"}:
        sets.append("retry_count=retry_count+1")
    vals.append(effect_key)
    c.execute(f"UPDATE external_side_effects SET {', '.join(sets)} WHERE effect_key=?", vals)
    row = c.execute("SELECT * FROM external_side_effects WHERE effect_key=?",
                    (effect_key,)).fetchone()
    updated = _external_effect_row(row)
    event = {"effect_key": effect_key, "effect_type": updated["effect_type"],
             "target": updated["target"], "resource": updated["resource"],
             "status": status, "readback": readback_obj}
    if last_error:
        event["last_error"] = last_error
    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
              (task_id or updated.get("task_id"), actor, f"side_effect.{status}",
               json.dumps(event, sort_keys=True), now))
    return {"effect_key": effect_key, "effect": updated}


def mark_external_effect_issued(effect_key: str, readback: Optional[Dict[str, Any]] = None,
                                actor: str = "system",
                                project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    with _conn(project) as c:
        return _update_external_effect_in(c, effect_key, "issued", readback=readback,
                                          actor=actor, project=project)


def verify_external_effect(effect_key: str, readback: Optional[Dict[str, Any]] = None,
                           actor: str = "system",
                           project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    with _conn(project) as c:
        return _update_external_effect_in(c, effect_key, "verified", readback=readback,
                                          actor=actor, project=project)


def fail_external_effect(effect_key: str, error: str, readback: Optional[Dict[str, Any]] = None,
                         dead_letter: bool = False, actor: str = "system",
                         project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    with _conn(project) as c:
        return _update_external_effect_in(
            c, effect_key, "dead_letter" if dead_letter else "failed",
            readback=readback or {}, last_error=error or "effect_failed",
            actor=actor, project=project)


def list_external_effects(effect_type: str = "", status: str = "", task_id: str = "",
                          target: str = "", project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    init_db(project)
    q = "SELECT * FROM external_side_effects WHERE 1=1"
    params: List[Any] = []
    if effect_type:
        q += " AND effect_type=?"; params.append(effect_type.strip().lower())
    if status:
        q += " AND status=?"; params.append(status.strip().lower())
    if task_id:
        q += " AND task_id=?"; params.append(task_id)
    if target:
        q += " AND target=?"; params.append(target)
    q += " ORDER BY updated_at DESC, effect_key"
    with _conn(project) as c:
        return [_external_effect_row(row) for row in c.execute(q, params).fetchall()]


def append_activity(kind: str, actor: str, payload: Optional[Dict[str, Any]] = None,
                    task_id: Optional[str] = None,
                    project: str = DEFAULT_PROJECT) -> int:
    with _conn(project) as c:
        cur = c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (task_id, actor, kind, json.dumps(payload or {}, sort_keys=True), time.time()))
        return cur.lastrowid


def _register_agent_impl(agent_id: str, runtime: str, model: str = "", lane: str = "",
                         task_id: str = "", ttl_s: int = 120,
                         control: Optional[Dict[str, Any]] = None,
                         protocol: Optional[Dict[str, Any]] = None,
                         principal_id: str = "",
                         actor: str = "system",
                         project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    ttl_s = max(10, int(ttl_s or 120))
    compatibility = check_protocol_compatibility(protocol)
    stored_control = dict(control or {})
    if protocol:
        stored_control["protocol"] = protocol
    stored_control["protocol_compatibility"] = compatibility
    control_json = json.dumps(stored_control, sort_keys=True)
    with _conn(project) as c:
        c.execute(
            "INSERT OR REPLACE INTO agent_presence"
            "(agent_id, runtime, model, lane, task_id, control, principal_id, "
            "registered_at, heartbeat_at, ttl_s) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (agent_id, runtime, model or None, lane or None, task_id or None, control_json,
             principal_id or None, now, now, ttl_s),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id or None, actor, "agent.registered",
                   json.dumps({"agent_id": agent_id, "runtime": runtime, "lane": lane,
                               "control": control or {}, "protocol": protocol or {},
                               "protocol_compatibility": compatibility}, sort_keys=True), now))
    return {"agent_id": agent_id, "runtime": runtime, "model": model or None,
            "lane": lane or None, "task_id": task_id or None,
            "control": control or {}, "protocol": protocol or {},
            "protocol_compatibility": compatibility, "registered_at": now,
            "heartbeat_at": now, "expires_at": now + ttl_s, "ttl_s": ttl_s}


def register_agent(agent_id: str, runtime: str, model: str = "", lane: str = "",
                   task_id: str = "", ttl_s: int = 120,
                   control: Optional[Dict[str, Any]] = None,
                   protocol: Optional[Dict[str, Any]] = None,
                   principal_id: str = "",
                   actor: str = "system",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    # Resolve impl via store façade so PERF-2 monkeypatches on store._register_agent_impl
    # still bind after ARCH-MS-45 moved this body into shell.py.
    import store as _store_facade
    return _write_through(project, lambda: _store_facade._register_agent_impl(
        agent_id, runtime, model=model, lane=lane, task_id=task_id, ttl_s=ttl_s,
        control=control, protocol=protocol, principal_id=principal_id,
        actor=actor, project=project))


def heartbeat(agent_id: str, project: str = DEFAULT_PROJECT,
              actor: str = "system") -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        cur = c.execute("UPDATE agent_presence SET heartbeat_at=? WHERE agent_id=?",
                        (now, agent_id))
        row = c.execute("SELECT * FROM agent_presence WHERE agent_id=?", (agent_id,)).fetchone()
        if cur.rowcount:
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"] if row else None, actor, "agent.heartbeat",
                       json.dumps({"agent_id": agent_id}, sort_keys=True), now))
    if not row:
        return {"error": "agent not registered", "agent_id": agent_id}
    return _presence_row(row, now=now)


def _presence_row(row: sqlite3.Row, now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    ttl_s = row["ttl_s"]
    expires_at = row["heartbeat_at"] + ttl_s
    return {"agent_id": row["agent_id"], "runtime": row["runtime"], "model": row["model"],
            "lane": row["lane"], "task_id": row["task_id"],
            "control": json.loads(row["control"] or "{}"),
            "registered_at": row["registered_at"], "heartbeat_at": row["heartbeat_at"],
            "expires_at": expires_at, "ttl_s": ttl_s, "stale": now >= expires_at}


def _agent_delivery_state(c: sqlite3.Connection, agent_id: str,
                          now: float) -> Dict[str, Any]:
    agent_id = (agent_id or "").strip()
    if not agent_id:
        return {
            "status": "unreachable",
            "reason": "missing_agent_id",
            "reachable": False,
            "message": "Directed messages require a target agent_id.",
        }
    row = c.execute("SELECT * FROM agent_presence WHERE agent_id=?", (agent_id,)).fetchone()
    presence = _presence_row(row, now=now) if row else None
    delivery = {"agent_id": agent_id}
    if presence:
        delivery.update({
            "runtime": presence.get("runtime"),
            "lane": presence.get("lane"),
            "task_id": presence.get("task_id"),
            "heartbeat_at": presence.get("heartbeat_at"),
            "expires_at": presence.get("expires_at"),
            "ttl_s": presence.get("ttl_s"),
        })
    if not presence:
        delivery.update({
            "status": "unreachable",
            "reason": "not_registered",
            "reachable": False,
            "message": "No active or historical registration exists for this agent_id.",
        })
    elif presence.get("stale"):
        delivery.update({
            "status": "unreachable",
            "reason": "stale_registration",
            "reachable": False,
            "message": "Agent registration exists but its heartbeat has expired.",
        })
    else:
        delivery.update({
            "status": "active",
            "reason": None,
            "reachable": True,
            "control": presence.get("control") or {},
        })
    hosts = [_host_row(host, now=now) for host in c.execute(
        "SELECT * FROM agent_hosts ORDER BY heartbeat_at DESC"
    ).fetchall()]
    wakes = [_wake_row(wake) for wake in c.execute(
        "SELECT * FROM wake_intents WHERE status IN ('pending','claimed') "
        "ORDER BY requested_at"
    ).fetchall()]
    delivery.update(classify_agent_delivery(agent_id, presence, hosts, wakes))
    return delivery


def _active_agent_presence_in(c: sqlite3.Connection, agent_id: str,
                              now: float) -> Optional[Dict[str, Any]]:
    agent_id = (agent_id or "").strip()
    if not agent_id:
        return None
    row = c.execute("SELECT * FROM agent_presence WHERE agent_id=?", (agent_id,)).fetchone()
    if not row:
        return None
    presence = _presence_row(row, now=now)
    return None if presence.get("stale") else presence


def _active_agent_ids_for_task(c: sqlite3.Connection, task_id: str,
                               now: float) -> List[str]:
    if not task_id:
        return []
    rows = c.execute("SELECT * FROM agent_presence WHERE task_id=?",
                     (task_id,)).fetchall()
    active: List[str] = []
    for row in rows:
        presence = _presence_row(row, now=now)
        if not presence.get("stale"):
            active.append(presence["agent_id"])
    return active


def list_active_agents(lane: str = "", project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    now = time.time()
    with _conn(project) as c:
        if lane:
            rows = c.execute("SELECT * FROM agent_presence WHERE lane=? ORDER BY heartbeat_at DESC",
                             (lane,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM agent_presence ORDER BY heartbeat_at DESC").fetchall()
    return [p for p in (_presence_row(r, now=now) for r in rows) if not p["stale"]]


def _host_row(row: sqlite3.Row, now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    d = dict(row)
    runtimes = _json_obj(d.pop("runtimes_json", "[]"), [])
    limits = _json_obj(d.pop("limits_json", "{}"), {})
    capacity = _json_obj(d.pop("capacity_json", "{}"), {})
    ttl_s = int(d.get("heartbeat_ttl_s") or 60)
    expires_at = float(d.get("heartbeat_at") or 0) + ttl_s
    active = int(capacity.get("active_sessions") or 0)
    max_sessions = limits.get("max_sessions")
    try:
        max_sessions = int(max_sessions) if max_sessions is not None else None
    except Exception:
        max_sessions = None
    d.update({
        "runtimes": runtimes,
        "limits": limits,
        "capacity": capacity,
        "expires_at": expires_at,
        "stale": now >= expires_at or d.get("status") != "online",
        "available_sessions": (max(0, max_sessions - active)
                               if max_sessions is not None else None),
    })
    return d































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












def _selector_runtime_for_agent(agent_id: str) -> str:
    return infer_runtime_for_agent(agent_id)


def _runtime_matches_selector(runtime: Dict[str, Any], selector: Dict[str, Any]) -> bool:
    return runtime_matches_selector(runtime, selector)


def _host_can_handle(host: Dict[str, Any], selector: Dict[str, Any]) -> bool:
    if host.get("stale"):
        return False
    if host.get("available_sessions") is not None and host["available_sessions"] <= 0:
        return False
    return any(_runtime_matches_selector(rt, selector) for rt in host.get("runtimes") or [])


def _eligible_hosts_in(c: sqlite3.Connection, selector: Dict[str, Any],
                       now: float) -> List[Dict[str, Any]]:
    rows = c.execute("SELECT * FROM agent_hosts ORDER BY heartbeat_at DESC").fetchall()
    hosts = [_host_row(r, now=now) for r in rows]
    return [h for h in hosts if _host_can_handle(h, selector)]


def register_host(inventory: Dict[str, Any], principal_id: str = "",
                  actor: str = "system",
                  project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Register or refresh an always-on Agent Host inventory record."""
    started_at = time.time()
    now = time.time()
    host_id = (inventory.get("host_id") or "").strip()
    if not host_id:
        return {"error": "host_id required"}
    runtimes = inventory.get("runtimes") or []
    limits = inventory.get("limits") or {}
    capacity = inventory.get("capacity") or {}
    if "active_sessions" in inventory and "active_sessions" not in capacity:
        capacity["active_sessions"] = inventory.get("active_sessions")
    ttl_s = max(10, int(inventory.get("heartbeat_ttl_s") or inventory.get("ttl_s") or 60))
    try:
        with _control_plane_conn(project) as c:
            c.execute(
                "INSERT INTO agent_hosts(host_id, hostname, agent_host_version, repo_root, "
                "runtimes_json, limits_json, capacity_json, principal_id, registered_at, "
                "heartbeat_at, heartbeat_ttl_s, status, last_error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(host_id) DO UPDATE SET hostname=excluded.hostname, "
                "agent_host_version=excluded.agent_host_version, repo_root=excluded.repo_root, "
                "runtimes_json=excluded.runtimes_json, limits_json=excluded.limits_json, "
                "capacity_json=excluded.capacity_json, principal_id=excluded.principal_id, "
                "heartbeat_at=excluded.heartbeat_at, heartbeat_ttl_s=excluded.heartbeat_ttl_s, "
                "status=excluded.status, last_error=NULL",
                (host_id, inventory.get("hostname") or None,
                 inventory.get("agent_host_version") or None, inventory.get("repo_root") or None,
                 json.dumps(runtimes, sort_keys=True), json.dumps(limits, sort_keys=True),
                 json.dumps(capacity, sort_keys=True), principal_id or None, now, now, ttl_s,
                 "online", None),
            )
            payload = {"host_id": host_id, "runtimes": runtimes, "limits": limits,
                       "heartbeat_ttl_s": ttl_s}
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (None, actor, "agent_host.registered",
                       json.dumps(payload, sort_keys=True), now))
            row = c.execute("SELECT * FROM agent_hosts WHERE host_id=?", (host_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if _sqlite_busy(exc):
            return _control_plane_unavailable("register_host", project, started_at, exc)
        raise
    return _host_row(row, now=now)


def heartbeat_host(host_id: str, active_sessions: Optional[int] = None,
                   capacity: Optional[Dict[str, Any]] = None,
                   status: str = "online", last_error: str = "",
                   actor: str = "system",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    started_at = time.time()
    now = time.time()
    try:
        with _control_plane_conn(project) as c:
            row = c.execute("SELECT * FROM agent_hosts WHERE host_id=?", (host_id,)).fetchone()
            if not row:
                return {"error": "host not registered", "host_id": host_id}
            current = _json_obj(row["capacity_json"], {})
            if capacity:
                current.update(capacity)
            if active_sessions is not None:
                current["active_sessions"] = int(active_sessions)
            c.execute(
                "UPDATE agent_hosts SET heartbeat_at=?, capacity_json=?, status=?, last_error=? "
                "WHERE host_id=?",
                (now, json.dumps(current, sort_keys=True), status or "online",
                 last_error or None, host_id),
            )
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (None, actor, "agent_host.heartbeat",
                       json.dumps({"host_id": host_id, "capacity": current,
                                   "status": status or "online"}, sort_keys=True), now))
            row = c.execute("SELECT * FROM agent_hosts WHERE host_id=?", (host_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if _sqlite_busy(exc):
            return _control_plane_unavailable("heartbeat_host", project, started_at, exc)
        raise
    return _host_row(row, now=now)


def list_agent_hosts(runtime: str = "", lane: str = "", capability: str = "",
                     include_stale: bool = False,
                     project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    started_at = time.time()
    now = time.time()
    selector = {"runtime": runtime or "", "lane": lane or "",
                "capabilities": [capability] if capability else []}
    try:
        with _control_plane_conn(project) as c:
            rows = c.execute("SELECT * FROM agent_hosts ORDER BY heartbeat_at DESC").fetchall()
    except sqlite3.OperationalError as exc:
        if _sqlite_busy(exc):
            return [_control_plane_unavailable("list_agent_hosts", project, started_at, exc)]
        raise
    hosts = [_host_row(r, now=now) for r in rows]
    out = []
    for host in hosts:
        if host.get("stale") and not include_stale:
            continue
        if (runtime or lane or capability) and not any(
            _runtime_matches_selector(rt, selector) for rt in host.get("runtimes") or []
        ):
            continue
        out.append(host)
    return out


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


def host_status(host_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    started_at = time.time()
    now = time.time()
    try:
        with _control_plane_conn(project) as c:
            row = c.execute("SELECT * FROM agent_hosts WHERE host_id=?", (host_id,)).fetchone()
            if not row:
                return {"error": "host not registered", "host_id": host_id}
            host = _host_row(row, now=now)
            counts = c.execute(
                "SELECT status, COUNT(*) n FROM wake_intents WHERE claimed_by_host=? GROUP BY status",
                (host_id,),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if _sqlite_busy(exc):
            return _control_plane_unavailable("host_status", project, started_at, exc)
        raise
    host["wake_counts"] = {r["status"]: r["n"] for r in counts}
    return host


def _active_resource_leases_in(c: sqlite3.Connection, now: float,
                               resource_type: Optional[str] = None) -> List[Dict[str, Any]]:
    if resource_type:
        rows = c.execute("SELECT * FROM resource_leases WHERE released_at IS NULL "
                         "AND resource_type=?", (resource_type,)).fetchall()
    else:
        rows = c.execute("SELECT * FROM resource_leases WHERE released_at IS NULL").fetchall()
    return [dict(r) for r in rows if now < r["claimed_at"] + r["ttl_seconds"]]


def claim_resources(agent_id: str, resource_type: str, names: List[str],
                    task_id: Optional[str] = None, ttl_seconds: int = 1800,
                    principal_id: str = "", actor: str = "system",
                    idem_key: str = "",
                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    clean_names = sorted({n.strip() for n in names if n and n.strip()})
    payload = {"agent_id": agent_id, "resource_type": resource_type, "names": clean_names,
               "task_id": task_id, "ttl_seconds": ttl_seconds}
    if not clean_names:
        return {"error": "no resource names given"}
    with _conn(project) as c:
        hit = _idem_hit(c, "claim", idem_key, actor, payload)
        if hit is not None:
            return hit
        wanted = set(clean_names)
        for lease in _active_resource_leases_in(c, now, resource_type):
            if lease["agent_id"] == agent_id:
                continue
            overlap = wanted & set(json.loads(lease["names"] or "[]"))
            if overlap:
                expires_at = lease["claimed_at"] + lease["ttl_seconds"]
                response = {"conflict": lease["agent_id"], "resource_type": resource_type,
                            "names": sorted(overlap), "task_id": lease.get("task_id"),
                            "retry_after_seconds": max(5, int((expires_at - now) / 2))}
                _idem_store(c, "claim", idem_key, actor, payload, response)
                return response
        lease_id = "lease-" + uuid.uuid4().hex[:16]
        c.execute(
            "INSERT INTO resource_leases(id, agent_id, principal_id, task_id, resource_type, "
            "names, claimed_at, ttl_seconds) VALUES (?,?,?,?,?,?,?,?)",
            (lease_id, agent_id, principal_id or None, task_id, resource_type,
             json.dumps(clean_names), now, max(1, int(ttl_seconds or 1800))),
        )
        response = {"lease_id": lease_id, "agent_id": agent_id, "resource_type": resource_type,
                    "names": clean_names, "task_id": task_id, "claimed_at": now,
                    "expires_at": now + max(1, int(ttl_seconds or 1800))}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "lease.claimed", json.dumps(response, sort_keys=True), now))
        _idem_store(c, "claim", idem_key, actor, payload, response)
        return response


def check_resources(resource_type: str, names: List[str],
                    project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    now = time.time()
    wanted = {n.strip() for n in names if n and n.strip()}
    out: List[Dict[str, Any]] = []
    with _conn(project) as c:
        for lease in _active_resource_leases_in(c, now, resource_type):
            for name in wanted & set(json.loads(lease["names"] or "[]")):
                out.append({"resource_type": resource_type, "name": name,
                            "held_by": lease["agent_id"], "lease_id": lease["id"],
                            "task_id": lease.get("task_id"),
                            "expires_at": lease["claimed_at"] + lease["ttl_seconds"]})
    return sorted(out, key=lambda x: x["name"])


def release_resource_lease(lease_id: str, actor: str = "system",
                           project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM resource_leases WHERE id=?", (lease_id,)).fetchone()
        if not row:
            return {"error": "lease not found", "lease_id": lease_id}
        if row["released_at"] is not None:
            return {"released": False, "lease_id": lease_id, "note": "already released"}
        c.execute("UPDATE resource_leases SET released_at=? WHERE id=?", (now, lease_id))
        payload = {"lease_id": lease_id, "agent_id": row["agent_id"],
                   "resource_type": row["resource_type"], "names": json.loads(row["names"] or "[]")}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (row["task_id"], actor, "lease.released", json.dumps(payload, sort_keys=True), now))
    return {"released": True, "lease_id": lease_id}


def list_active_resource_leases(project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    now = time.time()
    with _conn(project) as c:
        leases = _active_resource_leases_in(c, now)
    return [{"lease_id": l["id"], "agent_id": l["agent_id"], "task_id": l.get("task_id"),
             "resource_type": l["resource_type"], "names": json.loads(l["names"] or "[]"),
             "expires_at": l["claimed_at"] + l["ttl_seconds"]} for l in leases]


RISK_ORDER = {"low": 1, "medium": 2, "med": 2, "high": 3, "critical": 4}
CAPABILITY_RE = re.compile(
    r"(?:requires?\s+capabilit(?:y|ies)|required\s+capabilit(?:y|ies)|capabilities)\s*[:=]\s*([^\n.;]+)",
    re.I,
)


def _risk_value(risk: str) -> int:
    return RISK_ORDER.get((risk or "").strip().lower(), 0)


def _task_required_capabilities(task: Dict[str, Any]) -> List[str]:
    dispatch_state = ((task.get("agent_state") or {}).get("dispatch") or {})
    raw = (dispatch_state.get("required_capabilities") or
           dispatch_state.get("capabilities") or [])
    caps = coerce_csv_list(raw)
    if not caps:
        text = "\n".join(str(task.get(k) or "") for k in (
            "description", "entry_criteria", "exit_criteria", "deliverable"))
        for m in CAPABILITY_RE.finditer(text):
            caps.extend(coerce_csv_list(m.group(1)))
    return sorted({c.strip().lower() for c in caps if c and c.strip()})


def _evidence_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "ok", "pass", "passed", "clean"}


def _evidence_sequence(value: Any) -> List[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _completion_evidence_has_tests(evidence: Dict[str, Any],
                                   session: Dict[str, Any]) -> bool:
    keys = ("tests", "test_commands", "verification_commands", "checks")
    if any(_evidence_sequence(evidence.get(key)) for key in keys):
        return True
    for key in ("verification", "verification_note", "test_results"):
        if str(evidence.get(key) or "").strip():
            return True
    hygiene = session.get("hygiene") or {}
    if any(_evidence_sequence(hygiene.get(key)) for key in keys):
        return True
    return bool(str(hygiene.get("verification") or "").strip())


def _executed_test_run_candidates(evidence: Dict[str, Any],
                                  session: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []

    def add(value: Any, source: str) -> None:
        if value in (None, ""):
            return
        if isinstance(value, dict):
            row = dict(value)
            row.setdefault("_source", source)
            candidates.append(row)
            return
        if isinstance(value, list):
            for item in value:
                add(item, source)

    for key in (
        "executed_test_run",
        "executed_test_runs",
        "test_run",
        "test_runs",
        "test_results",
        "verification_run",
        "verification_runs",
    ):
        add(evidence.get(key), f"evidence.{key}")
    hygiene = (session or {}).get("hygiene") or {}
    for key in (
        "executed_test_run",
        "executed_test_runs",
        "test_run",
        "test_runs",
        "test_results",
        "verification_run",
        "verification_runs",
    ):
        add(hygiene.get(key), f"hygiene.{key}")
    return candidates


def _executed_test_run_commands(run: Dict[str, Any]) -> List[Any]:
    commands: List[Any] = []
    for key in ("commands", "test_commands", "verification_commands", "checks"):
        commands.extend(_evidence_sequence(run.get(key)))
    if run.get("command") not in (None, ""):
        commands.append(run.get("command"))
    return [cmd for cmd in commands if str(cmd or "").strip()]


def _executed_test_run_has_output_hash(run: Dict[str, Any]) -> bool:
    for key in (
        "output_hash",
        "output_sha256",
        "stdout_sha256",
        "stderr_sha256",
        "log_hash",
        "logs_hash",
        "artifact_hash",
        "result_hash",
    ):
        if str(run.get(key) or "").strip():
            return True
    return False


def _executed_test_run_succeeded(run: Dict[str, Any]) -> bool:
    if run.get("executed") is False:
        return False
    if run.get("ok") is True or run.get("passed") is True:
        return True
    exit_code = run.get("exit_code", run.get("returncode"))
    if exit_code not in (None, ""):
        try:
            return int(exit_code) == 0
        except (TypeError, ValueError):
            return False
    status = str(run.get("status") or run.get("conclusion") or run.get("result") or "").strip().lower()
    return status in {"pass", "passed", "success", "succeeded", "ok", "green", "completed"}


def _executed_test_run_gate(evidence: Dict[str, Any],
                            session: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    candidates = _executed_test_run_candidates(evidence, session)
    problems: List[Dict[str, Any]] = []
    session_id = str((session or {}).get("work_session_id") or "").strip()
    session_branch = str((session or {}).get("branch") or "").strip()
    session_head = str((session or {}).get("head_sha") or "").strip()
    for run in candidates:
        source = run.get("_source")
        run_id = str(run.get("run_id") or run.get("id") or "").strip() or None
        run_schema = str(run.get("schema") or "").strip()
        commands = _executed_test_run_commands(run)
        run_problems: List[Dict[str, Any]] = []
        if run_schema and run_schema != EXECUTED_TEST_RUN_SCHEMA:
            run_problems.append({"reason": "unknown_test_run_schema",
                                 "message": "Executed test run schema is not recognized.",
                                 "schema": run_schema})
        if not commands:
            run_problems.append({"reason": "missing_test_commands",
                                 "message": "Executed test run must include the command(s) that ran."})
        if not _executed_test_run_succeeded(run):
            run_problems.append({"reason": "test_run_failed",
                                 "message": "Executed test run did not record a passing result.",
                                 "status": run.get("status") or run.get("conclusion"),
                                 "exit_code": run.get("exit_code", run.get("returncode"))})
        if not _executed_test_run_has_output_hash(run):
            run_problems.append({"reason": "missing_test_output_hash",
                                 "message": "Executed test run must include an output/log/artifact hash."})
        if not any(str(run.get(key) or "").strip() for key in ("completed_at", "executed_at", "finished_at")):
            run_problems.append({"reason": "missing_test_completion_time",
                                 "message": "Executed test run must include completed_at/executed_at/finished_at."})
        run_session_id = str(run.get("work_session_id") or "").strip()
        if session_id and run_session_id and run_session_id != session_id:
            run_problems.append({"reason": "wrong_test_work_session",
                                 "message": "Executed test run belongs to a different Work Session.",
                                 "test_work_session_id": run_session_id,
                                 "work_session_id": session_id})
        run_branch = str(run.get("branch") or "").strip()
        if session_branch and run_branch and run_branch != session_branch:
            run_problems.append({"reason": "stale_test_branch",
                                 "message": "Executed test run branch does not match the Work Session.",
                                 "test_branch": run_branch,
                                 "work_session_branch": session_branch})
        run_head = str(run.get("head_sha") or "").strip()
        if session_head and run_head and run_head != session_head:
            run_problems.append({"reason": "stale_test_head_sha",
                                 "message": "Executed test run head_sha does not match the Work Session.",
                                 "test_head_sha": run_head,
                                 "work_session_head_sha": session_head})
        if not run_problems:
            clean = {k: v for k, v in run.items() if k != "_source"}
            return {"ok": True, "schema": EXECUTED_TEST_RUN_SCHEMA,
                    "source": source, "run_id": run_id, "run": clean}
        problems.append({"source": source, "run_id": run_id, "problems": run_problems})
    return {"ok": False, "schema": EXECUTED_TEST_RUN_SCHEMA,
            "reason": "missing_executed_test_run" if not candidates else "invalid_executed_test_run",
            "message": (
                "Completion evidence must include a passing executed test run with commands, "
                "completion time, and output/log hash."
            ),
            "problems": problems}


def _completion_evidence_has_diff_check(evidence: Dict[str, Any],
                                        session: Dict[str, Any]) -> bool:
    for key in ("git_diff_check", "diff_check", "diff_check_clean"):
        if key in evidence and _evidence_truthy(evidence.get(key)):
            return True
    for item in _evidence_sequence(evidence.get("checks")):
        text = json.dumps(item, sort_keys=True) if isinstance(item, dict) else str(item)
        if "git diff --check" in text and not any(word in text.lower() for word in ("fail", "failed")):
            return True
    for item in _evidence_sequence(evidence.get("verification_commands")):
        if "git diff --check" in str(item):
            return True
    hygiene = session.get("hygiene") or {}
    for key in ("git_diff_check", "diff_check", "diff_check_clean"):
        if key in hygiene and _evidence_truthy(hygiene.get(key)):
            return True
    return False


def _completion_has_push_or_review_evidence(evidence: Dict[str, Any]) -> bool:
    if evidence.get("pr_url") or evidence.get("pr_number"):
        return True
    if evidence.get("pushed_at") or evidence.get("remote_ref"):
        return True
    offline = evidence.get("offline_evidence")
    return bool(offline if isinstance(offline, dict) else str(offline or "").strip())



def _budget_status(max_budget_usd: Optional[float], spent_usd: float) -> Dict[str, Any]:
    remaining = max_budget_usd - spent_usd if max_budget_usd is not None else None
    if max_budget_usd is None:
        status = "not_limited"
    elif remaining is not None and remaining < 0:
        status = "over_budget"
    elif max_budget_usd and spent_usd >= max_budget_usd * 0.9:
        status = "tight"
    else:
        status = "ok"
    return {"budget_usd": max_budget_usd, "spent_usd": round(spent_usd, 6),
            "remaining_usd": round(remaining, 6) if remaining is not None else None,
            "status": status}


def _dispatch_score(task: Dict[str, Any], requested_lanes: set,
                    requested_caps: set, tally: Dict[str, Any],
                    max_budget_usd: Optional[float]) -> Dict[str, Any]:
    sort_order = int(task.get("sort_order") or 0)
    lane = (task.get("_wsId") or "").upper()
    required_caps = _task_required_capabilities(task)
    matched_caps = sorted(set(required_caps) & requested_caps)
    capability_fit = ((len(matched_caps) / len(required_caps)) if required_caps else 1.0)
    budget = _budget_status(max_budget_usd, float(tally["spend"]["cost_usd"] or 0.0))
    verified = len([o for o in tally.get("outcomes", []) if o.get("status") == "verified"])
    proposed = len([o for o in tally.get("outcomes", []) if o.get("status") == "proposed"])
    factors = {
        "blocking": 10000 if task.get("is_blocking") else 0,
        "sort_order": max(0, 1000 - min(sort_order, 1000)),
        "lane_affinity": 250 if requested_lanes and lane in requested_lanes else 0,
        "capability_fit": int(capability_fit * 200),
        "risk_fit": max(0, 120 - (_risk_value(task.get("risk_level") or "") * 20)),
        "budget_fit": 100 if budget["status"] in ("not_limited", "ok") else 0,
        "verified_outcome_signal": min(verified, 5) * 15,
        "pending_value_signal": min(proposed, 5) * 5,
    }
    return {"score": sum(factors.values()), "factors": factors,
            "required_capabilities": required_caps, "matched_capabilities": matched_caps,
            "budget": budget}


def _model_recommendation(task: Dict[str, Any], score: Dict[str, Any]) -> Dict[str, str]:
    risk = _risk_value(task.get("risk_level") or "")
    budget_status = score["budget"]["status"]
    if risk >= 3:
        tier = "high"
    elif budget_status == "tight":
        tier = "small"
    elif score["required_capabilities"]:
        tier = "balanced"
    else:
        tier = "small"
    return {"model_tier": tier,
            "reason": f"risk={task.get('risk_level') or 'unspecified'}, "
                      f"budget={budget_status}, "
                      f"capabilities={','.join(score['required_capabilities']) or 'none'}"}


def report_usage(source: str, confidence: str, task_id: Optional[str] = None,
                 claim_id: Optional[str] = None, outcome_id: Optional[str] = None,
                 agent_id: Optional[str] = None, principal_id: str = "",
                 runtime: str = "", call_site: str = "", provider: str = "",
                 model: str = "", prompt_tokens: int = 0,
                 completion_tokens: int = 0, total_tokens: Optional[int] = None,
                 cost_usd: float = 0.0, latency_ms: Optional[float] = None,
                 status: str = "ok", metadata: Optional[Dict[str, Any]] = None,
                 request_id: Optional[str] = None,
                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    total = int(total_tokens if total_tokens is not None else prompt_tokens + completion_tokens)
    now = time.time()
    with _conn(project) as c:
        if outcome_id and not task_id:
            outcome = c.execute("SELECT task_id FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
            if outcome:
                task_id = outcome["task_id"]
        if request_id:
            old = c.execute("SELECT * FROM llm_spend WHERE request_id=?", (request_id,)).fetchone()
            if old:
                return _spend_row(old)
        cur = c.execute(
            "INSERT INTO llm_spend(request_id, source, confidence, task_id, claim_id, outcome_id, "
            "agent_id, principal_id, runtime, call_site, provider, model, prompt_tokens, "
            "completion_tokens, total_tokens, cost_usd, latency_ms, status, metadata_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, source, confidence, task_id, claim_id, outcome_id, agent_id,
             principal_id or None, runtime or None, call_site or None, provider or None, model or None,
             int(prompt_tokens or 0), int(completion_tokens or 0), total, float(cost_usd or 0.0),
             latency_ms, status or "ok", json.dumps(metadata or {}, sort_keys=True), now),
        )
        row = c.execute("SELECT * FROM llm_spend WHERE id=?", (cur.lastrowid,)).fetchone()
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, agent_id or principal_id or "tally", "tally.usage_reported",
                   json.dumps({"spend_id": cur.lastrowid, "source": source,
                               "cost_usd": float(cost_usd or 0.0)}, sort_keys=True), now))
    return _spend_row(row)


def _spend_row(row: sqlite3.Row) -> Dict[str, Any]:
    out = dict(row)
    out["metadata"] = json.loads(out.pop("metadata_json") or "{}")
    return out


def _outcome_row(row: sqlite3.Row) -> Dict[str, Any]:
    out = dict(row)
    out["evidence"] = json.loads(out.pop("evidence_json") or "{}")
    out["value"] = json.loads(out.pop("value_json") or "{}")
    return out


def _kpi_row(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


def _outcome_kpi_link_row(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


def record_outcome(outcome_type: str, title: str,
                   task_id: Optional[str] = None, claim_id: Optional[str] = None,
                   epic_id: Optional[str] = None, status: str = "proposed",
                   verifier: str = "", verification: str = "",
                   evidence: Optional[Dict[str, Any]] = None,
                   value: Optional[Dict[str, Any]] = None,
                   actor: str = "tally",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    status = (status or "proposed").strip().lower()
    if status not in ("proposed", "verified", "rejected", "superseded"):
        return {"error": "invalid outcome status", "status": status}
    if not outcome_type or not title:
        return {"error": "outcome_type and title required"}
    now = time.time()
    outcome_id = "outcome-" + uuid.uuid4().hex[:16]
    verified_at = now if status == "verified" else None
    with _conn(project) as c:
        c.execute(
            "INSERT INTO outcomes(id, project, task_id, epic_id, claim_id, type, title, status, "
            "verifier, verification, evidence_json, value_json, created_at, verified_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (outcome_id, project, task_id or None, epic_id or None, claim_id or None,
             outcome_type, title, status, verifier or None, verification or None,
             json.dumps(_jsonish(evidence), sort_keys=True),
             json.dumps(_jsonish(value), sort_keys=True), now, verified_at),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "tally.outcome_recorded",
                   json.dumps({"outcome_id": outcome_id, "status": status,
                               "type": outcome_type, "title": title}, sort_keys=True), now))
        row = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
    return _outcome_row(row)


def verify_outcome(outcome_id: str, verifier: str, verification: str = "",
                   evidence: Optional[Dict[str, Any]] = None,
                   actor: str = "tally",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
        if not row:
            return {"error": "outcome not found", "outcome_id": outcome_id}
        merged_evidence = json.loads(row["evidence_json"] or "{}")
        merged_evidence.update(_jsonish(evidence))
        c.execute(
            "UPDATE outcomes SET status='verified', verifier=?, verification=?, "
            "evidence_json=?, verified_at=? WHERE id=?",
            (verifier or actor, verification or None,
             json.dumps(merged_evidence, sort_keys=True), now, outcome_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (row["task_id"], actor, "tally.outcome_verified",
                   json.dumps({"outcome_id": outcome_id, "verifier": verifier or actor,
                               "verification": verification or None}, sort_keys=True), now))
        row = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
    return _outcome_row(row)


def reject_outcome(outcome_id: str, verifier: str, reason: str,
                   actor: str = "tally",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
        if not row:
            return {"error": "outcome not found", "outcome_id": outcome_id}
        evidence = json.loads(row["evidence_json"] or "{}")
        evidence["rejection_reason"] = reason
        c.execute(
            "UPDATE outcomes SET status='rejected', verifier=?, verification='rejected', "
            "evidence_json=? WHERE id=?",
            (verifier or actor, json.dumps(evidence, sort_keys=True), outcome_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (row["task_id"], actor, "tally.outcome_rejected",
                   json.dumps({"outcome_id": outcome_id, "reason": reason}, sort_keys=True), now))
        row = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
    return _outcome_row(row)


def create_kpi(name: str, unit: str, direction: str,
               owner: str = "", baseline_value: Optional[float] = None,
               current_value: Optional[float] = None,
               target_value: Optional[float] = None,
               period: str = "", actor: str = "tally",
               project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    direction = (direction or "").strip().lower()
    if direction not in ("increase", "decrease", "maintain"):
        return {"error": "direction must be increase, decrease, or maintain"}
    if not name or not unit:
        return {"error": "name and unit required"}
    now = time.time()
    kpi_id = "kpi-" + uuid.uuid4().hex[:16]
    if current_value is None:
        current_value = baseline_value
    with _conn(project) as c:
        c.execute(
            "INSERT INTO kpis(id, project, name, unit, direction, owner, baseline_value, "
            "current_value, target_value, period, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (kpi_id, project, name, unit, direction, owner or None, baseline_value,
             current_value, target_value, period or None, now, now),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "tally.kpi_created",
                   json.dumps({"kpi_id": kpi_id, "name": name, "unit": unit,
                               "direction": direction}, sort_keys=True), now))
        row = c.execute("SELECT * FROM kpis WHERE id=?", (kpi_id,)).fetchone()
    return _kpi_row(row)


def update_kpi_value(kpi_id: str, current_value: float,
                     evidence: Optional[Dict[str, Any]] = None,
                     actor: str = "tally",
                     project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM kpis WHERE id=?", (kpi_id,)).fetchone()
        if not row:
            return {"error": "kpi not found", "kpi_id": kpi_id}
        c.execute("UPDATE kpis SET current_value=?, updated_at=? WHERE id=?",
                  (current_value, now, kpi_id))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "tally.kpi_updated",
                   json.dumps({"kpi_id": kpi_id, "current_value": current_value,
                               "evidence": _jsonish(evidence)}, sort_keys=True), now))
        row = c.execute("SELECT * FROM kpis WHERE id=?", (kpi_id,)).fetchone()
    return _kpi_row(row)


def link_outcome_to_kpi(outcome_id: str, kpi_id: str,
                        contribution: Optional[float] = None,
                        contribution_unit: str = "",
                        confidence: str = "directional",
                        rationale: str = "",
                        actor: str = "tally",
                        project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    confidence = (confidence or "directional").strip().lower()
    if confidence not in ("measured", "estimated", "directional"):
        return {"error": "confidence must be measured, estimated, or directional"}
    now = time.time()
    link_id = "okpi-" + uuid.uuid4().hex[:16]
    with _conn(project) as c:
        outcome = c.execute("SELECT * FROM outcomes WHERE id=?", (outcome_id,)).fetchone()
        if not outcome:
            return {"error": "outcome not found", "outcome_id": outcome_id}
        kpi = c.execute("SELECT * FROM kpis WHERE id=?", (kpi_id,)).fetchone()
        if not kpi:
            return {"error": "kpi not found", "kpi_id": kpi_id}
        c.execute(
            "INSERT INTO outcome_kpi_links(id, project, outcome_id, kpi_id, contribution, "
            "contribution_unit, confidence, rationale, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (link_id, project, outcome_id, kpi_id, contribution, contribution_unit or kpi["unit"],
             confidence, rationale or None, now),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (outcome["task_id"], actor, "tally.outcome_kpi_linked",
                   json.dumps({"link_id": link_id, "outcome_id": outcome_id, "kpi_id": kpi_id,
                               "contribution": contribution, "confidence": confidence},
                              sort_keys=True), now))
        row = c.execute("SELECT * FROM outcome_kpi_links WHERE id=?", (link_id,)).fetchone()
    return _outcome_kpi_link_row(row)


def _spend_for_task(c: sqlite3.Connection, task_id: str,
                    outcomes: List[Dict[str, Any]]) -> List[sqlite3.Row]:
    outcome_ids = [o["id"] for o in outcomes]
    claim_ids = [o["claim_id"] for o in outcomes if o.get("claim_id")]
    clauses = ["task_id=?"]
    params: List[Any] = [task_id]
    if outcome_ids:
        clauses.append("outcome_id IN (%s)" % ",".join("?" for _ in outcome_ids))
        params.extend(outcome_ids)
    if claim_ids:
        clauses.append("claim_id IN (%s)" % ",".join("?" for _ in claim_ids))
        params.extend(claim_ids)
    return c.execute("SELECT * FROM llm_spend WHERE " + " OR ".join(clauses), params).fetchall()


def _spend_summary(rows: List[sqlite3.Row]) -> Dict[str, Any]:
    spend = {"cost_usd": 0.0, "total_tokens": 0, "by_source": {}, "by_model": {}}
    seen = set()
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        cost = float(row["cost_usd"] or 0.0)
        tokens = int(row["total_tokens"] or 0)
        source = row["source"]
        bucket = spend["by_source"].setdefault(source, {"cost_usd": 0.0, "total_tokens": 0,
                                                        "confidence": row["confidence"]})
        bucket["cost_usd"] += cost
        bucket["total_tokens"] += tokens
        # UI-12: per-model breakdown drives the model-mix line in the Economics panels.
        model = row["model"] or "unknown"
        mbucket = spend["by_model"].setdefault(model, {"cost_usd": 0.0, "total_tokens": 0})
        mbucket["cost_usd"] += cost
        mbucket["total_tokens"] += tokens
        spend["cost_usd"] += cost
        spend["total_tokens"] += tokens
    spend["cost_usd"] = round(spend["cost_usd"], 6)
    for bucket in spend["by_source"].values():
        bucket["cost_usd"] = round(bucket["cost_usd"], 6)
    for mbucket in spend["by_model"].values():
        mbucket["cost_usd"] = round(mbucket["cost_usd"], 6)
    return spend


def kpi_tally(kpi_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    with _conn(project) as c:
        kpi = c.execute("SELECT * FROM kpis WHERE id=?", (kpi_id,)).fetchone()
        if not kpi:
            return {"error": "kpi not found", "kpi_id": kpi_id}
        rows = c.execute(
            "SELECT o.*, l.id link_id, l.contribution, l.contribution_unit, "
            "l.confidence link_confidence, l.rationale "
            "FROM outcome_kpi_links l JOIN outcomes o ON o.id=l.outcome_id "
            "WHERE l.kpi_id=? ORDER BY l.created_at",
            (kpi_id,),
        ).fetchall()
    outcomes = []
    verified_contribution = 0.0
    task_ids = set()
    for row in rows:
        outcome = _outcome_row(row)
        outcome["link"] = {
            "id": row["link_id"],
            "contribution": row["contribution"],
            "contribution_unit": row["contribution_unit"],
            "confidence": row["link_confidence"],
            "rationale": row["rationale"],
        }
        outcomes.append(outcome)
        if outcome["status"] == "verified" and row["contribution"] is not None:
            verified_contribution += float(row["contribution"] or 0.0)
        if outcome.get("task_id"):
            task_ids.add(outcome["task_id"])
    spend_rows = []
    for task_id in task_ids:
        with _conn(project) as c:
            task_outcomes = [_outcome_row(r) for r in c.execute(
                "SELECT * FROM outcomes WHERE task_id=?", (task_id,)).fetchall()]
            spend_rows.extend(_spend_for_task(c, task_id, task_outcomes))
    spend = _spend_summary(spend_rows)
    return {
        "kpi": _kpi_row(kpi),
        "spend": spend,
        "outcomes": outcomes,
        "verified_contribution": round(verified_contribution, 6),
        "unit_cost": {
            "cost_per_contribution_unit": (
                round(spend["cost_usd"] / verified_contribution, 6)
                if verified_contribution else None
            )
        },
    }


def _merge_spend_totals(target: Dict[str, Any], spend: Dict[str, Any]) -> None:
    target["cost_usd"] = round(float(target.get("cost_usd") or 0.0) +
                              float(spend.get("cost_usd") or 0.0), 6)
    target["total_tokens"] = int(target.get("total_tokens") or 0) + int(spend.get("total_tokens") or 0)
    by_source = target.setdefault("by_source", {})
    for source, bucket in (spend.get("by_source") or {}).items():
        dst = by_source.setdefault(source, {
            "cost_usd": 0.0,
            "total_tokens": 0,
            "confidence": bucket.get("confidence"),
        })
        dst["cost_usd"] = round(float(dst.get("cost_usd") or 0.0) +
                                float(bucket.get("cost_usd") or 0.0), 6)
        dst["total_tokens"] = int(dst.get("total_tokens") or 0) + int(bucket.get("total_tokens") or 0)
        if bucket.get("confidence"):
            dst["confidence"] = bucket["confidence"]
    by_model = target.setdefault("by_model", {})
    for model, bucket in (spend.get("by_model") or {}).items():
        dst = by_model.setdefault(model, {"cost_usd": 0.0, "total_tokens": 0})
        dst["cost_usd"] = round(float(dst.get("cost_usd") or 0.0) +
                                float(bucket.get("cost_usd") or 0.0), 6)
        dst["total_tokens"] = int(dst.get("total_tokens") or 0) + int(bucket.get("total_tokens") or 0)


def list_kpis(project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """All KPIs for a project with their live rollup (UI-2 tiles).

    Each entry is the KPI row plus verified_contribution, spend, and cost-per-unit
    from kpi_tally so the tile can show movement and unit economics without a
    second round-trip per KPI."""
    with _conn(project) as c:
        rows = c.execute("SELECT * FROM kpis ORDER BY created_at").fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        kpi = _kpi_row(row)
        tally = kpi_tally(kpi["id"], project=project)
        kpi["verified_contribution"] = tally.get("verified_contribution", 0.0)
        kpi["spend"] = tally.get("spend", {})
        kpi["unit_cost"] = tally.get("unit_cost", {})
        kpi["outcome_count"] = len(tally.get("outcomes", []))
        out.append(kpi)
    return out


def list_outcomes(project: str = DEFAULT_PROJECT, status: str = "",
                  limit: int = 200) -> List[Dict[str, Any]]:
    """Outcomes for a project, newest first, each with its KPI links (UI-2 queue).

    status filters to one lifecycle state (e.g. 'proposed' for the verify queue);
    empty returns all. limit caps the result."""
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200
    clauses = ""
    params: List[Any] = []
    if status:
        clauses = " WHERE status=?"
        params.append(status)
    params.append(limit)
    with _conn(project) as c:
        rows = c.execute(
            "SELECT * FROM outcomes" + clauses + " ORDER BY created_at DESC LIMIT ?",
            params).fetchall()
        outcomes = [_outcome_row(r) for r in rows]
        if outcomes:
            ids = [o["id"] for o in outcomes]
            links = c.execute(
                "SELECT l.outcome_id, l.kpi_id, l.contribution, l.contribution_unit, "
                "l.confidence, k.name kpi_name, k.unit kpi_unit "
                "FROM outcome_kpi_links l JOIN kpis k ON k.id=l.kpi_id "
                "WHERE l.outcome_id IN (%s)" % ",".join("?" for _ in ids), ids).fetchall()
            by_outcome: Dict[str, List[Dict[str, Any]]] = {}
            for link in links:
                by_outcome.setdefault(link["outcome_id"], []).append(dict(link))
            for outcome in outcomes:
                outcome["kpi_links"] = by_outcome.get(outcome["id"], [])
    return outcomes


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


def _active_leases_in(c, now: float) -> List[Dict[str, Any]]:
    """Active leases using an existing connection — not released and not TTL-expired."""
    rows = c.execute("SELECT * FROM file_leases WHERE released_at IS NULL").fetchall()
    return [dict(r) for r in rows if now < r["claimed_at"] + r["ttl_minutes"] * 60]


def claim_files(agent_id: str, files: List[str], task_id: Optional[str] = None,
                ttl_minutes: int = 30, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Claim a set of file paths for an agent. Returns {lease_id, files, expires_at} on
    success, or {conflict, task_id, files, retry_after_seconds} if any file is held by
    another active lease. Same agent claiming its own files is idempotent (no conflict)."""
    now = time.time()
    file_set = set(files)
    with _conn(project) as c:
        for lease in _active_leases_in(c, now):
            if lease["agent_id"] == agent_id:
                continue
            held = set(json.loads(lease["files"] or "[]"))
            overlap = file_set & held
            if overlap:
                expires_at = lease["claimed_at"] + lease["ttl_minutes"] * 60
                remaining = max(0.0, expires_at - now)
                return {"conflict": lease["agent_id"], "task_id": lease.get("task_id"),
                        "files": sorted(overlap),
                        "retry_after_seconds": max(30, int(remaining / 2))}
        lease_id = f"lease-{agent_id}-{int(now)}"
        c.execute(
            "INSERT OR REPLACE INTO file_leases(id, agent_id, task_id, files, claimed_at, ttl_minutes) "
            "VALUES (?,?,?,?,?,?)",
            (lease_id, agent_id, task_id, json.dumps(sorted(files)), now, ttl_minutes),
        )
    expires_at = now + ttl_minutes * 60
    return {"lease_id": lease_id, "agent_id": agent_id, "task_id": task_id,
            "files": sorted(files), "expires_at": expires_at, "ttl_minutes": ttl_minutes}


def release_files(lease_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Release a lease by id. Returns {released: true} or {error: ...}."""
    now = time.time()
    with _conn(project) as c:
        cur = c.execute(
            "UPDATE file_leases SET released_at=? WHERE id=? AND released_at IS NULL",
            (now, lease_id),
        )
        if cur.rowcount == 0:
            r = c.execute("SELECT id FROM file_leases WHERE id=?", (lease_id,)).fetchone()
            if r:
                return {"error": "lease already released", "lease_id": lease_id}
            return {"error": "lease not found", "lease_id": lease_id}
    return {"released": True, "lease_id": lease_id}


def check_files(files: List[str], project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """For each file path, return its holder if held by an active lease. Files not held
    are omitted. [{file, held_by, task_id, expires_at}]."""
    now = time.time()
    file_set = set(files)
    results = []
    with _conn(project) as c:
        for lease in _active_leases_in(c, now):
            held = set(json.loads(lease["files"] or "[]"))
            for f in file_set & held:
                results.append({"file": f, "held_by": lease["agent_id"],
                                 "task_id": lease.get("task_id"),
                                 "expires_at": lease["claimed_at"] + lease["ttl_minutes"] * 60})
    return sorted(results, key=lambda x: x["file"])


def set_agent_state(task_id: str, agent_id: str, state: Dict[str, Any],
                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Upsert this agent's state blob inside the task's agent_state JSON map.
    Other agents' state keys are preserved. Returns the full merged agent_state."""
    with _conn(project) as c:
        row = c.execute("SELECT agent_state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return {"error": "task not found", "task_id": task_id}
        current = json.loads(row["agent_state"] or "{}") if row["agent_state"] else {}
        current[agent_id] = state
        c.execute("UPDATE tasks SET agent_state=?, updated_at=? WHERE task_id=?",
                  (json.dumps(current, sort_keys=True), time.time(), task_id))
    return current


def get_agent_state(task_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Return the full agent_state map for a task (all agents' state blobs)."""
    with _conn(project) as c:
        row = c.execute("SELECT agent_state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        return {"error": "task not found", "task_id": task_id}
    return json.loads(row["agent_state"] or "{}") if row["agent_state"] else {}


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


def list_active_leases(project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """All active leases board-wide (not released, not TTL-expired)."""
    now = time.time()
    with _conn(project) as c:
        leases = _active_leases_in(c, now)
    out = []
    for lease in leases:
        out.append({"lease_id": lease["id"], "agent_id": lease["agent_id"],
                    "task_id": lease.get("task_id"),
                    "files": json.loads(lease["files"] or "[]"),
                    "expires_at": lease["claimed_at"] + lease["ttl_minutes"] * 60})
    return sorted(out, key=lambda x: x["lease_id"])


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


def _project_env_suffix(project: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (project or "").upper()).strip("_")


def _project_hierarchy_contract(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    return {
        "scope": "project",
        "project_id": project,
        "authority_boundary": [
            "repo",
            "trust",
            "policy",
            "access",
            "ci",
            "model",
            "budget",
            "done",
        ],
        "children": {
            "boards_missions_deliverables": "outcome cockpits under the Project boundary",
            "epics_workstreams_tasks": "execution planning below boards/missions/deliverables",
        },
        "compatibility": {
            "current_switchboard_project_id": project,
            "project_arg_is_workspace_alias": True,
            "repo_topology_is_board_level_truth": False,
        },
    }


def _legacy_project_github_repo(project: str = DEFAULT_PROJECT) -> str:
    configured = (get_meta("github_repo", "", project=project) or "").strip()
    if configured:
        return configured
    suffix = _project_env_suffix(project)
    for key in (
        f"PM_GITHUB_REPO_{suffix}" if suffix else "",
        f"GITHUB_REPOSITORY_{suffix}" if suffix else "",
    ):
        if key and os.environ.get(key):
            return os.environ[key].strip()
    if project in BUILTIN_GITHUB_REPOS:
        return BUILTIN_GITHUB_REPOS[project]
    if project in (DEFAULT_PROJECT, "switchboard"):
        return (os.environ.get("PM_GITHUB_REPO") or os.environ.get("GITHUB_REPOSITORY") or "").strip()
    return ""


def get_project_github_repo(project: str = DEFAULT_PROJECT) -> str:
    """Canonical repository used for PR-state reconciliation on one board.

    New deployments should read get_project_repo_topology() for all repo roles. This
    compatibility helper still returns the canonical repo so older reconcile and webhook
    paths remain centered on the code-truth repository.
    """
    topology = get_project_repo_topology(project=project)
    return ((topology.get("roles") or {}).get("canonical") or {}).get("repo", "").strip()


def list_canonical_repos(projects: Optional[List[str]] = None) -> Dict[str, List[str]]:
    """Map each configured canonical repo -> the project ids that claim it as code truth.

    Registry-driven so anything that fans out per repo (the PR provenance/claim gate,
    future per-repo automation) automatically covers a new project the moment it sets a
    canonical repo via set_project_repo_topology — no per-repo allowlist to keep in sync.
    A shared repo (e.g. StevenRidder/Helm backs several Helm boards) appears once, mapping
    to all of its projects.
    """
    out: Dict[str, List[str]] = {}
    for pid in (projects if projects is not None else project_ids()):
        try:
            repo = get_project_github_repo(pid)
        except Exception:
            repo = ""
        repo = (repo or "").strip()
        if repo:
            out.setdefault(repo, []).append(pid)
    return out


def resolve_claim_gate_mode(repo: str, primary_repo: str = "",
                            primary_project: str = "switchboard") -> str:
    """Resolve claim-gate mode for a canonical GitHub repo from project repo_topology.

    Each project's ``roles.canonical.claim_gate`` declares off|warn|enforce for fleet
    PR provenance on that repo. The primary repo prefers the CI-home project's mode;
    other canonical repos use the owning project's declaration (default warn).
    """
    repo_norm = _normalize_repo_slug(repo)
    if not repo_norm:
        return DEFAULT_CLAIM_GATE_MODE
    primary_norm = _normalize_repo_slug(primary_repo)
    project_ids: List[str] = []
    for canonical_repo, pids in list_canonical_repos().items():
        if _normalize_repo_slug(canonical_repo) == repo_norm:
            project_ids.extend(pids)
    if not project_ids:
        return DEFAULT_CLAIM_GATE_MODE
    by_project: Dict[str, str] = {}
    for pid in project_ids:
        canonical = ((get_project_repo_topology(project=pid).get("roles") or {})
                     .get("canonical") or {})
        by_project[pid] = _normalize_claim_gate(canonical.get("claim_gate"))
    if primary_norm and repo_norm == primary_norm and primary_project in by_project:
        return by_project[primary_project]
    return next(iter(by_project.values()))


def get_project_repo_role(repo: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Classify one GitHub repo against a project's repo_topology roles."""
    repo_norm = _normalize_repo_slug(repo)
    topology = get_project_repo_topology(project=project)
    roles = topology.get("roles") or {}
    matches: List[Dict[str, Any]] = []
    for role, data in roles.items():
        role_repo = (data or {}).get("repo") or ""
        if repo_norm and _normalize_repo_slug(role_repo) == repo_norm:
            matches.append({
                "role": role,
                "repo": role_repo,
                "authority": list((data or {}).get("authority") or []),
                "default_branch": (data or {}).get("default_branch") or "",
            })
    selected = next((m for m in matches if m["role"] == "canonical"), None)
    selected = selected or (matches[0] if matches else {})
    role = selected.get("role") or "unknown"
    return {
        "project": project,
        "repo": repo,
        "normalized_repo": repo_norm,
        "matched": bool(matches),
        "role": role,
        "canonical": role == "canonical",
        "evidence_only": role in {"public_ci", "public", "release"},
        "authority": selected.get("authority") or [],
        "default_branch": selected.get("default_branch") or "",
        "matches": matches,
        "code_repo_gate": topology.get("code_repo_gate"),
    }


def _validate_github_repo(repo: str) -> Tuple[str, str]:
    clean = (repo or "").strip()
    if clean and not GITHUB_REPO_RE.match(clean):
        return clean, "github repo must be 'owner/name'"
    return clean, ""


def _normalize_session_policy_profile(profile: str) -> str:
    clean = re.sub(r"[^a-z0-9_-]+", "_", (profile or "").strip().lower()).strip("_")
    return SESSION_POLICY_PROFILE_ALIASES.get(clean, clean)


def _session_profile_text(task: Dict[str, Any]) -> str:
    return "\n".join(str(task.get(k) or "") for k in (
        "title", "description", "entry_criteria", "exit_criteria", "deliverable"))


def _project_session_policy_defaults(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    builtins = {
        "helm": {
            "default_profile": "docs_review",
            "code_task_default_profile": "code_strict",
            "notes": ["Helm code tasks default to code_strict; docs/review tasks may opt into docs_review or offline_evidence."],
        },
        "switchboard": {
            "default_profile": "docs_review",
            "code_task_default_profile": "docs_review",
            "notes": ["Switchboard exposes code_strict for code/control-plane tasks; tasks can opt in explicitly while legacy board fixtures remain docs_review by default."],
        },
    }
    default = copy.deepcopy(builtins.get(project) or {
        "default_profile": "docs_review",
        "code_task_default_profile": "docs_review",
        "notes": ["Projects can opt code-like tasks into code_strict by setting code_task_default_profile or a task-level policy_profile."],
    })
    raw = get_meta("session_policy_profiles", {}, project=project) or {}
    if isinstance(raw, dict):
        for key in ("default_profile", "code_task_default_profile"):
            if raw.get(key):
                default[key] = _normalize_session_policy_profile(str(raw.get(key) or ""))
    default["default_profile"] = _normalize_session_policy_profile(default.get("default_profile") or "docs_review")
    default["code_task_default_profile"] = _normalize_session_policy_profile(
        default.get("code_task_default_profile") or "code_strict")
    return default


def get_session_policy_profiles(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Named Work Session enforcement profiles for a project.

    These profiles are intentionally policy data, not hidden prompt convention. Adapters and
    humans can read the same contract before claiming, writing, completing, or merging work.
    """
    profiles = copy.deepcopy(BUILTIN_SESSION_POLICY_PROFILES)
    raw = get_meta("session_policy_profiles", {}, project=project) or {}
    if isinstance(raw, dict) and isinstance(raw.get("profiles"), dict):
        for name, data in raw.get("profiles", {}).items():
            normalized = _normalize_session_policy_profile(str(name))
            if not normalized or not isinstance(data, dict):
                continue
            base = copy.deepcopy(profiles.get(normalized) or {"profile": normalized})
            for key, value in data.items():
                if key in {"allowed_storage_modes", "deny_hygiene", "warn_hygiene", "completion_evidence"}:
                    base[key] = _coerce_str_list(value)
                else:
                    base[key] = value
            base["profile"] = normalized
            profiles[normalized] = base

    defaults = _project_session_policy_defaults(project)
    known = sorted(profiles)
    if defaults.get("default_profile") not in profiles:
        defaults["default_profile"] = "docs_review"
    if defaults.get("code_task_default_profile") not in profiles:
        defaults["code_task_default_profile"] = "code_strict"
    return {
        "schema": SESSION_POLICY_PROFILE_SCHEMA,
        "project": project,
        "defaults": defaults,
        "profiles": profiles,
        "known_profiles": known,
        "task_override_fields": [
            "agent_state.session_policy.profile",
            "agent_state.work_session.policy_profile",
            "policy_profile:<name> in task text",
            "session_profile:<name> in task text",
            "claim/pre_tool/complete evidence session_policy_profile",
        ],
    }


def _session_policy_profile_rules(profile: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    profiles = get_session_policy_profiles(project).get("profiles") or {}
    normalized = _normalize_session_policy_profile(profile)
    return copy.deepcopy(profiles.get(normalized) or {})


def _repo_role_template(role: str) -> Dict[str, Any]:
    authority = {
        "canonical": ["done", "merge_provenance", "code_truth"],
        "public_ci": ["verification_only"],
        "public": ["publish_evidence_only"],
        "release": ["release_evidence_only"],
    }.get(role, [])
    return {
        "repo": "",
        "default_branch": "",
        "authority": authority,
        "required_status_contexts": [],
        "sync_scripts": [],
        "publish_scripts": [],
        "configured": False,
    }


def _normalize_claim_gate(mode: Any) -> str:
    normalized = (str(mode or "") or DEFAULT_CLAIM_GATE_MODE).strip().lower()
    return normalized if normalized in CLAIM_GATE_MODES else DEFAULT_CLAIM_GATE_MODE


def _merge_repo_role(roles: Dict[str, Dict[str, Any]], role: str, data) -> None:
    if not isinstance(data, dict):
        return
    role = "public_ci" if role == "ci" else role
    target = roles.setdefault(role, _repo_role_template(role))
    for key, value in data.items():
        if key in {"required_status_contexts", "sync_scripts", "publish_scripts"}:
            merged = _coerce_str_list(value)
            if merged:
                target[key] = merged
        elif key == "claim_gate":
            target[key] = _normalize_claim_gate(value)
        elif value is not None:
            target[key] = value


def get_project_repo_topology(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Repository role contract for one Project authority boundary.

    The canonical role is the only code-truth / Done authority. Public CI,
    public mirror, and release roles are evidence-only carriers. Missing
    canonical repo is exposed as a blocked gate so code-work projects cannot
    silently claim merge provenance.
    """
    raw = get_meta("repo_topology", {}, project=project) or {}
    raw_error = ""
    if raw and not isinstance(raw, dict):
        raw_error = "repo_topology meta must be an object"
        raw = {}

    roles: Dict[str, Dict[str, Any]] = {
        "canonical": _repo_role_template("canonical"),
        "public_ci": _repo_role_template("public_ci"),
        "public": _repo_role_template("public"),
        "release": _repo_role_template("release"),
    }
    topology_type = "single_repo"
    built_in = copy.deepcopy(BUILTIN_REPO_TOPOLOGIES.get(project) or {})
    if built_in.get("topology_type"):
        topology_type = str(built_in.get("topology_type"))
    for role, data in (built_in.get("roles") or {}).items():
        _merge_repo_role(roles, role, data)

    if raw.get("topology_type"):
        topology_type = str(raw.get("topology_type")).strip() or topology_type
    if isinstance(raw.get("roles"), dict):
        for role, data in raw.get("roles", {}).items():
            _merge_repo_role(roles, str(role), data)

    flattened = {
        "canonical_repo": ("canonical", "repo"),
        "private_repo": ("canonical", "repo"),
        "canonical_default_branch": ("canonical", "default_branch"),
        "default_branch": ("canonical", "default_branch"),
        "canonical_claim_gate": ("canonical", "claim_gate"),
        "claim_gate": ("canonical", "claim_gate"),
        "public_ci_repo": ("public_ci", "repo"),
        "ci_repo": ("public_ci", "repo"),
        "public_ci_default_branch": ("public_ci", "default_branch"),
        "ci_default_branch": ("public_ci", "default_branch"),
        "public_ci_required_status_contexts": ("public_ci", "required_status_contexts"),
        "ci_required_status_contexts": ("public_ci", "required_status_contexts"),
        "required_status_contexts": ("public_ci", "required_status_contexts"),
        "public_ci_sync_scripts": ("public_ci", "sync_scripts"),
        "ci_sync_scripts": ("public_ci", "sync_scripts"),
        "sync_scripts": ("public_ci", "sync_scripts"),
        "public_repo": ("public", "repo"),
        "public_default_branch": ("public", "default_branch"),
        "public_publish_scripts": ("public", "publish_scripts"),
        "publish_scripts": ("public", "publish_scripts"),
        "release_repo": ("release", "repo"),
        "release_default_branch": ("release", "default_branch"),
        "release_publish_scripts": ("release", "publish_scripts"),
    }
    for key, (role, field) in flattened.items():
        if key in raw and raw.get(key) not in (None, ""):
            role_data = roles.setdefault(role, _repo_role_template(role))
            if field in {"required_status_contexts", "sync_scripts", "publish_scripts"}:
                role_data[field] = _coerce_str_list(raw.get(key))
            else:
                role_data[field] = str(raw.get(key)).strip()

    if not (roles.get("canonical") or {}).get("repo"):
        roles["canonical"]["repo"] = _legacy_project_github_repo(project)

    missing: List[str] = []
    warnings: List[str] = []
    invalid: List[Dict[str, str]] = []
    if raw_error:
        warnings.append(raw_error)
    for role, data in roles.items():
        for field in ("required_status_contexts", "sync_scripts", "publish_scripts"):
            data[field] = _coerce_str_list(data.get(field))
        if role == "canonical":
            data["claim_gate"] = _normalize_claim_gate(data.get("claim_gate"))
        repo, error = _validate_github_repo(data.get("repo", ""))
        data["repo"] = repo
        data["configured"] = bool(repo)
        if error:
            data["configured"] = False
            invalid.append({"role": role, "field": "repo", "error": error, "value": repo})
    if invalid:
        warnings.append("one or more repo roles have invalid owner/name values")
    if not roles["canonical"].get("configured"):
        missing.append("roles.canonical.repo")

    gate_passed = not missing and not any(item.get("role") == "canonical" for item in invalid)
    gate = {
        "name": "canonical_repo_configured",
        "passed": gate_passed,
        "status": "passed" if gate_passed else "blocked",
        "message": (
            "canonical repo configured; code Done must be proven from this repo"
            if gate_passed else
            "missing canonical repo; code-work Done cannot be proven by webhook/reconcile"
        ),
    }
    return {
        "schema": REPO_TOPOLOGY_SCHEMA,
        "scope": "project",
        "project": project,
        "project_hierarchy": _project_hierarchy_contract(project),
        "topology_type": topology_type,
        "roles": roles,
        "aliases": {"ci": "public_ci", "private": "canonical"},
        "authority": {
            "done": "canonical",
            "merge_provenance": "canonical",
            "ci_verification": "public_ci",
            "publication": "public",
            "release": "release",
        },
        "code_repo_gate": gate,
        "valid": gate_passed,
        "missing": missing,
        "invalid": invalid,
        "warnings": warnings,
        "notes": [
            "canonical repo is the only code-truth and Done authority",
            "public_ci/public/release repos are evidence roles and cannot mark code work Done",
        ],
    }


def set_project_github_repo(repo: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    repo, error = _validate_github_repo(repo)
    if error:
        return {"error": error, "repo": repo, "project": project}
    set_meta("github_repo", repo, project=project)
    topology = get_meta("repo_topology", {}, project=project) or {}
    if isinstance(topology, dict) and topology:
        roles = topology.setdefault("roles", {})
        canonical = roles.setdefault("canonical", {})
        canonical["repo"] = repo
        set_meta("repo_topology", topology, project=project)
    return {"project": project, "github_repo": repo,
            "repo_topology": get_project_repo_topology(project=project)}


def github_repo_reachable(repo: str) -> Optional[bool]:
    """Best-effort reachability probe for a repo (UI-15 Verify button, explicit only).

    True = the repo exists and we can see it; False = a definitive not-found/forbidden;
    None = the probe itself was inconclusive (offline, rate-limited, timeout). Uses the
    same optional token as reconcile so private canonical repos resolve when creds exist.
    """
    if not repo or "/" not in repo:
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}")
    req.add_header("Accept", "application/vnd.github+json")
    token = _github_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return (getattr(r, "status", None) or r.getcode()) == 200
    except Exception as exc:  # HTTPError carries .code; other errors are inconclusive
        code = getattr(exc, "code", 0) or 0
        if code in (401, 403, 404):
            return False
        return None


def set_project_repo_topology(project: str = DEFAULT_PROJECT, canonical_repo: str = "",
                              public_ci_repo: str = "", public_repo: str = "",
                              release_repo: str = "", topology_type: str = "",
                              canonical_default_branch: str = "",
                              canonical_claim_gate: str = "",
                              public_ci_required_status_contexts=None,
                              public_ci_sync_scripts=None,
                              public_publish_scripts=None,
                              release_publish_scripts=None,
                              ci_repo: str = "", ci_required_status_contexts=None,
                              ci_sync_scripts=None) -> Dict[str, Any]:
    if ci_repo and not public_ci_repo:
        public_ci_repo = ci_repo
    if ci_required_status_contexts and not public_ci_required_status_contexts:
        public_ci_required_status_contexts = ci_required_status_contexts
    if ci_sync_scripts and not public_ci_sync_scripts:
        public_ci_sync_scripts = ci_sync_scripts

    updates = {
        "canonical": {"repo": canonical_repo, "default_branch": canonical_default_branch,
                      "claim_gate": canonical_claim_gate},
        "public_ci": {"repo": public_ci_repo,
                      "required_status_contexts": public_ci_required_status_contexts,
                      "sync_scripts": public_ci_sync_scripts},
        "public": {"repo": public_repo, "publish_scripts": public_publish_scripts},
        "release": {"repo": release_repo, "publish_scripts": release_publish_scripts},
    }
    for role, data in updates.items():
        repo = (data.get("repo") or "").strip()
        if repo:
            _, error = _validate_github_repo(repo)
            if error:
                return {"error": error, "repo": repo, "role": role, "project": project}

    topology = get_meta("repo_topology", {}, project=project) or {}
    if not isinstance(topology, dict):
        topology = {}
    topology["schema"] = REPO_TOPOLOGY_SCHEMA
    if (topology_type or "").strip():
        topology["topology_type"] = topology_type.strip()
    roles = topology.setdefault("roles", {})
    for role, data in updates.items():
        target = roles.setdefault(role, {})
        repo = (data.get("repo") or "").strip()
        if repo:
            target["repo"] = repo
        default_branch = (data.get("default_branch") or "").strip()
        if default_branch:
            target["default_branch"] = default_branch
        claim_gate = (data.get("claim_gate") or "").strip()
        if claim_gate and role == "canonical":
            target["claim_gate"] = _normalize_claim_gate(claim_gate)
        for field in ("required_status_contexts", "sync_scripts", "publish_scripts"):
            values = _coerce_str_list(data.get(field))
            if values:
                target[field] = values
    set_meta("repo_topology", topology, project=project)
    canonical = ((topology.get("roles") or {}).get("canonical") or {}).get("repo", "").strip()
    if canonical:
        set_meta("github_repo", canonical, project=project)
    return {"project": project, "repo_topology": get_project_repo_topology(project=project)}


REPO_ROLE_LABELS = {
    "canonical": "Done / code truth",
    "public_ci": "CI verification only",
    "public": "Public mirror publication evidence only",
    "release": "Release evidence only",
}


def _repo_role_summary(role: str, data: Dict[str, Any]) -> Dict[str, Any]:
    data = data or {}
    repo = (data.get("repo") or "").strip()
    placeholder = (data.get("repo_placeholder") or "").strip()
    return {
        "role": role,
        "label": REPO_ROLE_LABELS.get(role, role),
        "repo": repo or placeholder or None,
        "configured": bool(data.get("configured")),
        "default_branch": data.get("default_branch") or "",
        "authority": list(data.get("authority") or []),
        "description": data.get("description") or "",
    }


def repo_topology_role_guide(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Operator/agent cheat sheet: which repo controls Done, CI, and publication."""
    topology = get_project_repo_topology(project)
    roles = topology.get("roles") or {}
    canonical = roles.get("canonical") or {}
    public_ci = roles.get("public_ci") or {}
    public = roles.get("public") or {}
    release = roles.get("release") or {}
    ci_message = "public_ci verifies canonical SHAs but is not code truth."
    if project == "helm":
        ci_message += " helm-ci is CI-only; canonical Done remains private Helm merge provenance."
    return {
        "project": project,
        "topology_type": topology.get("topology_type"),
        "done_authority": {
            "role": "canonical",
            "repo": (canonical.get("repo") or "").strip() or None,
            "default_branch": canonical.get("default_branch") or "",
            "message": "Only the canonical repo can mark code work Done via merge provenance.",
        },
        "ci_verification": {
            "role": "public_ci",
            "repo": ((public_ci.get("repo") or "").strip()
                     or (public_ci.get("repo_placeholder") or "").strip() or None),
            "default_branch": public_ci.get("default_branch") or "",
            "message": ci_message,
        },
        "publication_evidence": {
            "role": "public",
            "repo": ((public.get("repo") or "").strip()
                     or (public.get("repo_placeholder") or "").strip() or None),
            "default_branch": public.get("default_branch") or "",
            "message": "public mirror roles carry publish evidence only; they never prove code Done.",
        },
        "release_evidence": {
            "role": "release",
            "repo": (release.get("repo") or "").strip() or None,
            "message": "release roles carry release/packaging evidence only.",
        },
        "role_summaries": [
            _repo_role_summary(role, data)
            for role, data in (
                ("canonical", canonical),
                ("public_ci", public_ci),
                ("public", public),
                ("release", release),
            )
        ],
    }


def get_project_context(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    topology = get_project_repo_topology(project)
    access = project_access(project)
    boards = list_project_boards(project=project)
    hierarchy = topology.get("project_hierarchy") or _project_hierarchy_contract(project)
    return {
        "project": project,
        "project_label": next((p.get("label") for p in projects() if p["id"] == project), project),
        "project_boundary": access.get("boundary") or "",
        "project_purpose": access.get("purpose") or "",
        "project_hierarchy": hierarchy,
        "hierarchy_stack": [
            {"level": "project", "id": project, "label": hierarchy.get("project_id") or project},
            {"level": "board_or_mission",
             "note": hierarchy["children"]["boards_missions_deliverables"]},
            {"level": "epic_or_workstream",
             "note": hierarchy["children"]["epics_workstreams_tasks"]},
            {"level": "task", "note": "atomic execution unit with provenance and gates"},
        ],
        "repo_topology": topology,
        "repo_role_guide": repo_topology_role_guide(project),
        "session_policy_profiles": get_session_policy_profiles(project),
        "boards_missions": boards,
        "code_repo_gate": topology.get("code_repo_gate"),
    }


def _enrich_task_project_context(task: Dict[str, Any], project: str = DEFAULT_PROJECT) -> None:
    ctx = get_project_context(project)
    links = list_task_deliverable_links(task.get("task_id") or "", project=project)
    task["project_context"] = {
        "project": project,
        "project_hierarchy": ctx.get("project_hierarchy"),
        "hierarchy_breadcrumb": _task_hierarchy_breadcrumb(task, project, links=links),
        "repo_topology": ctx.get("repo_topology"),
        "repo_role_guide": ctx.get("repo_role_guide"),
        "session_policy_profiles": ctx.get("session_policy_profiles"),
        "boards_missions": ctx.get("boards_missions"),
        "deliverable_links": links,
        "code_repo_gate": ctx.get("code_repo_gate"),
    }


def create_project(name: str, project_id: str = "", label: str = "", pretitle: str = "",
                   actor: str = "system", seed_path: str = "",
                   github_repo: str = "", owner_principal_id: str = "",
                   org_id: str = DEFAULT_ORG_ID, purpose: str = "",
                   boundary: str = "", visibility: str = "") -> Dict[str, Any]:
    """Create a physically isolated project board and register it for routing.

    Dynamic projects mirror the built-ins: one row in the lightweight registry, one SQLite
    file for that board's actual task/activity state. The returned id is the value callers pass
    as project="..." to all normal board tools.
    """
    clean_name = (name or "").strip()
    pid = normalize_project_id(project_id or clean_name)
    if not clean_name and not pid:
        return {"error": "project name or project_id required"}
    if not PROJECT_ID_VALID_RE.match(pid):
        return {"error": "invalid project id; use 2-63 chars: lowercase letters, digits, '-' or '_'",
                "project_id": pid}
    repo, repo_error = _validate_github_repo(github_repo)
    if repo_error:
        return {"error": repo_error, "repo": repo, "project_id": pid}

    existing = _dynamic_projects().get(pid)
    if existing:
        if get_project_record(pid).get("is_protected"):
            return {"error": f"reserved protected project id: {pid}", "project_id": pid}
        init_db(pid)
        seed_if_empty(pid)
        if repo:
            set_meta("github_repo", repo, project=pid)
        current_access = project_access(pid)
        access = set_project_access(
            pid,
            org_id or current_access.get("org_id") or DEFAULT_ORG_ID,
            owner_user_id=owner_principal_id or current_access.get("owner_user_id") or "",
            purpose=purpose or current_access.get("purpose") or f"{pid} work control plane",
            boundary=boundary or current_access.get("boundary") or f"Only work belonging to project={pid} belongs here.",
            created_by=actor,
            visibility=visibility,
        )
        grant = {}
        if owner_principal_id:
            grant = grant_project_role(pid, "principal", owner_principal_id, "admin",
                                       created_by=actor)
        return {"created": False, "project": {"id": pid, "label": existing["label"],
                "pretitle": existing.get("pretitle", ""), "db": existing["db"],
                "seed": existing.get("seed"),
                "github_repo": get_project_github_repo(pid) or None,
                "repo_topology": get_project_repo_topology(pid),
                "access": access, "owner_grant": grant or None}}

    base_dir = os.environ.get("PM_DYNAMIC_PROJECTS_DIR") or os.path.dirname(PROJECT_REGISTRY_DB_PATH)
    os.makedirs(base_dir, exist_ok=True)
    db_path = os.path.join(base_dir, f"{pid}.db")
    project_label = (label or clean_name or pid).strip()
    project_pretitle = (pretitle or "").strip()
    seed = (seed_path or "").strip() or None
    now = time.time()

    init_project_registry()
    with _registry_conn() as c:
        c.execute(
            "INSERT INTO projects(id, label, pretitle, db_path, seed_path, created_at, created_by) "
            "VALUES (?,?,?,?,?,?,?)",
            (pid, project_label, project_pretitle, db_path, seed, now, actor),
        )
    bust_project_cache()  # read-your-write: the new project resolves immediately in this process
    try:
        init_db(pid)
        set_meta("project", project_label, project=pid)
        set_meta("people", DEFAULT_PEOPLE, project=pid)
        if project_pretitle:
            set_meta("pretitle", project_pretitle, project=pid)
        if repo:
            set_meta("github_repo", repo, project=pid)
        if seed:
            seed_if_empty(pid)
        access = set_project_access(
            pid,
            org_id or DEFAULT_ORG_ID,
            owner_user_id=owner_principal_id or "",
            purpose=purpose or f"{pid} work control plane",
            boundary=boundary or f"Only work belonging to project={pid} belongs here.",
            created_by=actor,
            visibility=visibility,
        )
        grant = {}
        if owner_principal_id:
            grant = grant_project_role(pid, "principal", owner_principal_id, "admin",
                                       created_by=actor)
    except Exception:
        with _registry_conn() as c:
            c.execute("DELETE FROM projects WHERE id=?", (pid,))
        raise

    return {"created": True, "project": {"id": pid, "label": project_label,
            "pretitle": project_pretitle, "db": db_path, "seed": seed,
            "github_repo": get_project_github_repo(pid) or None,
            "repo_topology": get_project_repo_topology(pid),
            "access": access, "owner_grant": grant or None}}


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


def _publication_reconcile_findings(tasks: List[Dict[str, Any]],
                                    git_states: Dict[str, Dict[str, Any]],
                                    project: str = DEFAULT_PROJECT) -> Tuple[
                                        List[Dict[str, Any]],
                                        Dict[str, Any],
                                    ]:
    findings: List[Dict[str, Any]] = []
    checked = 0
    stale = 0
    missing = 0
    with _conn(project) as c:
        for task in tasks:
            task_id = task["task_id"]
            state = git_states.get(task_id, {})
            source_sha = state.get("merged_sha") or state.get("head_sha") or ""
            summary = _task_publication_summary_in(c, task_id, source_sha=source_sha)
            checked += 1
            required = _publication_required_from(task, state.get("evidence") or {})
            if required and not summary.get("passed"):
                missing += 1
                findings.append({
                    "severity": "medium",
                    "task_id": task_id,
                    "code": "publication_evidence_missing",
                    "detail": (
                        "Task requires public mirror publication evidence, but no passed "
                        "publication record matches the current source SHA."
                    ),
                    "repo_role": "public",
                    "expected_source_sha": source_sha or None,
                    "failure_class": "missing_data",
                })
            if summary.get("status") != "stale":
                continue
            latest = summary.get("latest") or {}
            stale += 1
            findings.append({
                "severity": "medium",
                "task_id": task_id,
                "code": "publish_drift_stale_public_mirror",
                "detail": (
                    "Public mirror evidence is stale: latest publication points at "
                    f"{latest.get('source_sha') or 'unknown'} but current source SHA is "
                    f"{source_sha or 'unknown'}."
                ),
                "repo_role": "public",
                "public_repo": latest.get("public_repo") or "",
                "public_ref": latest.get("public_ref") or "",
                "latest_source_sha": latest.get("source_sha") or "",
                "expected_source_sha": source_sha or "",
                "failure_class": "stale_branch",
            })
    return findings, {
        "publication_evidence": "checked",
        "publication_tasks_checked": checked,
        "publication_missing_count": missing,
        "publication_stale_count": stale,
    }


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

