"""SQLite store for the taikun-pm satellite — tasks + activity, seeded from a
bundled plan snapshot. One file, zero ops (see ADR 0007). No shared DB touched."""
import json
import hashlib
import copy
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

import evidence_claims

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
from receipts_store import *   # noqa: F401,F403
from jobs_store import *       # noqa: F401,F403



def normalize_project_id(value: str) -> str:
    """Turn a human project name like 'Vulkan Renderer' into a stable project id."""
    slug = PROJECT_ID_SLUG_RE.sub("-", (value or "").strip().lower()).strip("-_")
    slug = re.sub(r"[-_]{2,}", "-", slug)
    return slug


def project_ids() -> List[str]:
    return list(_project_map())


def has_project(project: Optional[str]) -> bool:
    return (project or DEFAULT_PROJECT) in _project_map()


def projects() -> List[Dict[str, Any]]:
    """The switcher's source of truth — [{id, label, pretitle}]."""
    visible = (os.environ.get("PM_TOP_LEVEL_PROJECTS") or "").strip()
    allowed = {p.strip() for p in visible.split(",") if p.strip()} if visible else None
    out = []
    for k, v in _project_map().items():
        if allowed is not None and k not in allowed:
            continue
        access = project_access(k)
        out.append({
            "id": k,
            "label": v["label"],
            "pretitle": v.get("pretitle", ""),
            "purpose": access.get("purpose") or "",
            "boundary": access.get("boundary") or "",
            "owner_user_id": access.get("owner_user_id") or "",
            "org_id": access.get("org_id") or "",
        })
    return sorted(out, key=lambda p: p["id"])


def role_scopes(role: str) -> List[str]:
    return list(ROLE_SCOPES.get((role or "").strip().lower(), []))


def principal_scope_definitions() -> Dict[str, List[str]]:
    return {role: list(scopes) for role, scopes in sorted(ROLE_SCOPES.items())}


def validate_principal_kind(kind: str) -> str:
    normalized = (kind or "").strip().lower()
    return normalized if normalized in VALID_PRINCIPAL_KINDS else ""


def validate_principal_scopes(scopes: List[str]) -> Tuple[List[str], List[str]]:
    normalized = sorted({(scope or "").strip() for scope in scopes if (scope or "").strip()})
    unknown = [scope for scope in normalized if scope not in VALID_PRINCIPAL_SCOPES]
    return normalized, unknown


def resolve_principal_scopes(scopes: Any = None, role: str = "") -> Dict[str, Any]:
    """Resolve a role preset plus explicit scope list into a validated least-privilege set."""
    requested = coerce_csv_list(scopes)
    role_name = (role or "").strip().lower()
    if role_name:
        preset = role_scopes(role_name)
        if not preset:
            return {"error": f"unknown role: {role_name}"}
        requested.extend(preset)
    resolved, unknown = validate_principal_scopes(requested)
    if unknown:
        return {"error": "unknown scope(s): " + ", ".join(unknown)}
    if not resolved:
        return {"error": "at least one scope or known role is required"}
    return {"scopes": resolved, "role": role_name or None}


def ensure_org(org_id: str, name: str, slug: str = "", created_by: str = "system") -> Dict[str, Any]:
    init_project_registry()
    org_id = (org_id or DEFAULT_ORG_ID).strip()
    name = (name or org_id).strip()
    slug = normalize_project_id(slug or name)
    now = time.time()
    with _registry_conn() as c:
        c.execute(
            "INSERT INTO orgs(id, name, slug, created_at, created_by) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, slug=excluded.slug",
            (org_id, name, slug, now, created_by),
        )
        row = c.execute("SELECT * FROM orgs WHERE id=?", (org_id,)).fetchone()
    return dict(row)


def ensure_user(user_id: str, email: str = "", display_name: str = "",
                created_by: str = "system") -> Dict[str, Any]:
    init_project_registry()
    user_id = (user_id or "").strip()
    if not user_id:
        raise ValueError("user_id required")
    email = (email or "").strip().lower() or None
    display_name = (display_name or email or user_id).strip()
    now = time.time()
    with _registry_conn() as c:
        c.execute(
            "INSERT INTO users(id, email, display_name, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET email=COALESCE(excluded.email, users.email), "
            "display_name=excluded.display_name",
            (user_id, email, display_name, now),
        )
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row)


def add_org_member(org_id: str, user_id: str, role: str = "member",
                   created_by: str = "system") -> Dict[str, Any]:
    init_project_registry()
    role = (role or "member").strip().lower()
    now = time.time()
    with _registry_conn() as c:
        c.execute(
            "INSERT INTO org_memberships(org_id, user_id, role, created_at, created_by) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(org_id, user_id) DO UPDATE SET role=excluded.role",
            (org_id, user_id, role, now, created_by),
        )
        row = c.execute(
            "SELECT * FROM org_memberships WHERE org_id=? AND user_id=?",
            (org_id, user_id),
        ).fetchone()
    return dict(row)


def set_project_access(project_id: str, org_id: str, owner_user_id: str = "",
                       purpose: str = "", boundary: str = "",
                       created_by: str = "system", visibility: str = "") -> Dict[str, Any]:
    init_project_registry()
    if not has_project(project_id):
        return {"error": f"unknown project: {project_id}"}
    if not org_id:
        return {"error": "org_id required"}
    vis = (visibility or "").strip().lower()
    if vis and vis not in ("private", "org"):
        return {"error": "visibility must be 'private' or 'org'"}
    now = time.time()
    with _registry_conn() as c:
        c.execute(
            "INSERT INTO project_access(project_id, org_id, owner_user_id, purpose, boundary, "
            "created_at, created_by, updated_at, visibility) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(project_id) DO UPDATE SET org_id=excluded.org_id, "
            "owner_user_id=excluded.owner_user_id, purpose=excluded.purpose, "
            "boundary=excluded.boundary, updated_at=excluded.updated_at, "
            # only overwrite visibility when a new value was supplied — preserve on plain re-saves
            "visibility=COALESCE(excluded.visibility, project_access.visibility)",
            (project_id, org_id, owner_user_id or None, purpose or None, boundary or None,
             now, created_by, now, vis or None),
        )
        row = c.execute("SELECT * FROM project_access WHERE project_id=?",
                        (project_id,)).fetchone()
    return dict(row)


def project_access(project_id: str) -> Dict[str, Any]:
    init_project_registry()
    with _registry_conn() as c:
        row = c.execute("SELECT * FROM project_access WHERE project_id=?",
                        (project_id,)).fetchone()
    if row:
        return dict(row)
    if has_project(project_id):
        return {
            "project_id": project_id,
            "org_id": "",
            "owner_user_id": "",
            "purpose": f"{project_id} work control plane",
            "boundary": f"Only work belonging to project={project_id} belongs here.",
            "created_at": None,
            "created_by": None,
            "updated_at": None,
        }
    return {}


def grant_project_role(project_id: str, subject_kind: str, subject_id: str, role: str,
                       created_by: str = "system",
                       scopes: Optional[List[str]] = None) -> Dict[str, Any]:
    init_project_registry()
    if not has_project(project_id):
        return {"error": f"unknown project: {project_id}"}
    subject_kind = (subject_kind or "").strip().lower()
    subject_id = (subject_id or "").strip()
    role = (role or "").strip().lower()
    if subject_kind not in {"user", "principal", "agent", "system"}:
        return {"error": "subject_kind must be user, principal, agent, or system"}
    if not subject_id:
        return {"error": "subject_id required"}
    grant_scopes = scopes if scopes is not None else role_scopes(role)
    if not grant_scopes:
        return {"error": f"unknown role: {role}"}
    now = time.time()
    with _registry_conn() as c:
        c.execute(
            "INSERT INTO project_role_grants(project_id, subject_kind, subject_id, role, scopes, "
            "created_at, created_by, revoked_at) VALUES (?,?,?,?,?,?,?,NULL) "
            "ON CONFLICT(project_id, subject_kind, subject_id, role) DO UPDATE SET "
            "scopes=excluded.scopes, revoked_at=NULL, created_by=excluded.created_by",
            (project_id, subject_kind, subject_id, role,
             json.dumps(sorted(set(grant_scopes)), sort_keys=True), now, created_by),
        )
        row = c.execute(
            "SELECT * FROM project_role_grants WHERE project_id=? AND subject_kind=? "
            "AND subject_id=? AND role=?",
            (project_id, subject_kind, subject_id, role),
        ).fetchone()
    out = dict(row)
    out["scopes"] = json.loads(out.get("scopes") or "[]")
    return out


def list_project_role_grants(project_id: str, include_revoked: bool = False) -> List[Dict[str, Any]]:
    init_project_registry()
    q = "SELECT * FROM project_role_grants WHERE project_id=?"
    params: List[Any] = [project_id]
    if not include_revoked:
        q += " AND revoked_at IS NULL"
    q += " ORDER BY subject_kind, subject_id, role"
    with _registry_conn() as c:
        rows = c.execute(q, params).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["scopes"] = json.loads(item.get("scopes") or "[]")
        out.append(item)
    return out


def principal_project_roles(project_id: str, principal_id: str) -> List[Dict[str, Any]]:
    principal_id = (principal_id or "").strip()
    if not principal_id:
        return []
    grants = []
    for grant in list_project_role_grants(project_id):
        if grant["subject_id"] != principal_id:
            continue
        if grant["subject_kind"] in {"principal", "user"}:
            grants.append(grant)
    return grants


def effective_principal_scopes(project_id: str, principal_id: str,
                               base_scopes: Optional[List[str]] = None) -> List[str]:
    scopes = set(base_scopes or [])
    for grant in principal_project_roles(project_id, principal_id):
        scopes.update(grant.get("scopes") or [])
    return sorted(scopes)


def project_access_model(project_id: str, principal_id: str = "") -> Dict[str, Any]:
    return {
        "project": project_id,
        "access": project_access(project_id),
        "role_definitions": {role: list(scopes) for role, scopes in sorted(ROLE_SCOPES.items())},
        "grants": list_project_role_grants(project_id),
        "principal_roles": principal_project_roles(project_id, principal_id),
    }


def ensure_bootstrap_project_owner(project_id: str, principal_id: str, login: str,
                                   display_name: str, actor: str = "switchboard/auth") -> Dict[str, Any]:
    org = ensure_org(DEFAULT_ORG_ID, "6th Element Labs", slug="6th-element-labs", created_by=actor)
    user = ensure_user(principal_id, email=login if "@" in (login or "") else "",
                       display_name=display_name or login or principal_id, created_by=actor)
    membership = add_org_member(org["id"], user["id"], role="owner", created_by=actor)
    access = set_project_access(
        project_id,
        org["id"],
        owner_user_id=user["id"],
        purpose=f"{project_id} work control plane",
        boundary=f"Only work belonging to project={project_id} belongs here.",
        created_by=actor,
    )
    grant = grant_project_role(project_id, "principal", principal_id, "admin", created_by=actor)
    return {"org": org, "user": user, "membership": membership,
            "project_access": access, "grant": grant}


# Fields a PATCH may change (everything an editor touches in an Asana-style board).
EDITABLE = ["title", "description", "owner_org", "owner_person_or_role", "assignee",
            "phase", "status", "effort_days", "duration_days", "start_date",
            "finish_date", "risk_level", "is_blocking", "sort_order",
            "entry_criteria", "exit_criteria", "deliverable", "depends_on"]

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
RECONCILE_FAILURE_CLASS_BY_CODE = {
    "canonical_main_sha_not_found": "stale_branch",
    "claim_evidence_missing": "missing_data",
    "claim_without_evidence": "missing_data",
    "done_pr_not_merged": "hidden_fallback",
    "done_without_merged_sha": "hidden_fallback",
    "head_sha_not_found": "stale_branch",
    "merged_sha_mismatch": "invalid_input",
    "merged_sha_not_found": "stale_branch",
    "merged_sha_not_on_canonical_main": "stale_branch",
    "missing_canonical_main_sha": "missing_data",
    "publish_drift_stale_public_mirror": "stale_branch",
    "publication_evidence_missing": "missing_data",
    "progress_without_pushed_head": "missing_data",
    "pr_state_unavailable": "broken_connection",
    "review_without_provenance": "missing_data",
    "stale_file_lease": "failed_gate",
    "stale_resource_lease": "failed_gate",
    "stale_task_claim": "failed_gate",
}

# Plan-level sections that are not per-task (kept verbatim from the seed snapshot).

PROTOCOL_ENVELOPE = {
    "name": "switchboard",
    "version": "ixp.v1",
    "profile": "p0-dogfood",
    "profile_version": "2026-06-28",
    "profiles": {
        "ixp_core": "1.0",
        "txp_dispatch": "0.1",
        "oxp_tally": "0.1",
        "reconcile": "0.1",
    },
    "compatible_versions": ["ixp.v1"],
    "field_aliases": {
        "send_agent_message.ack_timeout_seconds": "ack_deadline_minutes",
        "send_agent_message.ack_timeout_s": "ack_deadline_minutes",
    },
}



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
    with _conn(project) as c:
        apply_schema(c)


def seed_if_empty(project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        return seed_from_plan(c, _resolve(project)["seed"])


def _task_row(r: sqlite3.Row) -> Dict[str, Any]:
    d = dict(r)
    d["depends_on"] = _normalize_depends_on(d.get("depends_on"))
    d["is_blocking"] = bool(d.get("is_blocking"))
    d["_wsId"] = d.pop("workstream_id")
    d["_wsName"] = d.pop("workstream_name")
    raw_state = d.pop("agent_state", None)
    d["agent_state"] = json.loads(raw_state) if raw_state else {}
    return d


def _dependency_state_in(c: sqlite3.Connection, task: Dict[str, Any]) -> Dict[str, Any]:
    deps = list(dict.fromkeys(task.get("depends_on") or []))
    by_id: Dict[str, Dict[str, Any]] = {}
    if deps:
        placeholders = ",".join("?" for _ in deps)
        rows = c.execute(
            f"SELECT task_id, title, status FROM tasks WHERE task_id IN ({placeholders})",
            deps,
        ).fetchall()
        by_id = {r["task_id"]: {"title": r["title"], "status": r["status"]} for r in rows}
    dependency_rows: List[Dict[str, Any]] = []
    for dep in deps:
        row = by_id.get(dep)
        status = row["status"] if row else "Missing"
        dependency_rows.append({
            "task_id": dep,
            "title": row["title"] if row else None,
            "status": status,
            "done": status == "Done",
            "missing": row is None,
        })
    blocking = [d for d in dependency_rows if not d["done"]]
    return {
        "dependencies": dependency_rows,
        "dependency_count": len(dependency_rows),
        "done": [d["task_id"] for d in dependency_rows if d["done"]],
        "blocking": blocking,
        "blocked_by_count": len(blocking),
        "missing": [d["task_id"] for d in dependency_rows if d["missing"]],
        "satisfied": not blocking,
        "ready": task.get("status") == "Not Started" and not blocking,
    }


STALE_DEPENDENCY_RATIONALE_RE = re.compile(
    r"\b(blocked|blocking|blocked\s+on|blocked\s+by|waiting\s+on\s+dependencies)\b",
    re.I,
)
DONE_STATUS_CONTRADICTION_RE = re.compile(
    r"\b(in\s+review|not\s+started|in\s+progress|blocked)\b",
    re.I,
)
EVIDENCE_HASH_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$", re.I)


def _rationale_state(rationale: str, task: Dict[str, Any],
                     dependency_state: Dict[str, Any]) -> Dict[str, Any]:
    text = rationale or ""
    lower = text.lower()
    flags: List[str] = []
    if (task.get("status") != "Blocked"
            and dependency_state.get("satisfied")
            and STALE_DEPENDENCY_RATIONALE_RE.search(text)
            and "not blocked" not in lower):
        flags.append("says_blocked_but_dependencies_satisfied")
    if task.get("status") == "Done" and DONE_STATUS_CONTRADICTION_RE.search(text):
        flags.append("mentions_pre_done_status_but_task_is_done")
    stale = bool(flags)
    state = {
        "stale": stale,
        "flags": flags,
        "message": (
            "Generated rationale may be stale; trust status, dependency_state, "
            "git_state, and provenance."
        ) if stale else None,
    }
    if stale:
        detail = FAIL_FIX_FAILURE_CLASSES["missing_data"]
        state["failure_class"] = "missing_data"
        state["expected_signal"] = detail["expected_signal"]
    return state


def _is_terminal_done_task(task: Dict[str, Any]) -> bool:
    return task.get("status") == "Done" and _has_done_provenance(task.get("git_state") or {})


def _apply_terminal_done_view(task: Dict[str, Any]) -> None:
    """Make task-detail reads authoritative after Done provenance lands.

    Working-state blobs, live registrations, and claims are useful while a task is moving.
    Once merge/offline provenance marks Done, those blobs become historical breadcrumbs. Keep
    enough signal for operators to debug drift, but do not expose stale derived fields as
    current scheduling truth.
    """
    if not _is_terminal_done_task(task):
        return
    provenance = task.get("provenance") or _provenance_summary(task.get("git_state") or {})
    stale_agent_state = task.get("agent_state") or {}
    stale_claims = task.get("active_claims") or []
    identity = task.get("identity") or {}
    suppressed: Dict[str, Any] = {}
    if stale_agent_state:
        suppressed["agent_state_agents"] = sorted(stale_agent_state.keys())
    if stale_claims:
        suppressed["active_claim_count"] = len(stale_claims)
        suppressed["active_claim_ids"] = [
            c.get("claim_id") for c in stale_claims if c.get("claim_id")
        ]
    if identity.get("active_agents"):
        suppressed["identity_active_agents"] = list(identity.get("active_agents") or [])
    task["terminal_state"] = {
        "terminal": True,
        "authority": "status_git_state_provenance",
        "provenance_type": provenance.get("type"),
        "message": (
            "Task is terminal Done. Consumers should trust status, git_state, and "
            "provenance over historical agent_state, active_claims, identity, or rationale."
        ),
    }
    if suppressed:
        task["terminal_state"]["suppressed_derived"] = suppressed
    task["agent_state"] = {}
    task["active_claims"] = []
    task["identity"] = {
        "active_agents": [],
        "recent_unbound_activity": identity.get("recent_unbound_activity") or [],
        "risk_window_seconds": identity.get("risk_window_seconds") or IDENTITY_RISK_WINDOW_S,
        "takeover_safe": True,
        "status": "terminal_done",
        "reason": "terminal_done_with_provenance",
        "message": (
            "Identity and takeover risk are closed because the task is already Done "
            "with recorded provenance."
        ),
    }


DELIVERABLE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{1,127}$")
PROJECT_BOARD_ID_RE = DELIVERABLE_ID_RE
PROJECT_BOARD_KINDS = {"board", "mission"}
PROJECT_BOARD_STATUSES = {"proposed", "active", "paused", "blocked", "done", "archived"}
DELIVERABLE_STATUSES = {
    "proposed", "approved", "in_progress", "blocked", "in_review", "done", "archived"
}
DELIVERABLE_MILESTONE_STATUSES = {
    "not_started", "in_progress", "blocked", "in_review", "done", "skipped"
}
BREAKDOWN_PROPOSAL_STATUSES = {"proposed", "approved", "rejected", "superseded", "deferred"}


def normalize_deliverable_id(value: str = "", title: str = "") -> str:
    """Normalize a human outcome name into a stable mission id."""
    raw = (value or "").strip()
    if raw:
        candidate = raw
    else:
        slug = normalize_project_id(title or "")
        candidate = f"deliverable-{slug}" if slug else f"deliverable-{uuid.uuid4().hex[:12]}"
    if not DELIVERABLE_ID_RE.match(candidate):
        raise ValueError(
            "deliverable id must be 2-128 chars and start with a letter; "
            "letters, digits, '_', '-', '.', and ':' are allowed"
        )
    return candidate


def normalize_project_board_id(value: str = "", title: str = "") -> str:
    raw = (value or "").strip()
    if raw:
        candidate = raw
    else:
        slug = normalize_project_id(title or "")
        candidate = f"mission-{slug}" if slug else f"mission-{uuid.uuid4().hex[:12]}"
    if not PROJECT_BOARD_ID_RE.match(candidate):
        raise ValueError(
            "board id must be 2-128 chars and start with a letter; "
            "letters, digits, '_', '-', '.', and ':' are allowed"
        )
    return candidate


def _deliverable_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for key in (
        "acceptance_criteria_json",
        "policy_constraints_json",
        "proof_requirements_json",
        "kpi_links_json",
        "metadata_json",
    ):
        out_key = key[:-5] if key.endswith("_json") else key
        d[out_key] = _json_payload(d.pop(key, ""))
    d["mission_id"] = d.get("board_id") or None
    return d


def _project_board_row(row: sqlite3.Row, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    d = dict(row)
    d["project_id"] = project
    d["mission_id"] = d.get("id")
    d["metadata"] = _json_payload(d.pop("metadata_json", ""))
    return d


def _deliverable_milestone_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for key in ("acceptance_criteria_json", "proof_requirements_json"):
        d[key[:-5]] = _json_payload(d.pop(key, ""))
    return d


def _deliverable_link_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["mission_id"] = d.get("board_id") or None
    d["blocks_deliverable"] = bool(d.get("blocks_deliverable"))
    d["proof_required"] = _json_payload(d.pop("proof_required_json", ""))
    d["metadata"] = _json_payload(d.pop("metadata_json", ""))
    return d


def _project_board_exists_in(c: sqlite3.Connection, board_id: str) -> bool:
    return bool(c.execute("SELECT 1 FROM project_boards WHERE id=?",
                          (board_id,)).fetchone())


def create_project_board(data: Dict[str, Any], actor: str = "user",
                         project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Create or update a first-class Board/Mission child under a Project."""
    if not has_project(project):
        return {"error": f"unknown project: {project}"}
    title = (data.get("title") or "").strip()
    if not title:
        return {"error": "title required"}
    try:
        board_id = normalize_project_board_id(
            data.get("id") or data.get("board_id") or data.get("mission_id"), title)
    except ValueError as exc:
        return {"error": str(exc)}
    kind = (data.get("kind") or "mission").strip().lower()
    if kind not in PROJECT_BOARD_KINDS:
        return {"error": "invalid board kind", "allowed": sorted(PROJECT_BOARD_KINDS)}
    status = (data.get("status") or "active").strip().lower()
    if status not in PROJECT_BOARD_STATUSES:
        return {"error": "invalid board status", "allowed": sorted(PROJECT_BOARD_STATUSES)}
    now = time.time()
    with _conn(project) as c:
        c.execute(
            """INSERT INTO project_boards
               (id, title, kind, status, owner_org, owner_person_or_role, purpose,
                end_state, description, metadata_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                kind=excluded.kind,
                status=excluded.status,
                owner_org=excluded.owner_org,
                owner_person_or_role=excluded.owner_person_or_role,
                purpose=excluded.purpose,
                end_state=excluded.end_state,
                description=excluded.description,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at""",
            (
                board_id, title, kind, status, data.get("owner_org"),
                data.get("owner_person_or_role"), data.get("purpose"),
                data.get("end_state"), data.get("description"),
                _json_object_field(data.get("metadata", data.get("metadata_json"))),
                now, now,
            ),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "project_board.upsert",
                   json.dumps({"project_id": project, "board_id": board_id, "kind": kind,
                               "title": title}, sort_keys=True), now))
    return get_project_board(board_id, project=project) or {"error": "board not found"}


def get_project_board(board_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    if not has_project(project):
        return None
    bid = (board_id or "").strip()
    if not bid:
        return None
    with _conn(project) as c:
        row = c.execute("SELECT * FROM project_boards WHERE id=?", (bid,)).fetchone()
    return _project_board_row(row, project=project) if row else None


def list_project_boards(project: str = DEFAULT_PROJECT, kind: str = "",
                        status: str = "") -> List[Dict[str, Any]]:
    if not has_project(project):
        return []
    clauses = []
    args: List[Any] = []
    if (kind or "").strip():
        clauses.append("kind=?")
        args.append(kind.strip().lower())
    if (status or "").strip():
        clauses.append("status=?")
        args.append(status.strip().lower())
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _conn(project) as c:
        rows = c.execute(
            f"SELECT * FROM project_boards{where} ORDER BY updated_at DESC, id",
            args,
        ).fetchall()
    return [_project_board_row(row, project=project) for row in rows]


def _deliverable_exists_in(c: sqlite3.Connection, deliverable_id: str) -> bool:
    return bool(c.execute("SELECT 1 FROM deliverables WHERE id=?",
                          (deliverable_id,)).fetchone())


def _deliverable_milestone_exists_in(
        c: sqlite3.Connection, deliverable_id: str, milestone_id: str) -> bool:
    return bool(c.execute(
        "SELECT 1 FROM deliverable_milestones WHERE id=? AND deliverable_id=?",
        (milestone_id, deliverable_id),
    ).fetchone())


def create_deliverable(data: Dict[str, Any], actor: str = "user",
                       project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Create or update a project-owned product outcome/mission record."""
    if not has_project(project):
        return {"error": f"unknown project: {project}"}
    title = (data.get("title") or "").strip()
    if not title:
        return {"error": "title required"}
    try:
        deliverable_id = normalize_deliverable_id(data.get("id") or data.get("deliverable_id"), title)
    except ValueError as exc:
        return {"error": str(exc)}
    status = (data.get("status") or "proposed").strip().lower()
    if status not in DELIVERABLE_STATUSES:
        return {"error": "invalid status", "allowed": sorted(DELIVERABLE_STATUSES)}
    confidence = data.get("confidence")
    if confidence in ("", None):
        confidence_value = None
    else:
        try:
            confidence_value = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            return {"error": "confidence must be a number between 0 and 1"}
    board_id = (data.get("board_id") or data.get("mission_id") or "").strip() or None
    now = time.time()
    with _conn(project) as c:
        if board_id and not _project_board_exists_in(c, board_id):
            return {"error": "unknown board", "board_id": board_id, "project_id": project}
        c.execute(
            """INSERT INTO deliverables
               (id, board_id, title, status, owner_org, owner_person_or_role, end_state,
                why_it_matters, confidence, acceptance_criteria_json,
                policy_constraints_json, proof_requirements_json, kpi_links_json,
                metadata_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                board_id=COALESCE(excluded.board_id, deliverables.board_id),
                title=excluded.title,
                status=excluded.status,
                owner_org=excluded.owner_org,
                owner_person_or_role=excluded.owner_person_or_role,
                end_state=excluded.end_state,
                why_it_matters=excluded.why_it_matters,
                confidence=excluded.confidence,
                acceptance_criteria_json=excluded.acceptance_criteria_json,
                policy_constraints_json=excluded.policy_constraints_json,
                proof_requirements_json=excluded.proof_requirements_json,
                kpi_links_json=excluded.kpi_links_json,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at""",
            (
                deliverable_id, board_id, title, status, data.get("owner_org"),
                data.get("owner_person_or_role"), data.get("end_state"),
                data.get("why_it_matters"), confidence_value,
                _json_list_field(data.get("acceptance_criteria")),
                _json_object_field(data.get("policy_constraints")),
                _json_object_field(data.get("proof_requirements")),
                _json_list_field(data.get("kpi_links")),
                _json_object_field(data.get("metadata", data.get("metadata_json"))),
                now, now,
            ),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.upsert",
                   json.dumps({"deliverable_id": deliverable_id, "board_id": board_id,
                               "title": title},
                              sort_keys=True), now))
    return get_deliverable(deliverable_id, project=project) or {"error": "deliverable not found"}


def add_deliverable_milestone(deliverable_id: str, data: Dict[str, Any],
                              actor: str = "user",
                              project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    if not has_project(project):
        return {"error": f"unknown project: {project}"}
    title = (data.get("title") or "").strip()
    if not title:
        return {"error": "title required"}
    try:
        raw_mid = data.get("id") or data.get("milestone_id")
        if raw_mid:
            mid = normalize_deliverable_id(raw_mid, title)
        else:
            mid = normalize_deliverable_id(
                f"{deliverable_id}:{normalize_project_id(title)}", title)
    except ValueError as exc:
        return {"error": str(exc)}
    status = (data.get("status") or "not_started").strip().lower()
    if status not in DELIVERABLE_MILESTONE_STATUSES:
        return {"error": "invalid milestone status",
                "allowed": sorted(DELIVERABLE_MILESTONE_STATUSES)}
    now = time.time()
    with _conn(project) as c:
        if not _deliverable_exists_in(c, deliverable_id):
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
        order = data.get("sort_order")
        if order in ("", None):
            order = c.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 "
                "FROM deliverable_milestones WHERE deliverable_id=?",
                (deliverable_id,),
            ).fetchone()[0]
        c.execute(
            """INSERT INTO deliverable_milestones
               (id, deliverable_id, title, description, status, sort_order,
                acceptance_criteria_json, proof_requirements_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                description=excluded.description,
                status=excluded.status,
                sort_order=excluded.sort_order,
                acceptance_criteria_json=excluded.acceptance_criteria_json,
                proof_requirements_json=excluded.proof_requirements_json,
                updated_at=excluded.updated_at""",
            (
                mid, deliverable_id, title, data.get("description"), status, int(order),
                _json_list_field(data.get("acceptance_criteria")),
                _json_object_field(data.get("proof_requirements")),
                now, now,
            ),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.milestone_upsert",
                   json.dumps({"deliverable_id": deliverable_id, "milestone_id": mid,
                               "title": title}, sort_keys=True), now))
    return get_deliverable(deliverable_id, project=project) or {"error": "deliverable not found"}


def link_task_to_deliverable(deliverable_id: str, task_project: str, task_id: str,
                             milestone_id: str = "", data: Optional[Dict[str, Any]] = None,
                             actor: str = "user",
                             project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Link an explicitly routed board task to a deliverable without moving or editing it."""
    if not has_project(project):
        return {"error": f"unknown project: {project}"}
    task_project = (task_project or "").strip()
    task_id = (task_id or "").strip().upper()
    if not has_project(task_project):
        return {"error": f"unknown linked project: {task_project}"}
    target = get_task(task_id, project=task_project)
    if not target:
        return {"error": "unknown linked task", "project_id": task_project, "task_id": task_id}
    payload = data or {}
    requested_board_id = (payload.get("board_id") or payload.get("mission_id") or "").strip() or None
    link_id = (payload.get("id") or payload.get("link_id") or
               f"link-{deliverable_id}-{task_project}-{task_id}")
    role = (payload.get("role") or "").strip()
    if not role or role.lower() == "auto":
        # Auto-classify when the caller doesn't pick a role: a task that is
        # already Done at link time cannot be future flow work for this
        # deliverable — it is groundwork, so it lands in the mission map's
        # context row ('foundation') instead of cluttering the execution DAG.
        # mission_graph still promotes it into the graph if a flow task
        # depends_on it, and an explicit role always wins over this default.
        role = "foundation" if (target.get("status") or "").strip() == "Done" else "contributes"
    now = time.time()
    with _conn(project) as c:
        if not _deliverable_exists_in(c, deliverable_id):
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
        deliverable_row = c.execute("SELECT board_id FROM deliverables WHERE id=?",
                                    (deliverable_id,)).fetchone()
        deliverable_board_id = (deliverable_row["board_id"] if deliverable_row else "") or None
        if requested_board_id and not _project_board_exists_in(c, requested_board_id):
            return {"error": "unknown board", "board_id": requested_board_id,
                    "project_id": project}
        if requested_board_id and deliverable_board_id and requested_board_id != deliverable_board_id:
            return {"error": "board mismatch", "board_id": requested_board_id,
                    "deliverable_board_id": deliverable_board_id,
                    "deliverable_id": deliverable_id}
        board_id = requested_board_id or deliverable_board_id
        mid = (milestone_id or payload.get("milestone_id") or "").strip() or None
        if mid and not _deliverable_milestone_exists_in(c, deliverable_id, mid):
            return {"error": "unknown milestone", "deliverable_id": deliverable_id,
                    "milestone_id": mid}
        c.execute(
            """INSERT INTO deliverable_task_links
               (id, deliverable_id, board_id, milestone_id, project_id, task_id, role,
                blocks_deliverable, proof_required_json, metadata_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(deliverable_id, project_id, task_id) DO UPDATE SET
                board_id=excluded.board_id,
                milestone_id=excluded.milestone_id,
                role=excluded.role,
                blocks_deliverable=excluded.blocks_deliverable,
                proof_required_json=excluded.proof_required_json,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at""",
            (
                link_id, deliverable_id, board_id, mid, task_project, task_id, role,
                1 if payload.get("blocks_deliverable") else 0,
                _json_object_field(payload.get("proof_required")),
                _json_object_field(payload.get("metadata", payload.get("metadata_json"))),
                now, now,
            ),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.task_linked",
                   json.dumps({"deliverable_id": deliverable_id, "board_id": board_id,
                               "project_id": task_project,
                               "task_id": task_id, "milestone_id": mid},
                              sort_keys=True), now))
    return get_deliverable(deliverable_id, project=project) or {"error": "deliverable not found"}


def _decorate_deliverable_task_link(link: Dict[str, Any]) -> Dict[str, Any]:
    if not has_project(link.get("project_id")):
        link["task"] = {"error": "unknown project", "project_id": link.get("project_id")}
        return link
    task = get_task(link["task_id"], project=link["project_id"])
    if not task:
        link["task"] = {"error": "unknown task", "project_id": link["project_id"],
                        "task_id": link["task_id"]}
        return link
    link["task"] = {
        "task_id": task["task_id"],
        "title": task.get("title"),
        "status": task.get("status"),
        "workstream": task.get("_wsId"),
        "provenance": task.get("provenance"),
        "external_ci": task.get("external_ci"),
        "publication": task.get("publication"),
    }
    return link


def get_deliverable(deliverable_id: str, project: str = DEFAULT_PROJECT,
                    include_task_snapshots: bool = True) -> Optional[Dict[str, Any]]:
    if not has_project(project):
        return None
    with _conn(project) as c:
        row = c.execute("SELECT * FROM deliverables WHERE id=?",
                        (deliverable_id,)).fetchone()
        if not row:
            return None
        deliverable = _deliverable_row(row)
        if deliverable.get("board_id"):
            board_row = c.execute("SELECT * FROM project_boards WHERE id=?",
                                  (deliverable["board_id"],)).fetchone()
            deliverable["board"] = (_project_board_row(board_row, project=project)
                                    if board_row else {"error": "unknown board",
                                                       "board_id": deliverable["board_id"],
                                                       "project_id": project})
        milestones = [
            _deliverable_milestone_row(r)
            for r in c.execute(
                "SELECT * FROM deliverable_milestones WHERE deliverable_id=? "
                "ORDER BY sort_order, created_at, id",
                (deliverable_id,),
            ).fetchall()
        ]
        links = [
            _deliverable_link_row(r)
            for r in c.execute(
                "SELECT * FROM deliverable_task_links WHERE deliverable_id=? "
                "ORDER BY created_at, id",
                (deliverable_id,),
            ).fetchall()
        ]
    if include_task_snapshots:
        links = [_decorate_deliverable_task_link(link) for link in links]
    deliverable["milestones"] = milestones
    deliverable["task_links"] = links
    deliverable["progress"] = deliverable_progress(deliverable)
    return deliverable


def list_deliverables(project: str = DEFAULT_PROJECT, board_id: str = "") -> List[Dict[str, Any]]:
    if not has_project(project):
        return []
    board_id = (board_id or "").strip()
    with _conn(project) as c:
        if board_id:
            if not _project_board_exists_in(c, board_id):
                return []
            rows = c.execute(
                "SELECT id FROM deliverables WHERE board_id=? ORDER BY updated_at DESC, id",
                (board_id,),
            ).fetchall()
        else:
            rows = c.execute("SELECT id FROM deliverables ORDER BY updated_at DESC, id").fetchall()
    return [d for d in (get_deliverable(r["id"], project=project) for r in rows) if d]


def deliverable_progress(deliverable: Dict[str, Any]) -> Dict[str, Any]:
    links = deliverable.get("task_links") or []
    status_counts: Dict[str, int] = {}
    done = in_review = blocked = 0
    external_ci_required = external_ci_passed = external_ci_blocked = 0
    publication_required = publication_passed = publication_blocked = 0
    for link in links:
        task = link.get("task") or {}
        status = task.get("status") or "Unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
        if status == "Done" and ((task.get("provenance") or {}).get("terminal")):
            done += 1
        elif status == "In Review":
            in_review += 1
        elif status == "Blocked":
            blocked += 1
        proof_required = link.get("proof_required") or {}
        external_ci = task.get("external_ci") or {}
        gate = external_ci.get("gate") or {}
        if proof_required.get("external_ci_passed") or gate.get("required"):
            external_ci_required += 1
            if external_ci.get("passed"):
                external_ci_passed += 1
            else:
                external_ci_blocked += 1
        publication = task.get("publication") or {}
        publication_gate = publication.get("gate") or {}
        if (proof_required.get("publication_evidence")
                or proof_required.get("public_mirror_published")
                or proof_required.get("publish_evidence")
                or publication_gate.get("required")):
            publication_required += 1
            if publication.get("passed"):
                publication_passed += 1
            else:
                publication_blocked += 1
    total = len(links)
    return {
        "linked_task_count": total,
        "done_with_proof_count": done,
        "in_review_count": in_review,
        "blocked_count": blocked,
        "external_ci_required_count": external_ci_required,
        "external_ci_passed_count": external_ci_passed,
        "external_ci_blocked_count": external_ci_blocked,
        "publication_required_count": publication_required,
        "publication_passed_count": publication_passed,
        "publication_blocked_count": publication_blocked,
        "status_counts": dict(sorted(status_counts.items())),
        "done_with_proof_ratio": (done / total) if total else 0.0,
    }


def _breakdown_proposal_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["payload"] = _json_payload(d.pop("payload_json", ""))
    return d


def _validate_breakdown_task_spec(milestone_idx: int, task_idx: int,
                                  task: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    task_project = (task.get("project_id") or task.get("task_project") or "").strip()
    if not task_project:
        return None, f"milestones[{milestone_idx}].tasks[{task_idx}] requires project_id"
    if not has_project(task_project):
        return None, f"unknown linked project: {task_project}"
    action = (task.get("action") or "create").strip().lower()
    if action == "link":
        task_id = (task.get("task_id") or "").strip().upper()
        if not task_id:
            return None, f"milestones[{milestone_idx}].tasks[{task_idx}] link requires task_id"
        if not get_task(task_id, project=task_project):
            return None, (
                f"unknown linked task {task_id} on project {task_project}"
            )
        return dict(task, action="link", project_id=task_project, task_id=task_id), None
    workstream_id = (task.get("workstream_id") or "").strip()
    task_title = (task.get("title") or "").strip()
    if not workstream_id or not task_title:
        return None, (
            f"milestones[{milestone_idx}].tasks[{task_idx}] create requires "
            "workstream_id and title"
        )
    return dict(task, action="create", project_id=task_project,
                workstream_id=workstream_id, title=task_title), None


def _validate_breakdown_payload(payload: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    parsed = _parse_jsonish(payload)
    if not isinstance(parsed, dict):
        return None, "breakdown payload must be a JSON object"
    milestones = parsed.get("milestones")
    if isinstance(milestones, str):
        milestones = _parse_jsonish(milestones)
    if not isinstance(milestones, list) or not milestones:
        return None, "breakdown payload requires a non-empty milestones array"
    normalized: List[Dict[str, Any]] = []
    for idx, milestone in enumerate(milestones):
        if not isinstance(milestone, dict):
            return None, f"milestones[{idx}] must be an object"
        title = (milestone.get("title") or "").strip()
        if not title:
            return None, f"milestones[{idx}] requires title"
        tasks = milestone.get("tasks") or []
        if tasks and not isinstance(tasks, list):
            return None, f"milestones[{idx}].tasks must be an array"
        normalized_tasks: List[Dict[str, Any]] = []
        for t_idx, task in enumerate(tasks):
            if not isinstance(task, dict):
                return None, f"milestones[{idx}].tasks[{t_idx}] must be an object"
            normalized_task, err = _validate_breakdown_task_spec(idx, t_idx, task)
            if err:
                return None, err
            normalized_tasks.append(normalized_task)
        normalized.append({
            "id": (milestone.get("id") or "").strip() or None,
            "title": title,
            "description": milestone.get("description"),
            "status": (milestone.get("status") or "not_started").strip().lower(),
            "sort_order": milestone.get("sort_order"),
            "acceptance_criteria": milestone.get("acceptance_criteria") or [],
            "proof_requirements": milestone.get("proof_requirements") or {},
            "tasks": normalized_tasks,
        })
    target_projects = parsed.get("target_projects") or []
    if isinstance(target_projects, str):
        target_projects = _parse_jsonish(target_projects)
    if target_projects and not isinstance(target_projects, list):
        return None, "target_projects must be an array"
    for tp_idx, target in enumerate(target_projects or []):
        if isinstance(target, str):
            if not has_project(target.strip()):
                return None, f"unknown target project: {target.strip()}"
            continue
        if not isinstance(target, dict):
            return None, f"target_projects[{tp_idx}] must be an object or project id string"
        pid = (target.get("project_id") or target.get("project") or "").strip()
        if not pid or not has_project(pid):
            return None, f"unknown target project: {pid or target}"
    return {
        "schema": parsed.get("schema") or "switchboard.deliverable_breakdown_draft.v1",
        "outcome": parsed.get("outcome"),
        "target_projects": target_projects or [],
        "policy_constraints": parsed.get("policy_constraints") or {},
        "acceptance_criteria": parsed.get("acceptance_criteria") or [],
        "milestones": normalized,
        "notes": parsed.get("notes"),
        "generation": parsed.get("generation") or {},
    }, None


def unlink_task_from_deliverable(deliverable_id: str, task_project: str, task_id: str,
                                 actor: str = "user",
                                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Remove a cross-project task link from a deliverable without mutating the task."""
    if not has_project(project):
        return {"error": f"unknown project: {project}"}
    task_project = (task_project or "").strip()
    task_id = (task_id or "").strip().upper()
    if not task_project or not task_id:
        return {"error": "task_project and task_id are required"}
    now = time.time()
    with _conn(project) as c:
        if not _deliverable_exists_in(c, deliverable_id):
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
        row = c.execute(
            "SELECT id FROM deliverable_task_links "
            "WHERE deliverable_id=? AND project_id=? AND task_id=?",
            (deliverable_id, task_project, task_id),
        ).fetchone()
        if not row:
            return {"error": "unknown task link", "deliverable_id": deliverable_id,
                    "project_id": task_project, "task_id": task_id}
        c.execute(
            "DELETE FROM deliverable_task_links "
            "WHERE deliverable_id=? AND project_id=? AND task_id=?",
            (deliverable_id, task_project, task_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.task_unlinked",
                   json.dumps({"deliverable_id": deliverable_id, "project_id": task_project,
                               "task_id": task_id}, sort_keys=True), now))
    return get_deliverable(deliverable_id, project=project) or {"error": "deliverable not found"}


def update_mission_narrative(deliverable_id: str, narrative: str, actor: str = "user",
                             project: str = DEFAULT_PROJECT,
                             append: bool = False) -> Dict[str, Any]:
    """Store or append the operator-facing mission narrative on a deliverable."""
    if not has_project(project):
        return {"error": f"unknown project: {project}"}
    text = (narrative or "").strip()
    if not text:
        return {"error": "narrative is required"}
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT metadata_json FROM deliverables WHERE id=?",
                        (deliverable_id,)).fetchone()
        if not row:
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
        metadata = _json_payload(row["metadata_json"])
        if append and metadata.get("narrative"):
            metadata["narrative"] = f"{metadata['narrative'].rstrip()}\n\n{text}"
        else:
            metadata["narrative"] = text
        metadata["narrative_updated_at"] = now
        metadata["narrative_updated_by"] = actor
        metadata["narrative_source"] = "manual"
        c.execute(
            "UPDATE deliverables SET metadata_json=?, updated_at=? WHERE id=?",
            (json.dumps(metadata, sort_keys=True), now, deliverable_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.narrative_updated",
                   json.dumps({"deliverable_id": deliverable_id}, sort_keys=True), now))
    return get_deliverable(deliverable_id, project=project) or {"error": "deliverable not found"}


def propose_deliverable_breakdown(deliverable_id: str, payload: Any, actor: str = "user",
                                  project: str = DEFAULT_PROJECT,
                                  proposal_id: str = "",
                                  outcome_text: str = "") -> Dict[str, Any]:
    """Store a milestone/task breakdown proposal without creating board tasks."""
    if not has_project(project):
        return {"error": f"unknown project: {project}"}
    normalized, err = _validate_breakdown_payload(payload)
    if err:
        return {"error": err}
    pid = (proposal_id or "").strip() or f"proposal-{deliverable_id}-{uuid.uuid4().hex[:10]}"
    outcome = (outcome_text or normalized.get("outcome") or "").strip() or None
    now = time.time()
    with _conn(project) as c:
        if not _deliverable_exists_in(c, deliverable_id):
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
        c.execute(
            "UPDATE deliverable_breakdown_proposals SET status='superseded', updated_at=? "
            "WHERE deliverable_id=? AND status='proposed'",
            (now, deliverable_id),
        )
        c.execute(
            """INSERT INTO deliverable_breakdown_proposals
               (id, deliverable_id, status, proposed_by, approved_by, reviewed_by,
                outcome_text, review_reason, deferred_until, payload_json,
                created_at, updated_at, approved_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, deliverable_id, "proposed", actor, None, None, outcome, None, None,
             json.dumps(normalized, sort_keys=True), now, now, None),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.breakdown_proposed",
                   json.dumps({"deliverable_id": deliverable_id, "proposal_id": pid,
                               "outcome": outcome}, sort_keys=True), now))
    return get_deliverable_breakdown_proposal(pid, project=project) or {
        "error": "proposal not found", "proposal_id": pid}


def get_deliverable_breakdown_proposal(proposal_id: str,
                                       project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    if not has_project(project):
        return {"error": f"unknown project: {project}"}
    proposal_id = (proposal_id or "").strip()
    if not proposal_id:
        return {"error": "proposal_id is required"}
    with _conn(project) as c:
        row = c.execute("SELECT * FROM deliverable_breakdown_proposals WHERE id=?",
                        (proposal_id,)).fetchone()
    if not row:
        return None
    proposal = _breakdown_proposal_row(row)
    return {
        "schema": "switchboard.deliverable_breakdown_proposal.v1",
        "project_id": project,
        "deliverable_id": proposal["deliverable_id"],
        "proposal": proposal,
        "deliverable": get_deliverable(proposal["deliverable_id"], project=project),
        "tasks_created": proposal.get("status") == "approved",
    }


def list_deliverable_breakdown_proposals(deliverable_id: str = "",
                                         project: str = DEFAULT_PROJECT,
                                         status: str = "") -> List[Dict[str, Any]]:
    if not has_project(project):
        return []
    deliverable_id = (deliverable_id or "").strip()
    status = (status or "").strip().lower()
    query = "SELECT * FROM deliverable_breakdown_proposals WHERE 1=1"
    params: List[Any] = []
    if deliverable_id:
        query += " AND deliverable_id=?"
        params.append(deliverable_id)
    if status:
        if status not in BREAKDOWN_PROPOSAL_STATUSES:
            return []
        query += " AND status=?"
        params.append(status)
    query += " ORDER BY updated_at DESC, created_at DESC, id"
    with _conn(project) as c:
        rows = c.execute(query, params).fetchall()
    return [_breakdown_proposal_row(r) for r in rows]


def update_deliverable_breakdown_proposal(proposal_id: str, payload: Any,
                                          actor: str = "user",
                                          project: str = DEFAULT_PROJECT,
                                          outcome_text: str = "") -> Dict[str, Any]:
    """Edit a pending breakdown proposal before approval."""
    if not has_project(project):
        return {"error": f"unknown project: {project}"}
    proposal_id = (proposal_id or "").strip()
    if not proposal_id:
        return {"error": "proposal_id is required"}
    normalized, err = _validate_breakdown_payload(payload)
    if err:
        return {"error": err}
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM deliverable_breakdown_proposals WHERE id=?",
                        (proposal_id,)).fetchone()
        if not row:
            return {"error": "unknown proposal", "proposal_id": proposal_id}
        if row["status"] != "proposed":
            return {"error": "only proposed breakdowns can be edited",
                    "proposal_id": proposal_id, "status": row["status"]}
        outcome = (outcome_text or normalized.get("outcome") or row["outcome_text"] or "").strip()
        c.execute(
            "UPDATE deliverable_breakdown_proposals "
            "SET payload_json=?, outcome_text=?, updated_at=? WHERE id=?",
            (json.dumps(normalized, sort_keys=True), outcome or None, now, proposal_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.breakdown_updated",
                   json.dumps({"proposal_id": proposal_id,
                               "deliverable_id": row["deliverable_id"]}, sort_keys=True), now))
    return get_deliverable_breakdown_proposal(proposal_id, project=project) or {
        "error": "proposal not found", "proposal_id": proposal_id}


def _finalize_breakdown_review(proposal_id: str, status: str, actor: str,
                               project: str, reason: str = "",
                               deferred_until: Optional[float] = None) -> Dict[str, Any]:
    if status not in BREAKDOWN_PROPOSAL_STATUSES:
        return {"error": "invalid proposal status", "allowed": sorted(BREAKDOWN_PROPOSAL_STATUSES)}
    reason = (reason or "").strip()
    if status in ("rejected", "deferred") and not reason:
        return {"error": f"{status} requires reason"}
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM deliverable_breakdown_proposals WHERE id=?",
                        (proposal_id,)).fetchone()
        if not row:
            return {"error": "unknown proposal", "proposal_id": proposal_id}
        if row["status"] != "proposed":
            return {"error": "proposal is not pending review", "proposal_id": proposal_id,
                    "status": row["status"]}
        c.execute(
            "UPDATE deliverable_breakdown_proposals "
            "SET status=?, review_reason=?, reviewed_by=?, deferred_until=?, updated_at=? "
            "WHERE id=?",
            (status, reason or None, actor, deferred_until, now, proposal_id),
        )
        kind = f"deliverable.breakdown_{status}"
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, kind,
                   json.dumps({"proposal_id": proposal_id,
                               "deliverable_id": row["deliverable_id"],
                               "reason": reason,
                               "deferred_until": deferred_until}, sort_keys=True), now))
    return get_deliverable_breakdown_proposal(proposal_id, project=project) or {
        "error": "proposal not found", "proposal_id": proposal_id}


def reject_deliverable_breakdown(proposal_id: str, reason: str, actor: str = "user",
                                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Reject a pending breakdown proposal with an audited reason."""
    if not has_project(project):
        return {"error": f"unknown project: {project}"}
    return _finalize_breakdown_review(
        (proposal_id or "").strip(), "rejected", actor, project, reason=reason)


def defer_deliverable_breakdown(proposal_id: str, reason: str, actor: str = "user",
                                project: str = DEFAULT_PROJECT,
                                defer_until: Optional[float] = None) -> Dict[str, Any]:
    """Defer a pending breakdown proposal with an audited reason."""
    if not has_project(project):
        return {"error": f"unknown project: {project}"}
    return _finalize_breakdown_review(
        (proposal_id or "").strip(), "deferred", actor, project,
        reason=reason, deferred_until=defer_until)


def submit_deliverable_outcome(deliverable_id: str, outcome: str, actor: str = "user",
                               project: str = DEFAULT_PROJECT,
                               target_projects: Any = None,
                               policy_constraints: Any = None,
                               acceptance_criteria: Any = None,
                               use_llm: bool = False) -> Dict[str, Any]:
    """Generate and store a breakdown proposal from a coordinator outcome statement."""
    import deliverable_breakdown

    if not has_project(project):
        return {"error": f"unknown project: {project}"}
    outcome = (outcome or "").strip()
    if not outcome:
        return {"error": "outcome is required"}
    deliverable = get_deliverable(deliverable_id, project=project)
    if not deliverable:
        return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
    try:
        draft = deliverable_breakdown.generate_breakdown_draft(
            outcome,
            deliverable=deliverable,
            target_projects=target_projects,
            policy_constraints=policy_constraints,
            acceptance_criteria=acceptance_criteria,
            project=project,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    if use_llm:
        draft = deliverable_breakdown.maybe_enrich_with_llm(draft, project=project)
    return propose_deliverable_breakdown(
        deliverable_id, draft, actor=actor, project=project, outcome_text=outcome)


def approve_deliverable_breakdown(proposal_id: str, actor: str = "user",
                                  project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Materialize an approved breakdown into milestones, tasks, and deliverable links."""
    if not has_project(project):
        return {"error": f"unknown project: {project}"}
    proposal_id = (proposal_id or "").strip()
    if not proposal_id:
        return {"error": "proposal_id is required"}
    with _conn(project) as c:
        row = c.execute("SELECT * FROM deliverable_breakdown_proposals WHERE id=?",
                        (proposal_id,)).fetchone()
        if not row:
            return {"error": "unknown proposal", "proposal_id": proposal_id}
        proposal = _breakdown_proposal_row(row)
    if proposal["status"] != "proposed":
        return {"error": "proposal is not pending approval", "proposal_id": proposal_id,
                "status": proposal["status"]}
    deliverable_id = proposal["deliverable_id"]
    payload = proposal.get("payload") or {}
    created_tasks: List[Dict[str, Any]] = []
    linked_tasks: List[Dict[str, Any]] = []
    for milestone in payload.get("milestones") or []:
        milestone_result = add_deliverable_milestone(
            deliverable_id,
            milestone,
            actor=actor,
            project=project,
        )
        if milestone_result.get("error"):
            return milestone_result
        milestone_id = milestone.get("id")
        for item in milestone_result.get("milestones") or []:
            if milestone_id and item.get("id") == milestone_id:
                break
            if item.get("title") == milestone.get("title"):
                milestone_id = item.get("id")
                break
        if not milestone_id:
            return {"error": "failed to resolve created milestone id",
                    "milestone_title": milestone.get("title")}
        for task_spec in milestone.get("tasks") or []:
            task_project = task_spec["project_id"]
            action = task_spec.get("action") or "create"
            if action == "link":
                task_id = task_spec["task_id"]
            else:
                created = create_task({
                    "workstream_id": task_spec["workstream_id"],
                    "workstream_name": task_spec.get("workstream_name"),
                    "title": task_spec["title"],
                    "description": task_spec.get("description"),
                    "owner_org": task_spec.get("owner_org"),
                    "owner_person_or_role": task_spec.get("owner_person_or_role"),
                    "assignee": task_spec.get("assignee"),
                    "phase": task_spec.get("phase"),
                    "status": task_spec.get("status") or "Not Started",
                    "depends_on": task_spec.get("depends_on") or [],
                }, actor=actor, project=task_project)
                if not created:
                    return {"error": "failed to create proposed task",
                            "project_id": task_project,
                            "workstream_id": task_spec["workstream_id"],
                            "title": task_spec["title"]}
                task_id = created["task_id"]
                created_tasks.append({
                    "project_id": task_project,
                    "task_id": task_id,
                    "milestone_id": milestone_id,
                    "action": "create",
                })
            link_result = link_task_to_deliverable(
                deliverable_id,
                task_project,
                task_id,
                milestone_id=milestone_id,
                data={
                    "role": task_spec.get("role") or "contributes",
                    "blocks_deliverable": bool(task_spec.get("blocks_deliverable")),
                    "proof_required": task_spec.get("proof_required") or {},
                    "metadata": task_spec.get("metadata") or {},
                },
                actor=actor,
                project=project,
            )
            if link_result.get("error"):
                return link_result
            if action == "link":
                linked_tasks.append({
                    "project_id": task_project,
                    "task_id": task_id,
                    "milestone_id": milestone_id,
                    "action": "link",
                })
    deliverable_existing = get_deliverable(deliverable_id, project=project) or {}
    deliverable_patch: Dict[str, Any] = {
        "id": deliverable_id,
        "title": deliverable_existing.get("title") or deliverable_id,
    }
    if payload.get("acceptance_criteria"):
        deliverable_patch["acceptance_criteria"] = payload["acceptance_criteria"]
    if payload.get("policy_constraints"):
        merged_policy = dict(deliverable_existing.get("policy_constraints") or {})
        merged_policy.update(payload["policy_constraints"])
        deliverable_patch["policy_constraints"] = merged_policy
    if payload.get("outcome"):
        deliverable_patch["end_state"] = payload["outcome"]
    if len(deliverable_patch) > 2:
        patched = create_deliverable(deliverable_patch, actor=actor, project=project)
        if patched.get("error"):
            return patched
    now = time.time()
    with _conn(project) as c:
        c.execute(
            "UPDATE deliverable_breakdown_proposals "
            "SET status='approved', approved_by=?, reviewed_by=?, approved_at=?, updated_at=? "
            "WHERE id=?",
            (actor, actor, now, now, proposal_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.breakdown_approved",
                   json.dumps({"deliverable_id": deliverable_id, "proposal_id": proposal_id,
                               "created_task_count": len(created_tasks),
                               "linked_task_count": len(linked_tasks)}, sort_keys=True), now))
    return {
        "schema": "switchboard.deliverable_breakdown_approval.v1",
        "project_id": project,
        "proposal_id": proposal_id,
        "deliverable_id": deliverable_id,
        "created_tasks": created_tasks,
        "linked_tasks": linked_tasks,
        "deliverable": get_deliverable(deliverable_id, project=project),
        "mission_status": get_mission_status(project=project, deliverable_id=deliverable_id),
    }


def _resolve_mission_deliverable(project: str, deliverable_id: str = "",
                               board_id: str = "", mission_id: str = "") -> Dict[str, Any]:
    if not has_project(project):
        return {"error": f"unknown project: {project}"}
    deliverable_id = (deliverable_id or "").strip()
    board_id = (board_id or mission_id or "").strip()
    if deliverable_id:
        deliverable = get_deliverable(deliverable_id, project=project)
        if not deliverable:
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id,
                    "project_id": project}
        return {"deliverable": deliverable, "board_id": deliverable.get("board_id")}
    if board_id:
        board = get_project_board(board_id, project=project)
        if not board:
            return {"error": "unknown board", "board_id": board_id, "project_id": project}
        deliverables = list_deliverables(project=project, board_id=board_id)
        if not deliverables:
            return {"error": "no deliverable for board", "board_id": board_id,
                    "project_id": project, "board": board}
        if len(deliverables) > 1:
            return {
                "error": "multiple deliverables for board; pass deliverable_id",
                "board_id": board_id,
                "project_id": project,
                "board": board,
                "deliverable_ids": [d["id"] for d in deliverables],
            }
        return {"deliverable": deliverables[0], "board": board, "board_id": board_id}
    return {"error": "deliverable_id or board_id/mission_id is required", "project_id": project}


def _registry_project_ids() -> List[str]:
    init_project_registry()
    with _registry_conn() as c:
        rows = c.execute("SELECT id FROM projects ORDER BY id").fetchall()
    return [r["id"] for r in rows if has_project(r["id"])]


def _find_deliverable_links_for_task(task_project: str, task_id: str,
                                     mission_project: str = "",
                                     deliverable_id: str = "") -> List[Dict[str, Any]]:
    """Return deliverable links for claim/mission rollup using the same scan as task detail."""
    links = list_task_deliverable_links(task_id, project=task_project)
    mission_project = (mission_project or "").strip()
    deliverable_id = (deliverable_id or "").strip()
    if mission_project:
        links = [link for link in links
                 if (link.get("deliverable_home_project") or "") == mission_project]
    if deliverable_id:
        links = [link for link in links if (link.get("deliverable_id") or "") == deliverable_id]
    for link in links:
        link["mission_project"] = link.get("deliverable_home_project")
    return links


def _record_mission_claim_completion(mission_project: str, deliverable_id: str,
                                     task_project: str, task_id: str,
                                     claim_id: str, status: str,
                                     milestone_id: str = "",
                                     actor: str = "system") -> Dict[str, Any]:
    """Refresh mission progress after a linked task claim completes."""
    deliverable = get_deliverable(deliverable_id, project=mission_project)
    if not deliverable:
        return {"error": "unknown deliverable", "deliverable_id": deliverable_id,
                "mission_project": mission_project}
    progress = deliverable.get("progress") or deliverable_progress(deliverable)
    now = time.time()
    payload = {
        "schema": "switchboard.mission_claim_completion.v1",
        "mission_project": mission_project,
        "deliverable_id": deliverable_id,
        "milestone_id": (milestone_id or "").strip() or None,
        "task_project": task_project,
        "task_id": task_id,
        "claim_id": claim_id,
        "task_status": status,
        "progress": progress,
    }
    with _conn(mission_project) as c:
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.claim_completed",
                   json.dumps(payload, sort_keys=True), now))
    deliverable_status = deliverable.get("status")
    if (progress.get("in_review_count", 0) > 0
            and deliverable_status in ("approved", "in_progress", "proposed")):
        with _conn(mission_project) as c:
            c.execute("UPDATE deliverables SET status=?, updated_at=? WHERE id=?",
                      ("in_review", now, deliverable_id))
        deliverable = get_deliverable(deliverable_id, project=mission_project) or deliverable
        progress = deliverable.get("progress") or deliverable_progress(deliverable)
        payload["progress"] = progress
        payload["deliverable_status"] = deliverable.get("status")
    return payload


def _claim_next_mission_scoped(agent_id: str, lanes: Any = None,
                               capabilities: Any = None,
                               max_risk: str = "", max_budget_usd: Optional[float] = None,
                               principal_id: str = "", actor: str = "system",
                               ttl_seconds: int = 1800, idem_key: str = "",
                               override_identity_risk: bool = False,
                               work_session_id: str = "", work_session: Any = None,
                               session_policy_profile: str = "",
                               require_work_session: bool = False,
                               mission_project: str = DEFAULT_PROJECT,
                               deliverable_id: str = "", board_id: str = "",
                               mission_id: str = "", milestone_id: str = "") -> Dict[str, Any]:
    """Claim the next ready task linked to a deliverable/mission."""
    now = time.time()
    lanes = coerce_csv_list(lanes)
    capabilities = coerce_csv_list(capabilities)
    lane_set = {x.strip().upper() for x in lanes}
    cap_set = {x.strip().lower() for x in capabilities}
    max_risk_value = _risk_value(max_risk)
    milestone_id = (milestone_id or "").strip()
    payload = {"agent_id": agent_id, "lanes": sorted(lane_set),
               "capabilities": sorted(capabilities or []), "max_risk": max_risk,
               "max_budget_usd": max_budget_usd, "ttl_seconds": ttl_seconds,
               "override_identity_risk": bool(override_identity_risk),
               "deliverable_id": (deliverable_id or "").strip(),
               "board_id": (board_id or "").strip(),
               "mission_id": (mission_id or "").strip(),
               "milestone_id": milestone_id,
               "work_session_id": work_session_id,
               "work_session": work_session,
               "session_policy_profile": session_policy_profile,
               "require_work_session": bool(require_work_session),
               "mission_scope": True}
    with _conn(mission_project) as mission_c:
        hit = _idem_hit(mission_c, "claim_next", idem_key, actor, payload)
        if hit is not None:
            return hit
        scope = _resolve_mission_deliverable(
            mission_project, deliverable_id=deliverable_id,
            board_id=board_id, mission_id=mission_id)
        if scope.get("error"):
            _idem_store(mission_c, "claim_next", idem_key, actor, payload, scope)
            return scope
        deliverable = scope["deliverable"]
        resolved_deliverable_id = deliverable["id"]
        links = list(deliverable.get("task_links") or [])
        if milestone_id:
            links = [l for l in links if (l.get("milestone_id") or "") == milestone_id]
        if not links:
            response = {
                "claimed": False,
                "reason": "no_milestone_tasks" if milestone_id else "no_linked_tasks",
                "deliverable_id": resolved_deliverable_id,
                "milestone_id": milestone_id or None,
                "mission_project": mission_project,
                "retry_after_seconds": 60,
                "dispatch_reason": {
                    "policy": "mission_scope.v1",
                    "deliverable_id": resolved_deliverable_id,
                    "milestone_id": milestone_id or None,
                    "linked_task_count": 0,
                    "skipped": {},
                    "candidate_count": 0,
                },
            }
            _idem_store(mission_c, "claim_next", idem_key, actor, payload, response)
            return response

        eligible: List[Tuple[Any, ...]] = []
        skipped = {"active_claim": 0, "status": 0, "lane": 0, "dependencies": 0,
                   "human_approval": 0, "capability_mismatch": 0, "risk": 0, "budget": 0,
                   "identity_unknown": 0, "missing_task": 0, "unknown_project": 0,
                   "work_session": 0}
        human_gates: Dict[str, Dict[str, Any]] = {}
        identity_risks: Dict[str, Dict[str, Any]] = {}
        work_session_findings: Dict[str, Dict[str, Any]] = {}

        for link in links:
            task_project = (link.get("project_id") or "").strip()
            task_id = (link.get("task_id") or "").strip()
            if not has_project(task_project):
                skipped["unknown_project"] += 1
                continue
            with _conn(task_project) as c:
                row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if not row:
                    skipped["missing_task"] += 1
                    continue
                active = c.execute(
                    "SELECT 1 FROM task_claims WHERE task_id=? AND status='active' AND expires_at>?",
                    (task_id, now),
                ).fetchone()
                if active:
                    skipped["active_claim"] += 1
                    continue
                task = _task_row(row)
                if task.get("status") not in READY_TASK_STATUSES:
                    skipped["status"] += 1
                    continue
                if lane_set and (task.get("_wsId") or "").upper() not in lane_set:
                    skipped["lane"] += 1
                    continue
                rows = c.execute("SELECT * FROM tasks ORDER BY sort_order, task_id").fetchall()
                by_id = {t["task_id"]: t for t in [_task_row(r) for r in rows]}
                if not _deps_done(task, by_id):
                    skipped["dependencies"] += 1
                    continue
                gate = _task_human_gate_state(task)
                if gate["blocked"]:
                    skipped["human_approval"] += 1
                    human_gates[task_id] = gate
                    continue
                identity_risk = _identity_takeover_risk_in(c, task_id, now)
                if identity_risk and not override_identity_risk:
                    skipped["identity_unknown"] += 1
                    identity_risks[task_id] = identity_risk
                    continue
                session_verdict = _validate_work_session_claim_binding_in(
                    c, task, agent_id, project=task_project,
                    work_session_id=work_session_id,
                    work_session=work_session,
                    policy_profile=session_policy_profile,
                    require_work_session=require_work_session,
                    now=now)
                if not session_verdict.get("ok"):
                    skipped["work_session"] += 1
                    work_session_findings[f"{task_project}:{task_id}"] = session_verdict
                    continue
                required_caps = _task_required_capabilities(task)
                if required_caps and not set(required_caps).issubset(cap_set):
                    skipped["capability_mismatch"] += 1
                    continue
                if max_risk_value and _risk_value(task.get("risk_level") or "") > max_risk_value:
                    skipped["risk"] += 1
                    continue
                tally = _task_tally_snapshot(c, task_id)
                score = _dispatch_score(task, lane_set, cap_set, tally, max_budget_usd)
                if score["budget"]["status"] == "over_budget":
                    skipped["budget"] += 1
                    continue
                if identity_risk and override_identity_risk:
                    score["identity_override"] = identity_risk
                eligible.append((
                    score["score"], -int(task.get("sort_order") or 0), task_id,
                    task, score, task_project, link,
                ))

        dispatch_base = {
            "policy": "mission_scope.v1",
            "deliverable_id": resolved_deliverable_id,
            "milestone_id": milestone_id or None,
            "linked_task_count": len(links),
            "skipped": skipped,
            "human_gates": human_gates,
            "identity_risks": identity_risks,
            "work_session_findings": work_session_findings,
        }
        if not eligible:
            response = {
                "claimed": False,
                "reason": "no_unblocked_work",
                "deliverable_id": resolved_deliverable_id,
                "milestone_id": milestone_id or None,
                "mission_project": mission_project,
                "retry_after_seconds": 60,
                "cursor": mission_c.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0],
                "dispatch_reason": {**dispatch_base, "candidate_count": 0},
            }
            _idem_store(mission_c, "claim_next", idem_key, actor, payload, response)
            return response

        _, _, task_id, task, selected_score, task_project, link = sorted(
            eligible, key=lambda x: (-x[0], -x[1], x[2]))[0]
        claim_id = "taskclaim-" + uuid.uuid4().hex[:16]
        lease_id = "lease-" + uuid.uuid4().hex[:16]
        expires_at = now + max(60, int(ttl_seconds or 1800))
        with _conn(task_project) as c:
            c.execute(
                "INSERT INTO task_claims(id, task_id, agent_id, principal_id, status, claimed_at, "
                "expires_at, idem_key) VALUES (?,?,?,?,?,?,?,?)",
                (claim_id, task_id, agent_id, principal_id or None, "active",
                 now, expires_at, idem_key or None),
            )
            c.execute(
                "INSERT INTO resource_leases(id, agent_id, principal_id, task_id, resource_type, "
                "names, claimed_at, ttl_seconds) VALUES (?,?,?,?,?,?,?,?)",
                (lease_id, agent_id, principal_id or None, task_id, "task",
                 json.dumps([task_id]), now, max(60, int(ttl_seconds or 1800))),
            )
            c.execute("UPDATE tasks SET status='In Progress', assignee=?, updated_at=? WHERE task_id=?",
                      (agent_id, now, task_id))
            dispatch_reason = {**dispatch_base,
                               "score": selected_score["score"],
                               "factors": selected_score["factors"],
                               "required_capabilities": selected_score["required_capabilities"],
                               "matched_capabilities": selected_score["matched_capabilities"],
                               "candidate_count": len(eligible),
                               "task_project": task_project}
            if selected_score.get("identity_override"):
                dispatch_reason["identity_override"] = selected_score["identity_override"]
            session_verdict = _validate_work_session_claim_binding_in(
                c, task, agent_id, project=task_project,
                work_session_id=work_session_id,
                work_session=work_session,
                policy_profile=session_policy_profile,
                require_work_session=require_work_session,
                now=now)
            work_session_binding = _attach_work_session_claim_in(
                c, session_verdict, claim_id, task_id, agent_id, actor,
                principal_id=principal_id, project=task_project, now=now)
            if work_session_binding.get("error"):
                response = {"claimed": False, "reason": "work_session_bind_failed",
                            "task_id": task_id, "task_project": task_project,
                            "work_session": work_session_binding}
                _idem_store(mission_c, "claim_next", idem_key, actor, payload, response)
                return response
            dispatch_reason["work_session"] = work_session_binding
            payload_event = {"claim_id": claim_id, "lease_id": lease_id,
                             "task_id": task_id, "task_project": task_project,
                             "agent_id": agent_id, "deliverable_id": resolved_deliverable_id,
                             "milestone_id": link.get("milestone_id"),
                             "dispatch_reason": dispatch_reason}
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (task_id, actor, "task.claimed",
                       json.dumps(payload_event, sort_keys=True), now))
            claimed_task = _task_row(c.execute("SELECT * FROM tasks WHERE task_id=?",
                                               (task_id,)).fetchone())
        mission_c.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (None, actor, "deliverable.claim_started",
             json.dumps({"claim_id": claim_id, "task_id": task_id,
                         "task_project": task_project,
                         "deliverable_id": resolved_deliverable_id,
                         "milestone_id": link.get("milestone_id"),
                         "agent_id": agent_id}, sort_keys=True), now))
        response = {
            "claimed": True,
            "claim_id": claim_id,
            "task": claimed_task,
            "task_project": task_project,
            "mission_project": mission_project,
            "deliverable_id": resolved_deliverable_id,
            "milestone_id": link.get("milestone_id"),
            "lease": {"lease_id": lease_id, "resource_type": "task",
                      "names": [task_id], "expires_at": expires_at},
            "budget": selected_score["budget"],
            "dispatch_reason": dispatch_reason,
            "recommendation": _model_recommendation(task, selected_score),
            "work_session_id": work_session_binding.get("work_session_id"),
            "work_session": work_session_binding,
        }
        _idem_store(mission_c, "claim_next", idem_key, actor, payload, response)
        return response


def _enriched_mission_task_link(link: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(link)
    task_project = link.get("project_id")
    task_id = link.get("task_id")
    if not has_project(task_project):
        enriched["task_detail"] = {"error": "unknown project", "project_id": task_project}
        return enriched
    task = get_task(task_id, project=task_project)
    if not task:
        enriched["task_detail"] = {"error": "unknown task", "project_id": task_project,
                                   "task_id": task_id}
        return enriched
    with _conn(task_project) as c:
        claims = _active_task_claims_in(c, task_id)
    enriched["task_detail"] = {
        "task_id": task["task_id"],
        "title": task.get("title"),
        "status": task.get("status"),
        "assignee": task.get("assignee"),
        "workstream": task.get("_wsId"),
        "depends_on": task.get("depends_on") or [],
        "dependency_state": task.get("dependency_state"),
        "provenance": task.get("provenance"),
        "git_state": task.get("git_state"),
        "external_ci": task.get("external_ci"),
        "publication": task.get("publication"),
        "human_gate": _task_human_gate_state(task),
        "session_health": task.get("session_health"),
        "active_claims": claims,
        # CEO-voice summary for map-node hover tooltips. narration is None while a live task
        # is transiently stale; narration_raw keeps the last prose so the tooltip still shows.
        "narration": task.get("narration"),
        "narration_raw": task.get("narration_raw"),
        "narration_stale": (task.get("narration_state") or {}).get("stale"),
    }
    return enriched


def _mission_blockers(deliverable: Dict[str, Any],
                      linked_tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    blockers: List[Dict[str, Any]] = []
    if deliverable.get("status") == "blocked":
        blockers.append({
            "kind": "deliverable_blocked",
            "deliverable_id": deliverable.get("id"),
            "message": "Deliverable status is blocked",
        })
    for link in linked_tasks:
        detail = link.get("task_detail") or link.get("task") or {}
        if detail.get("error"):
            blockers.append({
                "kind": "missing_task_snapshot",
                "project_id": link.get("project_id"),
                "task_id": link.get("task_id"),
                "message": detail.get("error"),
            })
            continue
        if detail.get("status") == "Blocked":
            blockers.append({
                "kind": "task_blocked",
                "project_id": link.get("project_id"),
                "task_id": detail.get("task_id"),
                "title": detail.get("title"),
                "blocks_deliverable": bool(link.get("blocks_deliverable")),
            })
        dep = detail.get("dependency_state") or {}
        if not dep.get("satisfied"):
            for blocking in dep.get("blocking") or []:
                blockers.append({
                    "kind": "dependency_unsatisfied",
                    "project_id": link.get("project_id"),
                    "task_id": detail.get("task_id"),
                    "title": detail.get("title"),
                    "blocking_task_id": blocking.get("task_id"),
                    "blocking_status": blocking.get("status"),
                })
        gate = detail.get("human_gate") or {}
        if gate.get("blocked"):
            blockers.append({
                "kind": "human_gate",
                "project_id": link.get("project_id"),
                "task_id": detail.get("task_id"),
                "title": detail.get("title"),
                "reason": gate.get("reason"),
            })
        session_health = detail.get("session_health") or {}
        if session_health.get("status") == "unsafe":
            for finding in session_health.get("findings") or []:
                if not finding.get("blocking"):
                    continue
                blockers.append({
                    "kind": "unsafe_session",
                    "project_id": link.get("project_id"),
                    "task_id": detail.get("task_id"),
                    "title": detail.get("title"),
                    "failure_class": finding.get("failure_class"),
                    "finding_code": finding.get("code"),
                    "work_session_id": finding.get("work_session_id"),
                    "severity": finding.get("severity"),
                    "message": finding.get("message"),
                    "repair": finding.get("repair"),
                })
        proof_required = link.get("proof_required") or {}
        external_ci = detail.get("external_ci") or {}
        if (proof_required.get("external_ci_passed")
                or (external_ci.get("gate") or {}).get("required")):
            if not external_ci.get("passed"):
                blockers.append({
                    "kind": "external_ci",
                    "project_id": link.get("project_id"),
                    "task_id": detail.get("task_id"),
                    "title": detail.get("title"),
                })
        publication = detail.get("publication") or {}
        if (proof_required.get("publication_evidence")
                or proof_required.get("public_mirror_published")
                or (publication.get("gate") or {}).get("required")):
            if not publication.get("passed"):
                blockers.append({
                    "kind": "publication_evidence",
                    "project_id": link.get("project_id"),
                    "task_id": detail.get("task_id"),
                    "title": detail.get("title"),
                })
        if link.get("blocks_deliverable"):
            provenance = detail.get("provenance") or {}
            if not (detail.get("status") == "Done" and provenance.get("terminal")):
                blockers.append({
                    "kind": "blocking_task_incomplete",
                    "project_id": link.get("project_id"),
                    "task_id": detail.get("task_id"),
                    "title": detail.get("title"),
                    "status": detail.get("status"),
                })
    return blockers


def _mission_next_actions(deliverable: Dict[str, Any],
                          linked_tasks: List[Dict[str, Any]],
                          pending_proposal: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    if pending_proposal and pending_proposal.get("status") == "proposed":
        actions.append({
            "action": "approve_breakdown",
            "proposal_id": pending_proposal.get("id"),
            "reason": "A milestone/task breakdown is waiting for approval",
        })
    for link in linked_tasks:
        detail = link.get("task_detail") or {}
        if detail.get("error"):
            actions.append({
                "action": "repair_task_link",
                "project_id": link.get("project_id"),
                "task_id": link.get("task_id"),
                "reason": detail.get("error"),
            })
            continue
        status = detail.get("status")
        claims = detail.get("active_claims") or []
        dep = detail.get("dependency_state") or {}
        if status == "Not Started" and dep.get("ready") and not claims:
            actions.append({
                "action": "claim_task",
                "project_id": link.get("project_id"),
                "task_id": detail.get("task_id"),
                "title": detail.get("title"),
                "reason": "Ready and unclaimed",
            })
        elif status == "In Review":
            actions.append({
                "action": "verify_merge_provenance",
                "project_id": link.get("project_id"),
                "task_id": detail.get("task_id"),
                "title": detail.get("title"),
                "reason": "Awaiting merge/default-branch provenance for Done",
            })
        elif status == "In Progress" and not claims:
            actions.append({
                "action": "resume_or_claim",
                "project_id": link.get("project_id"),
                "task_id": detail.get("task_id"),
                "title": detail.get("title"),
                "reason": "In progress without an active claim",
            })
        gate = detail.get("human_gate") or {}
        if gate.get("blocked"):
            actions.append({
                "action": "request_human_approval",
                "project_id": link.get("project_id"),
                "task_id": detail.get("task_id"),
                "title": detail.get("title"),
                "reason": gate.get("reason") or "Human gate blocked",
            })
        session_health = detail.get("session_health") or {}
        if session_health.get("status") == "unsafe":
            actions.append({
                "action": "repair_work_session",
                "project_id": link.get("project_id"),
                "task_id": detail.get("task_id"),
                "title": detail.get("title"),
                "reason": session_health.get("recommended_repair") or "Unsafe Work Session",
            })
        elif session_health.get("status") == "warning":
            actions.append({
                "action": "refresh_work_session_health",
                "project_id": link.get("project_id"),
                "task_id": detail.get("task_id"),
                "title": detail.get("title"),
                "reason": session_health.get("recommended_repair") or "Work Session warning",
            })
    if not linked_tasks and not (deliverable.get("milestones") or []):
        actions.append({
            "action": "propose_breakdown",
            "reason": "No milestones or linked tasks yet",
        })
    return actions


def get_mission_status(project: str = DEFAULT_PROJECT, deliverable_id: str = "",
                       board_id: str = "", mission_id: str = "") -> Dict[str, Any]:
    """Return a mission cockpit rollup: end state, milestones, proof, blockers, next actions."""
    scope = _resolve_mission_deliverable(project, deliverable_id=deliverable_id,
                                          board_id=board_id, mission_id=mission_id)
    if scope.get("error"):
        return scope
    deliverable = scope["deliverable"]
    board = scope.get("board") or deliverable.get("board")
    metadata = deliverable.get("metadata") or {}
    linked_tasks = [_enriched_mission_task_link(link)
                      for link in (deliverable.get("task_links") or [])]
    milestone_task_counts: Dict[str, int] = {}
    for link in deliverable.get("task_links") or []:
        mid = link.get("milestone_id")
        if mid:
            milestone_task_counts[mid] = milestone_task_counts.get(mid, 0) + 1
    milestones = []
    for milestone in deliverable.get("milestones") or []:
        item = dict(milestone)
        item["linked_task_count"] = milestone_task_counts.get(milestone.get("id"), 0)
        milestones.append(item)
    active_work = []
    done_with_proof = []
    active_agents: Dict[str, Dict[str, Any]] = {}
    for link in linked_tasks:
        detail = link.get("task_detail") or {}
        if detail.get("error"):
            continue
        status = detail.get("status")
        claims = detail.get("active_claims") or []
        provenance = detail.get("provenance") or {}
        if status == "Done" and provenance.get("terminal"):
            done_with_proof.append({
                "project_id": link.get("project_id"),
                "task_id": detail.get("task_id"),
                "title": detail.get("title"),
                "provenance": provenance,
                "git_state": detail.get("git_state"),
            })
        elif status in ("In Progress", "In Review") or claims:
            active_work.append({
                "project_id": link.get("project_id"),
                "task_id": detail.get("task_id"),
                "title": detail.get("title"),
                "status": status,
                "assignee": detail.get("assignee"),
                "active_claims": claims,
                "session_health": detail.get("session_health"),
                "milestone_id": link.get("milestone_id"),
                "role": link.get("role"),
            })
        for claim in claims:
            agent_id = claim.get("agent_id")
            if agent_id and agent_id not in active_agents:
                active_agents[agent_id] = {
                    "agent_id": agent_id,
                    "claim_id": claim.get("claim_id"),
                    "task_id": detail.get("task_id"),
                    "project_id": link.get("project_id"),
                }
    # Enrich each active agent with its advertised runtime/platform + model, so map-node
    # hover tooltips can show WHO (and on which platform) is working the task.
    if active_agents:
        with _conn(project) as c:
            for agent_id, info in active_agents.items():
                prow = c.execute("SELECT * FROM agent_presence WHERE agent_id=?",
                                 (agent_id,)).fetchone()
                if prow:
                    pres = _presence_row(prow)
                    info["runtime"] = pres.get("runtime")
                    info["model"] = pres.get("model")
                    info["stale"] = pres.get("stale")
    pending_proposal = None
    with _conn(project) as c:
        row = c.execute(
            "SELECT * FROM deliverable_breakdown_proposals "
            "WHERE deliverable_id=? AND status='proposed' "
            "ORDER BY updated_at DESC LIMIT 1",
            (deliverable.get("id"),),
        ).fetchone()
        if row:
            pending_proposal = _breakdown_proposal_row(row)
    blockers = _mission_blockers(deliverable, linked_tasks)
    economics = deliverable_tally(deliverable.get("id"), project=project)
    result = {
        "schema": "switchboard.mission_status.v1",
        "project_id": project,
        "board_id": scope.get("board_id") or deliverable.get("board_id"),
        "mission_id": scope.get("board_id") or deliverable.get("board_id"),
        "deliverable_id": deliverable.get("id"),
        "board": board,
        "deliverable": {
            "id": deliverable.get("id"),
            "title": deliverable.get("title"),
            "status": deliverable.get("status"),
            "end_state": deliverable.get("end_state") or (board or {}).get("end_state"),
            "why_it_matters": deliverable.get("why_it_matters"),
            "acceptance_criteria": deliverable.get("acceptance_criteria"),
            "policy_constraints": deliverable.get("policy_constraints"),
            "proof_requirements": deliverable.get("proof_requirements"),
        },
        "narrative": metadata.get("narrative"),
        "narrative_updated_at": metadata.get("narrative_updated_at"),
        "progress": deliverable.get("progress") or deliverable_progress(deliverable),
        "milestones": milestones,
        "linked_tasks": linked_tasks,
        "blockers": blockers,
        "active_work": active_work,
        "done_with_proof": done_with_proof,
        "active_agents": list(active_agents.values()),
        "pending_proposal": pending_proposal,
        "next_actions": _mission_next_actions(deliverable, linked_tasks, pending_proposal),
        "economics": economics if not economics.get("error") else economics,
    }
    return _attach_mission_brief_fields(result, project=project)


def get_deliverable_dependency_graph(project: str = DEFAULT_PROJECT, deliverable_id: str = "",
                                     board_id: str = "", mission_id: str = "") -> Dict[str, Any]:
    """Return task-level depends_on graph for a deliverable (strategic map layer)."""
    import mission_graph

    scope = _resolve_mission_deliverable(project, deliverable_id=deliverable_id,
                                          board_id=board_id, mission_id=mission_id)
    if scope.get("error"):
        return scope
    deliverable = scope["deliverable"]
    deliverable_id = deliverable.get("id") or deliverable_id
    linked_tasks = [_enriched_mission_task_link(link)
                    for link in (deliverable.get("task_links") or [])]

    def _lookup(task_project: str, task_id: str, fallback: bool = False) -> Optional[Dict[str, Any]]:
        proj = project if fallback else (task_project or project)
        task = get_task(task_id, project=proj)
        if not task and fallback and proj != project:
            task = get_task(task_id, project=project)
        if not task:
            return None
        out = dict(task)
        out["_project_id"] = proj
        return out

    return mission_graph.build_dependency_graph(
        linked_tasks,
        deliverable_id=deliverable_id,
        project_id=project,
        task_lookup=_lookup,
    )


def _deliverable_activity(project: str, deliverable_id: str, limit: int = 12) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with _conn(project) as c:
        for row in c.execute(
            "SELECT actor, kind, payload, created_at FROM activity "
            "WHERE kind LIKE 'deliverable.%' ORDER BY created_at DESC LIMIT ?",
            (max(limit * 8, 40),),
        ).fetchall():
            payload = _json_payload(row["payload"])
            if isinstance(payload, dict) and payload.get("deliverable_id") not in (
                None, deliverable_id,
            ):
                continue
            rows.append({
                "actor": row["actor"],
                "kind": row["kind"],
                "payload": payload,
                "created_at": row["created_at"],
            })
            if len(rows) >= limit:
                break
    return rows


def _attach_mission_brief_fields(mission_status: Dict[str, Any],
                                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    if mission_status.get("error"):
        return mission_status
    import mission_narrative

    deliverable_id = mission_status.get("deliverable_id") or ""
    deliverable = get_deliverable(deliverable_id, project=project) if deliverable_id else None
    metadata = (deliverable or {}).get("metadata") or {}
    stored_brief = metadata.get("generated_brief") or {}
    mission_status["mission_brief"] = stored_brief or None
    mission_status["narrative_state"] = mission_narrative.narrative_state(
        mission_status, metadata=metadata, stored_brief=stored_brief)
    mission_status["brief_generated_at"] = metadata.get("brief_generated_at")
    mission_status["narrative_source"] = metadata.get("narrative_source")
    # NARRATE-3: CEO-voice header, rewritten from the structured brief. Stale when the current
    # mission fingerprint no longer matches the one it was written from (same discipline as the
    # generated brief). See docs/CEO-NARRATOR-CONTRACT.md.
    ceo_text = metadata.get("ceo_narrative")
    if ceo_text:
        current_fp = mission_narrative.brief_source_fingerprint(mission_status)
        stored_fp = metadata.get("ceo_narrative_fingerprint")
        ceo_stale = bool(stored_fp) and stored_fp != current_fp
        mission_status["ceo_narrative_state"] = {
            "stale": ceo_stale,
            "source_fingerprint": current_fp,
            "stored_fingerprint": stored_fp,
            "message": ("CEO narration is regenerating; trust mission_status and provenance."
                        if ceo_stale else None),
        }
        mission_status["ceo_narrative"] = None if ceo_stale else ceo_text
        if ceo_stale:
            mission_status["ceo_narrative_raw"] = ceo_text
        mission_status["ceo_narrative_generated_at"] = metadata.get("ceo_narrative_generated_at")
    return mission_status


def generate_mission_brief(project: str = DEFAULT_PROJECT, deliverable_id: str = "",
                           board_id: str = "", mission_id: str = "",
                           actor: str = "system", persist: bool = True) -> Dict[str, Any]:
    """Generate a structured mission brief from durable events and optionally persist it."""
    import mission_narrative

    if not has_project(project):
        return {"error": f"unknown project: {project}"}
    status = get_mission_status(project=project, deliverable_id=deliverable_id,
                                board_id=board_id, mission_id=mission_id)
    if status.get("error"):
        return status
    deliverable_id = status.get("deliverable_id") or deliverable_id
    activity = _deliverable_activity(project, deliverable_id)
    brief = mission_narrative.build_mission_brief(status, recent_activity=activity)
    narrative_state = mission_narrative.narrative_state(status, stored_brief=brief)
    result = {
        "schema": "switchboard.mission_brief_result.v1",
        "project_id": project,
        "deliverable_id": deliverable_id,
        "mission_brief": brief,
        "narrative_state": narrative_state,
        "mission_status": status,
    }
    if not persist:
        return result
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT metadata_json FROM deliverables WHERE id=?",
                        (deliverable_id,)).fetchone()
        if not row:
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
        metadata = _json_payload(row["metadata_json"])
        metadata["generated_brief"] = brief
        metadata["brief_generated_at"] = now
        metadata["brief_generated_by"] = actor
        metadata["brief_fingerprint"] = brief.get("source_fingerprint")
        metadata["narrative"] = brief.get("summary_markdown")
        metadata["narrative_updated_at"] = now
        metadata["narrative_updated_by"] = actor
        metadata["narrative_source"] = "generated"
        c.execute(
            "UPDATE deliverables SET metadata_json=?, updated_at=? WHERE id=?",
            (json.dumps(metadata, sort_keys=True), now, deliverable_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.brief_generated",
                   json.dumps({"deliverable_id": deliverable_id,
                               "source_fingerprint": brief.get("source_fingerprint")},
                              sort_keys=True), now))
    result["mission_status"] = get_mission_status(
        project=project, deliverable_id=deliverable_id)
    return result


def set_deliverable_narration(deliverable_id: str, narration: str, source_fingerprint: str = "",
                              model: str = "", actor: str = "narrator",
                              project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """NARRATE-3: persist the CEO-voice header for a deliverable in its metadata. Stored under
    ceo_narrative* keys, kept separate from the structured `generated_brief`/`narrative` so the
    two never clobber each other."""
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT metadata_json FROM deliverables WHERE id=?",
                        (deliverable_id,)).fetchone()
        if not row:
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
        metadata = _json_payload(row["metadata_json"])
        metadata["ceo_narrative"] = narration
        metadata["ceo_narrative_fingerprint"] = source_fingerprint
        metadata["ceo_narrative_generated_at"] = now
        metadata["ceo_narrative_model"] = model
        metadata["ceo_narrative_by"] = actor
        c.execute("UPDATE deliverables SET metadata_json=?, updated_at=? WHERE id=?",
                  (json.dumps(metadata, sort_keys=True), now, deliverable_id))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.ceo_narrated",
                   json.dumps({"deliverable_id": deliverable_id,
                               "source_fingerprint": source_fingerprint}, sort_keys=True), now))
    return {"deliverable_id": deliverable_id, "ceo_narrative": narration,
            "source_fingerprint": source_fingerprint, "generated_at": now}


def run_mission_coordinator_tick(project: str = DEFAULT_PROJECT, deliverable_id: str = "",
                               board_id: str = "", mission_id: str = "",
                               coordinator_agent_id: str = "", actor: str = "system",
                               idem_key: str = "", policy: Any = None) -> Dict[str, Any]:
    """Run one deliverable-scoped coordinator tick: brief refresh, dispatch, or escalation."""
    import mission_coordinator

    if not has_project(project):
        return {"error": f"unknown project: {project}"}
    policy_obj = _parse_jsonish(policy) if policy not in (None, "") else None
    if policy_obj is not None and not isinstance(policy_obj, dict):
        return {"error": "policy must be a JSON object"}
    payload = {
        "deliverable_id": (deliverable_id or "").strip(),
        "board_id": (board_id or "").strip(),
        "mission_id": (mission_id or "").strip(),
        "coordinator_agent_id": (coordinator_agent_id or "").strip(),
        "policy": policy_obj or {},
    }
    with _conn(project) as c:
        hit = _idem_hit(c, "run_mission_coordinator_tick", idem_key, actor, payload)
        if hit is not None:
            return hit
        status = get_mission_status(
            project=project, deliverable_id=deliverable_id,
            board_id=board_id, mission_id=mission_id)
        if status.get("error"):
            _idem_store(c, "run_mission_coordinator_tick", idem_key, actor, payload, status)
            return status
        resolved_id = status.get("deliverable_id") or deliverable_id
        result = mission_coordinator.run_coordinator_tick(
            status,
            mission_project=project,
            coordinator_agent_id=coordinator_agent_id,
            actor=actor,
            policy=policy_obj,
        )
        now = time.time()
        c.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (None, actor, "deliverable.coordinator_tick",
             json.dumps({
                 "schema": result.get("schema"),
                 "deliverable_id": resolved_id,
                 "coordinator_agent_id": coordinator_agent_id or None,
                 "status": result.get("status"),
                 "plan": result.get("plan"),
                 "executed": result.get("executed"),
                 "escalations": result.get("escalations"),
                 "dispatch": {
                     "claimed": bool((result.get("dispatch") or {}).get("claimed")),
                     "claim_id": (result.get("dispatch") or {}).get("claim_id"),
                     "task_id": ((result.get("dispatch") or {}).get("task") or {}).get("task_id"),
                 } if result.get("dispatch") else None,
             }, sort_keys=True), now))
        result["mission_status"] = get_mission_status(
            project=project, deliverable_id=resolved_id)
        _idem_store(c, "run_mission_coordinator_tick", idem_key, actor, payload, result)
        return result


def _empty_economics_totals() -> Dict[str, Any]:
    return {
        "linked_task_count": 0,
        "tasks_with_spend": 0,
        "tasks_with_verified_outcomes": 0,
        "verified_outcomes": 0,
        "proposed_outcomes": 0,
        "rejected_outcomes": 0,
        "superseded_outcomes": 0,
        "verified_kpi_contribution": 0.0,
        "spend": {"cost_usd": 0.0, "total_tokens": 0, "by_source": {}},
        "unit_cost": {
            "cost_per_verified_outcome": None,
            "cost_per_kpi_contribution_unit": None,
        },
    }


def _finalize_economics_totals(totals: Dict[str, Any]) -> Dict[str, Any]:
    if totals["verified_outcomes"]:
        totals["unit_cost"]["cost_per_verified_outcome"] = round(
            totals["spend"]["cost_usd"] / totals["verified_outcomes"], 6)
    if totals["verified_kpi_contribution"]:
        totals["unit_cost"]["cost_per_kpi_contribution_unit"] = round(
            totals["spend"]["cost_usd"] / totals["verified_kpi_contribution"], 6)
    return totals


def _task_proof_bucket(task: Dict[str, Any]) -> str:
    if _is_terminal_done_task(task):
        return "proven"
    status = task.get("status")
    if status == "In Review":
        return "in_review"
    if status == "In Progress":
        return "active"
    return "other"


def _merge_task_tally_into_totals(totals: Dict[str, Any], tally: Dict[str, Any]) -> None:
    spend = tally.get("spend") or {}
    outcomes = tally.get("outcomes") or {}
    verified = int(outcomes.get("verified") or 0)
    proposed = int(outcomes.get("proposed") or 0)
    rejected = int(outcomes.get("rejected") or 0)
    superseded = int(outcomes.get("superseded") or 0)
    cost = float(spend.get("cost_usd") or 0.0)
    kpi_groups = tally.get("kpis") or []
    kpi_contribution = round(sum(float(k.get("verified_contribution") or 0.0)
                                 for k in kpi_groups), 6)
    totals["linked_task_count"] += 1
    _merge_spend_totals(totals["spend"], spend)
    totals["verified_outcomes"] += verified
    totals["proposed_outcomes"] += proposed
    totals["rejected_outcomes"] += rejected
    totals["superseded_outcomes"] += superseded
    totals["verified_kpi_contribution"] = round(
        totals["verified_kpi_contribution"] + kpi_contribution, 6)
    if cost:
        totals["tasks_with_spend"] += 1
    if verified:
        totals["tasks_with_verified_outcomes"] += 1


def _merge_kpi_group(target: Dict[str, Dict[str, Any]], tally: Dict[str, Any],
                     project_id: str) -> None:
    spend = tally.get("spend") or {}
    for group in tally.get("kpis") or []:
        kpi_id = group.get("kpi_id")
        if not kpi_id:
            continue
        key = f"{project_id}:{kpi_id}"
        entry = target.setdefault(key, {
            "project_id": project_id,
            "kpi_id": kpi_id,
            "name": group.get("name"),
            "unit": group.get("unit"),
            "direction": group.get("direction"),
            "verified_contribution": 0.0,
            "spend": {"cost_usd": 0.0, "total_tokens": 0, "by_source": {}},
            "unit_cost": {"cost_per_contribution_unit": None},
            "links": [],
        })
        entry["verified_contribution"] = round(
            entry["verified_contribution"] + float(group.get("verified_contribution") or 0.0), 6)
        _merge_spend_totals(entry["spend"], spend)
        entry["links"].extend(group.get("links") or [])


def deliverable_tally(deliverable_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Aggregate Tally economics across all tasks linked to a deliverable/mission.

    Proven spend (Done + terminal provenance) is separated from in-flight In Review / In Progress
    spend so mission operators can see cost-to-outcome for merged work vs unproven spend.
    """
    deliverable = get_deliverable(deliverable_id, project=project, include_task_snapshots=False)
    if not deliverable:
        return {"error": "unknown deliverable", "deliverable_id": deliverable_id,
                "project_id": project}
    combined = _empty_economics_totals()
    proven = _empty_economics_totals()
    in_review = _empty_economics_totals()
    by_milestone: Dict[str, Dict[str, Any]] = {}
    by_task: List[Dict[str, Any]] = []
    kpi_index: Dict[str, Dict[str, Any]] = {}
    milestone_titles = {m.get("id"): m.get("title")
                          for m in (deliverable.get("milestones") or [])}

    for link in deliverable.get("task_links") or []:
        task_project = link.get("project_id")
        task_id = link.get("task_id")
        milestone_id = link.get("milestone_id") or ""
        if not has_project(task_project):
            continue
        task = get_task(task_id, project=task_project)
        if not task:
            continue
        tally = task_tally(task_id, project=task_project)
        proof_bucket = _task_proof_bucket(task)
        _merge_task_tally_into_totals(combined, tally)
        if proof_bucket == "proven":
            _merge_task_tally_into_totals(proven, tally)
        elif proof_bucket in ("in_review", "active"):
            _merge_task_tally_into_totals(in_review, tally)
        _merge_kpi_group(kpi_index, tally, task_project)

        ms = by_milestone.setdefault(milestone_id or "__unassigned__", {
            "milestone_id": milestone_id or None,
            "title": milestone_titles.get(milestone_id) or ("Unassigned" if not milestone_id else milestone_id),
            "combined": _empty_economics_totals(),
            "proven": _empty_economics_totals(),
            "in_review": _empty_economics_totals(),
            "by_task": [],
        })
        _merge_task_tally_into_totals(ms["combined"], tally)
        if proof_bucket == "proven":
            _merge_task_tally_into_totals(ms["proven"], tally)
        elif proof_bucket in ("in_review", "active"):
            _merge_task_tally_into_totals(ms["in_review"], tally)
        task_row = {
            "project_id": task_project,
            "task_id": task_id,
            "title": task.get("title"),
            "status": task.get("status"),
            "proof_bucket": proof_bucket,
            "milestone_id": milestone_id or None,
            "role": link.get("role"),
            "spend": tally.get("spend") or {},
            "outcomes": tally.get("outcomes") or {},
            "unit_cost": tally.get("unit_cost") or {},
            "verified_kpi_contribution": round(sum(
                float(k.get("verified_contribution") or 0.0)
                for k in (tally.get("kpis") or [])), 6),
            "kpis": tally.get("kpis") or [],
        }
        by_task.append(task_row)
        ms["by_task"].append(task_row)

    for bucket in (combined, proven, in_review):
        _finalize_economics_totals(bucket)
    milestone_rows = []
    for ms in by_milestone.values():
        for key in ("combined", "proven", "in_review"):
            _finalize_economics_totals(ms[key])
        milestone_rows.append(ms)
    milestone_rows.sort(key=lambda x: (x.get("milestone_id") is None,
                                      x.get("title") or ""))
    kpis = []
    for entry in kpi_index.values():
        if entry["verified_contribution"]:
            entry["unit_cost"]["cost_per_contribution_unit"] = round(
                entry["spend"]["cost_usd"] / entry["verified_contribution"], 6)
        kpis.append(entry)
    kpis.sort(key=lambda x: (-float(x["spend"]["cost_usd"] or 0.0),
                             x.get("project_id") or "", x.get("kpi_id") or ""))
    by_task.sort(key=lambda x: (-float((x.get("spend") or {}).get("cost_usd") or 0.0),
                                x.get("project_id") or "", x.get("task_id") or ""))

    return {
        "schema": "switchboard.deliverable_tally.v1",
        "project_id": project,
        "deliverable_id": deliverable_id,
        "board_id": deliverable.get("board_id"),
        "totals": {
            "combined": combined,
            "proven": proven,
            "in_review": in_review,
        },
        "by_milestone": milestone_rows,
        "by_task": by_task,
        "kpis": kpis,
    }


def bug_intake_policy() -> Dict[str, Any]:
    return json.loads(json.dumps(BUG_INTAKE_POLICY))


def _approval_payload(task: Dict[str, Any]) -> Dict[str, Any]:
    state = task.get("agent_state") or {}
    if not isinstance(state, dict):
        return {}
    candidates = [
        state.get("human_gate"),
        state.get("approval"),
        (state.get("governance") or {}).get("human_gate")
        if isinstance(state.get("governance"), dict) else None,
        (state.get("governance") or {}).get("approval")
        if isinstance(state.get("governance"), dict) else None,
        (state.get("bug_intake") or {}).get("conversion_gate")
        if isinstance(state.get("bug_intake"), dict) else None,
    ]
    for value in candidates:
        if isinstance(value, dict) and value:
            return value
    return {}


def _task_human_gate_state(task: Dict[str, Any]) -> Dict[str, Any]:
    raw = _approval_payload(task)
    required = bool(raw.get("required") or raw.get("approval_required")
                    or raw.get("needs_human"))
    approved_by = raw.get("approved_by") or raw.get("approver")
    status = str(raw.get("status") or "").strip().lower()
    approved = bool(
        raw.get("approved") is True
        or approved_by
        or status in set(BUG_INTAKE_POLICY["conversion_gate"]["approved_statuses"])
    )
    blocked = bool(required and not approved)
    return {
        "required": required,
        "approved": approved,
        "blocked": blocked,
        "reason": (
            raw.get("reason")
            or raw.get("approval_reason")
            or ("human approval required" if blocked else None)
        ),
        "status": (
            BUG_INTAKE_POLICY["conversion_gate"]["unapproved_status"]
            if blocked else (status or ("approved" if approved else "not_required"))
        ),
        "approved_by": approved_by,
        "approved_at": raw.get("approved_at") or raw.get("accepted_at"),
        "source_bug_task_id": raw.get("source_bug_task_id"),
        "target_workstream": raw.get("target_workstream"),
        "severity": raw.get("severity") or raw.get("severity_hint"),
        "policy": "bug_intake_human_gate.v1",
    }


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


def _reconcile_failure_class(code: str) -> str:
    return RECONCILE_FAILURE_CLASS_BY_CODE.get(
        _slug_token(code or ""), "failed_gate")


def _annotate_reconcile_finding(finding: Dict[str, Any]) -> Dict[str, Any]:
    failure_class = finding.get("failure_class") or _reconcile_failure_class(
        str(finding.get("code") or ""))
    detail = _failure_class_detail(failure_class) or {}
    annotated = dict(finding)
    annotated["failure_class"] = failure_class
    annotated["expected_signal"] = annotated.get(
        "expected_signal") or detail.get("expected_signal")
    return annotated


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


def _normalize_depends_on(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = value
    else:
        parsed = value
    if isinstance(parsed, str):
        raw_items = parsed.replace("\n", ",").replace(" ", ",").split(",")
    elif isinstance(parsed, list):
        raw_items = parsed
    else:
        raw_items = []
    out: List[str] = []
    seen = set()
    for item in raw_items:
        dep = str(item or "").strip().upper()
        if dep and dep not in seen:
            seen.add(dep)
            out.append(dep)
    return out


def _git_state_row(r: Optional[sqlite3.Row]) -> Dict[str, Any]:
    if not r:
        return {"branch": None, "head_sha": None, "pushed_at": None, "pr_number": None,
                "pr_url": None, "merged_sha": None, "merged_at": None,
                "in_main_content": False, "published_ref": None,
                "last_reconciled_at": None, "evidence": {}}
    d = dict(r)
    d["in_main_content"] = bool(d.get("in_main_content"))
    d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
    return d


def _offline_evidence_from_state(git_state: Dict[str, Any]) -> Dict[str, Any]:
    evidence = git_state.get("evidence") or {}
    offline = evidence.get("offline_evidence") if isinstance(evidence, dict) else None
    return offline if isinstance(offline, dict) else {}


def _valid_evidence_hash(value: str) -> bool:
    return bool(EVIDENCE_HASH_RE.fullmatch((value or "").strip()))


def _has_done_provenance(git_state: Dict[str, Any]) -> bool:
    return bool(git_state.get("merged_sha") or _offline_evidence_from_state(git_state))


def _provenance_summary(git_state: Dict[str, Any]) -> Dict[str, Any]:
    offline = _offline_evidence_from_state(git_state)
    if offline:
        return {
            "type": "offline_evidence",
            "terminal": True,
            "label": "Offline evidence",
            "verifier": offline.get("verifier"),
            "reviewed_at": offline.get("reviewed_at"),
            "artifact_url": offline.get("artifact_url"),
            "evidence_hash": offline.get("evidence_hash"),
        }
    if git_state.get("merged_sha"):
        return {
            "type": "github_pr_merged" if git_state.get("pr_number") else "default_branch_commit",
            "terminal": True,
            "label": "Merged code",
            "merged_sha": git_state.get("merged_sha"),
            "pr_number": git_state.get("pr_number"),
            "pr_url": git_state.get("pr_url"),
        }
    if git_state.get("pr_number") or git_state.get("pr_url"):
        return {
            "type": "github_pr_open",
            "terminal": False,
            "label": "PR evidence",
            "pr_number": git_state.get("pr_number"),
            "pr_url": git_state.get("pr_url"),
        }
    if git_state.get("head_sha"):
        return {
            "type": "branch_head",
            "terminal": False,
            "label": "Branch evidence",
            "head_sha": git_state.get("head_sha"),
        }
    return {"type": None, "terminal": False, "label": "No provenance"}


def _load_git_state(c: sqlite3.Connection, task_id: str) -> Dict[str, Any]:
    state = _git_state_row(c.execute("SELECT * FROM task_git_state WHERE task_id=?",
                                     (task_id,)).fetchone())
    state["provenance_type"] = _provenance_summary(state)["type"]
    return state


def _provenance_by_task(c: sqlite3.Connection, task_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Batch equivalent of _provenance_summary(_load_git_state(c, id)) for many tasks:
    one query for all task_git_state rows instead of one per task. This is the board
    N+1 fix (HARDEN-34) — the whole-board list needs provenance for every card's
    Done-proof badge, and doing it per-task was ~1 query/task."""
    if not task_ids:
        return {}
    by_id: Dict[str, sqlite3.Row] = {}
    chunk = 400  # stay well under SQLite's 999-variable limit
    for i in range(0, len(task_ids), chunk):
        batch = task_ids[i:i + chunk]
        placeholders = ",".join("?" * len(batch))
        for r in c.execute(
            f"SELECT * FROM task_git_state WHERE task_id IN ({placeholders})", batch
        ).fetchall():
            by_id[r["task_id"]] = r
    out: Dict[str, Dict[str, Any]] = {}
    for tid in task_ids:
        state = _git_state_row(by_id.get(tid))
        state["provenance_type"] = _provenance_summary(state)["type"]
        out[tid] = _provenance_summary(state)
    return out


def _active_task_claims_in(c: sqlite3.Connection, task_id: str,
                           now: Optional[float] = None) -> List[Dict[str, Any]]:
    now = time.time() if now is None else now
    rows = c.execute(
        "SELECT * FROM task_claims WHERE task_id=? AND status='active' "
        "AND expires_at>? ORDER BY claimed_at",
        (task_id, now),
    ).fetchall()
    return [{
        "claim_id": r["id"],
        "task_id": r["task_id"],
        "agent_id": r["agent_id"],
        "principal_id": r["principal_id"],
        "status": r["status"],
        "claimed_at": r["claimed_at"],
        "expires_at": r["expires_at"],
    } for r in rows]


def _parse_evidence(evidence: Any) -> Dict[str, Any]:
    if isinstance(evidence, dict):
        return dict(evidence)
    if not evidence:
        return {}
    if isinstance(evidence, str):
        try:
            parsed = json.loads(evidence)
            return parsed if isinstance(parsed, dict) else {"note": evidence}
        except Exception:
            return {"note": evidence}
    return {"value": evidence}


def _upsert_git_state(c: sqlite3.Connection, task_id: str,
                      updates: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    current = _load_git_state(c, task_id)
    evidence = dict(current.get("evidence") or {})
    if "evidence" in updates and isinstance(updates["evidence"], dict):
        evidence.update(updates.pop("evidence"))
    clean_updates = {k: v for k, v in updates.items() if v is not None}
    merged = {**current, **clean_updates}
    branch = merged.get("branch")
    head_sha = merged.get("head_sha")
    pushed_at = merged.get("pushed_at")
    pr_number = merged.get("pr_number")
    pr_url = merged.get("pr_url")
    merged_sha = merged.get("merged_sha")
    merged_at = merged.get("merged_at")
    in_main = 1 if merged.get("in_main_content") else 0
    published_ref = merged.get("published_ref")
    last_reconciled_at = merged.get("last_reconciled_at")
    c.execute(
        "INSERT INTO task_git_state(task_id, branch, head_sha, pushed_at, pr_number, pr_url, "
        "merged_sha, merged_at, in_main_content, published_ref, last_reconciled_at, "
        "evidence_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(task_id) DO UPDATE SET branch=excluded.branch, head_sha=excluded.head_sha, "
        "pushed_at=excluded.pushed_at, pr_number=excluded.pr_number, pr_url=excluded.pr_url, "
        "merged_sha=excluded.merged_sha, merged_at=excluded.merged_at, "
        "in_main_content=excluded.in_main_content, published_ref=excluded.published_ref, "
        "last_reconciled_at=excluded.last_reconciled_at, evidence_json=excluded.evidence_json, "
        "updated_at=excluded.updated_at",
        (task_id, branch, head_sha, pushed_at, pr_number, pr_url, merged_sha, merged_at,
         in_main, published_ref, last_reconciled_at, json.dumps(evidence, sort_keys=True), now),
    )
    return _load_git_state(c, task_id)


def _session_health_summary(session: Dict[str, Any]) -> Dict[str, Any]:
    health = session.get("health") or {}
    workspace = health.get("workspace") or {}
    return {
        "work_session_id": session.get("work_session_id"),
        "agent_id": session.get("agent_id"),
        "claim_id": session.get("claim_id"),
        "status": session.get("status"),
        "health_status": health.get("status"),
        "safe": health.get("safe"),
        "storage_mode": session.get("storage_mode"),
        "repo_role": session.get("repo_role"),
        "branch": session.get("branch") or workspace.get("branch"),
        "workspace_path": workspace.get("path"),
        "head_sha": session.get("head_sha") or workspace.get("head_sha"),
        "finding_count": health.get("finding_count", 0),
        "blocking_count": health.get("blocking_count", 0),
        "recommended_repair": health.get("recommended_repair"),
        "updated_at": session.get("updated_at"),
        "expires_at": session.get("expires_at"),
    }


def _task_session_health_in(c: sqlite3.Connection, task: Dict[str, Any],
                            project: str = DEFAULT_PROJECT,
                            active_claims: Optional[List[Dict[str, Any]]] = None,
                            git_state: Optional[Dict[str, Any]] = None,
                            now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    task_id = task.get("task_id") or ""
    rows = c.execute(
        "SELECT * FROM work_sessions WHERE task_id=? "
        "ORDER BY updated_at DESC, work_session_id",
        (task_id,),
    ).fetchall()
    sessions = [_work_session_row(row) for row in rows]
    current_sessions = [
        s for s in sessions
        if s.get("status") not in {"completed", "archived"}
    ]
    active_sessions = [
        s for s in current_sessions
        if s.get("status") == "active" and (
            not s.get("expires_at") or float(s.get("expires_at") or 0) > now
        )
    ]
    unsafe = [s for s in current_sessions if (s.get("health") or {}).get("status") == "unsafe"]
    warnings = [s for s in current_sessions if (s.get("health") or {}).get("status") == "warning"]
    findings: List[Dict[str, Any]] = []
    for session in unsafe + warnings:
        health = session.get("health") or {}
        for finding in health.get("findings") or []:
            findings.append({
                "code": finding.get("code"),
                "kind": "unsafe_session" if finding.get("blocking") else "session_warning",
                "work_session_id": session.get("work_session_id"),
                "agent_id": session.get("agent_id"),
                "task_id": task_id,
                "message": finding.get("message"),
                "failure_class": finding.get("failure_class"),
                "severity": finding.get("severity"),
                "blocking": bool(finding.get("blocking")),
                "repair": finding.get("repair"),
            })

    claims = active_claims if active_claims is not None else _active_task_claims_in(c, task_id)
    session_claim_ids = {s.get("claim_id") for s in sessions if s.get("claim_id")}
    for claim in claims or []:
        claim_id = claim.get("claim_id")
        if claim_id and claim_id not in session_claim_ids:
            findings.append({
                "code": "active_claim_without_work_session",
                "kind": "session_warning",
                "claim_id": claim_id,
                "agent_id": claim.get("agent_id"),
                "task_id": task_id,
                "message": "Active task claim is not bound to a Work Session.",
                "failure_class": "missing_data",
                "severity": "medium",
                "blocking": False,
                "repair": "Create/bind a Work Session for code work, or use a non-code policy profile.",
            })

    blocking = [f for f in findings if f.get("blocking")]
    nonblocking = [f for f in findings if not f.get("blocking")]
    status = (
        "unsafe" if blocking else
        "warning" if nonblocking else
        "healthy" if sessions else
        "no_sessions"
    )
    gs = git_state if git_state is not None else _load_git_state(c, task_id)
    return {
        "schema": TASK_SESSION_HEALTH_SCHEMA,
        "project_id": project,
        "task_id": task_id,
        "status": status,
        "safe": not blocking,
        "blocking": bool(blocking),
        "session_count": len(sessions),
        "current_session_count": len(current_sessions),
        "active_session_count": len(active_sessions),
        "unsafe_session_count": len(unsafe),
        "warning_session_count": len(warnings),
        "active_claim_count": len(claims or []),
        "findings": findings,
        "active_sessions": [_session_health_summary(s) for s in active_sessions],
        "latest_sessions": [_session_health_summary(s) for s in sessions[:5]],
        "pr_url": (gs or {}).get("pr_url"),
        "branch": (gs or {}).get("branch"),
        "head_sha": (gs or {}).get("head_sha"),
        "recommended_repair": (
            (blocking or nonblocking)[0].get("repair") if (blocking or nonblocking) else
            "No repair needed." if sessions else
            "No Work Session is bound yet."
        ),
        "checked_at": now,
    }


def list_tasks(workstream: Optional[str] = None, status: Optional[str] = None,
               assignee: Optional[str] = None, project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    q = "SELECT * FROM tasks WHERE 1=1"
    p: List[Any] = []
    if workstream:
        q += " AND workstream_id=?"; p.append(workstream)
    if status:
        q += " AND status=?"; p.append(status)
    if assignee:
        q += " AND assignee=?"; p.append(assignee)
    q += " ORDER BY sort_order"
    with _conn(project) as c:
        tasks = []
        for r in c.execute(q, p).fetchall():
            t = _task_row(r)
            t["provenance"] = _provenance_summary(_load_git_state(c, t["task_id"]))
            t["external_ci"] = _task_external_ci_summary_in(c, t["task_id"], project=project)
            t["publication"] = _task_publication_summary_in(c, t["task_id"])
            t["session_health"] = _task_session_health_in(c, t, project=project)
            tasks.append(t)
        return tasks


def list_tasks_for_board(project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """Slim, batched task list for the board/kanban and its rollups (HARDEN-34).

    Returns base task rows plus `provenance` (every card's Done-proof badge),
    with provenance loaded in ONE batched query. It deliberately skips the
    per-task external_ci / publication / session_health enrichment that full
    list_tasks() runs — the board never renders those (the task-detail modal
    re-fetches them via get_task). That turns the board's ~4-queries-per-task
    (≈1600 for a 400-task board, ~73s under swap) into 2 queries total.
    """
    with _conn(project) as c:
        rows = c.execute("SELECT * FROM tasks ORDER BY sort_order").fetchall()
        tasks = [_task_row(r) for r in rows]
        provenance = _provenance_by_task(c, [t["task_id"] for t in tasks])
        for t in tasks:
            t["provenance"] = provenance.get(t["task_id"])
        return tasks


def board_rollups(project: str = DEFAULT_PROJECT,
                  tasks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Compute board-level counts from live task rows, not seed metadata."""
    rows = tasks if tasks is not None else list_tasks(project=project)
    status_counts: Dict[str, int] = {}
    workstream_counts: Dict[str, int] = {}
    effort = 0.0
    for t in rows:
        status = t.get("status") or "Unknown"
        ws_id = t.get("_wsId") or t.get("workstream_id") or "UNKNOWN"
        status_counts[status] = status_counts.get(status, 0) + 1
        workstream_counts[ws_id] = workstream_counts.get(ws_id, 0) + 1
        raw_effort = t.get("effort_days")
        if raw_effort in (None, ""):
            continue
        try:
            effort += float(raw_effort)
        except (TypeError, ValueError):
            continue
    effort_value: Any = int(effort) if effort.is_integer() else round(effort, 2)
    return {
        "total_tasks": len(rows),
        "total_workstreams": len(workstream_counts),
        "total_effort_days": effort_value,
        "status_counts": dict(sorted(status_counts.items())),
        "workstream_counts": dict(sorted(workstream_counts.items())),
    }


def get_task(task_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    now = time.time()
    with _conn(project) as c:
        r = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not r:
            # Task ids are user-visible codes whose stored casing can be mixed
            # (CONTRACT-5b), while mission/deliverable callers normalize ids to
            # uppercase. Resolve case-insensitively, then re-read under the
            # canonical id so every sub-query (activity, git_state, claims, ...)
            # matches the stored casing.
            row = c.execute("SELECT task_id FROM tasks WHERE task_id=? COLLATE NOCASE",
                            (task_id,)).fetchone()
            if not row or row["task_id"] == task_id:
                return None
            return get_task(row["task_id"], project=project)
        t = _task_row(r)
        t["activity"] = [dict(a) | {"payload": _json_payload(a["payload"])}
                         for a in c.execute(
                             "SELECT * FROM activity WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]
        t["git_state"] = _load_git_state(c, task_id)
        t["provenance"] = _provenance_summary(t["git_state"])
        t["active_claims"] = _active_task_claims_in(c, task_id)
        t["identity"] = _task_identity_state_in(c, task_id, now)
        t["dependency_state"] = _dependency_state_in(c, t)
        t["human_gate"] = _task_human_gate_state(t)
        t["external_ci"] = _external_ci_review_gate(t, c=c, project=project)
        t["publication"] = _publication_review_gate(t, c=c, project=project)
        t["session_health"] = _task_session_health_in(
            c, t, project=project, active_claims=t["active_claims"], git_state=t["git_state"])
        s = c.execute("SELECT rationale FROM task_summaries WHERE task_id=?", (task_id,)).fetchone()
        if s:
            raw_rationale = s["rationale"]
            rationale_state = _rationale_state(raw_rationale, t, t["dependency_state"])
            t["rationale_state"] = rationale_state
            if rationale_state["stale"]:
                t["rationale_raw"] = raw_rationale
                t["rationale"] = None
            else:
                t["rationale"] = raw_rationale
        n = c.execute(
            "SELECT narration, source_fingerprint, generated_at FROM task_narrations "
            "WHERE task_id=?", (task_id,)).fetchone()
        if n:
            narration_state = _narration_state(dict(n), t)
            t["narration_state"] = narration_state
            if narration_state["stale"]:
                t["narration_raw"] = n["narration"]
                t["narration"] = None
            else:
                t["narration"] = n["narration"]
        _apply_terminal_done_view(t)
        _enrich_task_project_context(t, project=project)
        return t


def update_task(task_id: str, fields: Dict[str, Any], actor: str = "user",
                project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    sets, vals, changed = [], [], {}
    for k, v in fields.items():
        if k not in EDITABLE:
            continue
        if k == "is_blocking":
            v = 1 if v else 0
        if k == "depends_on":
            v = _normalize_depends_on(v)
            sets.append(f"{k}=?"); vals.append(json.dumps(v)); changed[k] = v
            continue
        sets.append(f"{k}=?"); vals.append(v); changed[k] = v
    if not sets:
        return get_task(task_id, project)
    if str(changed.get("status") or "").strip().lower() == "done":
        now = time.time()
        with _conn(project) as c:
            row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                return None
            git_state = _load_git_state(c, task_id)
            if not _has_done_provenance(git_state):
                payload = {
                    "requested_status": "Done",
                    "reason": "done_requires_merge_provenance",
                    "message": "Status Done requires GitHub/default-branch or offline evidence provenance.",
                }
                c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                          (task_id, actor, "task.done_blocked",
                           json.dumps(payload, sort_keys=True), now))
                task = _task_row(row)
                task["git_state"] = git_state
                task["error"] = "done_requires_merge_provenance"
                task["message"] = payload["message"]
                return task
    sets.append("updated_at=?"); vals.append(time.time())
    vals.append(task_id)
    with _conn(project) as c:
        cur = c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=?", vals)
        if cur.rowcount == 0:
            return None
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "edit", json.dumps(changed), time.time()))
    # NARRATE-2: enqueue CEO-narration only on a real status transition, never on cosmetic
    # edits — this is the cost guarantee. The drain job applies the trigger-status filter.
    if "status" in changed:
        enqueue_narration(task_id, status=str(changed.get("status") or ""),
                          reason="status_change", project=project)
    return get_task(task_id, project)


def add_comment(task_id: str, actor: str, text: str, kind: str = "comment",
                project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    now = time.time()
    with _conn(project) as c:
        if not c.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
            return None
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, kind, json.dumps({"text": text}), now))
        if _is_unbound_system_actor(actor):
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
    return get_task(task_id, project)


def create_task(data: Dict[str, Any], actor: str = "user",
                project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    ws = (data.get("workstream_id") or "").strip()
    title = (data.get("title") or "").strip()
    if not ws or not title:
        return None
    with _conn(project) as c:
        wsname = data.get("workstream_name")
        if not wsname:
            r = c.execute("SELECT workstream_name FROM tasks WHERE workstream_id=? LIMIT 1", (ws,)).fetchone()
            wsname = r[0] if r else ws
        ids = [row[0] for row in c.execute("SELECT task_id FROM tasks WHERE workstream_id=?", (ws,)).fetchall()]
        mx = 0
        for t in ids:
            tail = t.rsplit("-", 1)[-1]
            if tail.isdigit():
                mx = max(mx, int(tail))
        tid = f"{ws}-{mx + 1}"
        while c.execute("SELECT 1 FROM tasks WHERE task_id=?", (tid,)).fetchone():
            mx += 1
            tid = f"{ws}-{mx + 1}"
        order = c.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM tasks").fetchone()[0]
        now = time.time()
        c.execute(
            """INSERT INTO tasks (task_id, workstream_id, workstream_name, title, description,
                 owner_org, owner_person_or_role, assignee, phase, status, effort_days, duration_days,
                 start_date, finish_date, start_day, depends_on, entry_criteria, exit_criteria,
                 deliverable, risk_level, is_blocking, sort_order, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tid, ws, wsname, title, data.get("description"), data.get("owner_org"),
             data.get("owner_person_or_role"), data.get("assignee"), (data.get("phase") or "Build"),
             (data.get("status") or "Not Started"), data.get("effort_days"), data.get("duration_days"),
             data.get("start_date"), data.get("finish_date"), 0,
             json.dumps(_normalize_depends_on(data.get("depends_on"))),
             data.get("entry_criteria"), data.get("exit_criteria"),
             data.get("deliverable"), (data.get("risk_level") or "Medium"),
             1 if data.get("is_blocking") else 0, order, now, now))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (tid, actor, "create", json.dumps({"title": title}), now))
    # NARRATE-2: a newly created task is a meaningful transition — enqueue its first narration.
    enqueue_narration(tid, status=(data.get("status") or "Not Started"),
                      reason="create", project=project)
    return get_task(tid, project)


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
    contract = _external_ci_topology_contract(project)
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
                             project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    evidence = evidence or {}
    source_sha = (
        evidence.get("external_ci_source_sha")
        or evidence.get("source_sha")
        or evidence.get("head_sha")
        or (task.get("git_state") or {}).get("head_sha")
        or ""
    )
    if c is None:
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

    blocking = [f for f in findings if f.get("blocking")]
    ok = not blocking
    result = {
        "schema": MERGE_GATE_SCHEMA,
        "project": project,
        "task_id": task_id,
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
                             project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
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
    if c is None:
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


def create_principal(kind: str, display_name: str, token: str, scopes: List[str],
                     principal_id: Optional[str] = None,
                     project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    kind = validate_principal_kind(kind)
    if not kind:
        return {"error": "kind must be one of: " + ", ".join(sorted(VALID_PRINCIPAL_KINDS))}
    scopes, unknown = validate_principal_scopes(scopes)
    if unknown:
        return {"error": "unknown scope(s): " + ", ".join(unknown)}
    if not token:
        return {"error": "token required"}
    principal_id = principal_id or f"{kind}-{uuid.uuid4().hex[:12]}"
    display_name = (display_name or principal_id).strip()
    now = time.time()
    scopes_json = json.dumps(scopes, sort_keys=True)
    with _conn(project) as c:
        c.execute(
            "INSERT INTO principals(id, kind, display_name, project, scopes, token_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (principal_id, kind, display_name, project, scopes_json, hash_token(token), now),
        )
    return {"id": principal_id, "kind": kind, "display_name": display_name,
            "project": project, "scopes": scopes, "created_at": now}


def _principal_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    out = dict(row)
    out["scopes"] = json.loads(out.get("scopes") or "[]")
    return out


def public_principal_record(principal: Dict[str, Any], project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    out = {
        "id": principal.get("id"),
        "kind": principal.get("kind"),
        "display_name": principal.get("display_name"),
        "project": principal.get("project"),
        "scopes": list(principal.get("scopes") or []),
        "created_at": principal.get("created_at"),
        "revoked_at": principal.get("revoked_at"),
    }
    pid = principal.get("id") or ""
    if pid:
        out["effective_scopes"] = effective_principal_scopes(
            project, pid, list(principal.get("scopes") or []))
        out["project_roles"] = principal_project_roles(project, pid)
    else:
        out["effective_scopes"] = list(principal.get("scopes") or [])
        out["project_roles"] = []
    return out


def list_principals(project: str = DEFAULT_PROJECT, include_revoked: bool = False,
                    kind: str = "") -> List[Dict[str, Any]]:
    filters = []
    args: List[Any] = []
    if not include_revoked:
        filters.append("revoked_at IS NULL")
    normalized_kind = validate_principal_kind(kind) if kind else ""
    if kind and not normalized_kind:
        return []
    if normalized_kind:
        filters.append("kind=?")
        args.append(normalized_kind)
    q = "SELECT * FROM principals"
    if filters:
        q += " WHERE " + " AND ".join(filters)
    q += " ORDER BY created_at DESC, id"
    with _conn(project) as c:
        rows = c.execute(q, args).fetchall()
    return [public_principal_record(_principal_from_row(row), project=project) for row in rows]


def get_principal_by_id(principal_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    if not principal_id:
        return None
    with _conn(project) as c:
        row = c.execute("SELECT * FROM principals WHERE id=?", (principal_id,)).fetchone()
    return _principal_from_row(row) if row else None


def get_principal_by_token(project: str, token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    with _conn(project) as c:
        row = c.execute("SELECT * FROM principals WHERE token_hash=?",
                        (hash_token(token),)).fetchone()
    return _principal_from_row(row) if row else None


def password_login_count(project: str = DEFAULT_PROJECT) -> int:
    with _conn(project) as c:
        return int(c.execute("SELECT COUNT(*) FROM principal_passwords").fetchone()[0] or 0)


def set_principal_password(principal_id: str, login: str, password_hash: str,
                           must_rotate: bool = False,
                           project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    login = (login or "").strip().lower()
    if not login:
        return {"error": "login required"}
    if not principal_id:
        return {"error": "principal_id required"}
    principal = get_principal_by_id(principal_id, project=project)
    if not principal:
        return {"error": "principal not found"}
    now = time.time()
    with _conn(project) as c:
        c.execute(
            "INSERT INTO principal_passwords(login, principal_id, password_hash, "
            "password_updated_at, must_rotate, created_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(login) DO UPDATE SET principal_id=excluded.principal_id, "
            "password_hash=excluded.password_hash, password_updated_at=excluded.password_updated_at, "
            "must_rotate=excluded.must_rotate",
            (login, principal_id, password_hash, now, 1 if must_rotate else 0, now),
        )
    return {"login": login, "principal_id": principal_id,
            "password_updated_at": now, "must_rotate": bool(must_rotate)}


def get_password_login(login: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    login = (login or "").strip().lower()
    if not login:
        return None
    with _conn(project) as c:
        row = c.execute(
            "SELECT pp.*, p.kind, p.display_name, p.project, p.scopes, p.revoked_at "
            "FROM principal_passwords pp JOIN principals p ON p.id=pp.principal_id "
            "WHERE pp.login=?",
            (login,),
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["scopes"] = json.loads(out.get("scopes") or "[]")
    out["must_rotate"] = bool(out.get("must_rotate"))
    return out


def create_password_principal(login: str, display_name: str, password_hash: str,
                              scopes: List[str], principal_id: Optional[str] = None,
                              kind: str = "user",
                              project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    token_seed = f"password-principal:{uuid.uuid4().hex}"
    principal = create_principal(
        kind=kind,
        display_name=display_name or login,
        token=token_seed,
        scopes=scopes,
        principal_id=principal_id,
        project=project,
    )
    password_row = set_principal_password(
        principal["id"], login, password_hash, project=project)
    principal["login"] = password_row.get("login")
    return principal


def create_auth_session(principal_id: str, session_token: str, ttl_seconds: int,
                        user_agent: str = "", ip: str = "",
                        project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    if not principal_id:
        return {"error": "principal_id required"}
    if not session_token:
        return {"error": "session_token required"}
    now = time.time()
    ttl = max(60, int(ttl_seconds or 0))
    session_id = f"sess-{uuid.uuid4().hex}"
    expires_at = now + ttl
    with _conn(project) as c:
        c.execute(
            "INSERT INTO auth_sessions(session_id, principal_id, project, session_hash, "
            "created_at, expires_at, last_seen_at, user_agent, ip) VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, principal_id, project, hash_token(session_token), now,
             expires_at, now, user_agent or None, ip or None),
        )
    return {"session_id": session_id, "principal_id": principal_id,
            "project": project, "created_at": now, "expires_at": expires_at}


def get_principal_by_session(project: str, session_token: str) -> Optional[Dict[str, Any]]:
    if not session_token:
        return None
    now = time.time()
    with _conn(project) as c:
        row = c.execute(
            "SELECT p.*, s.session_id, s.expires_at, s.revoked_at AS session_revoked_at "
            "FROM auth_sessions s JOIN principals p ON p.id=s.principal_id "
            "WHERE s.session_hash=? AND s.project=?",
            (hash_token(session_token), project),
        ).fetchone()
        if not row:
            return None
        if row["session_revoked_at"] or row["revoked_at"] or float(row["expires_at"] or 0) <= now:
            return None
        c.execute("UPDATE auth_sessions SET last_seen_at=? WHERE session_id=?",
                  (now, row["session_id"]))
    out = _principal_from_row(row)
    out["session_id"] = row["session_id"]
    out["session_expires_at"] = row["expires_at"]
    return out


def get_principal_by_session_any_project(session_token: str) -> Optional[Dict[str, Any]]:
    """Resolve a human session across projects for explicit cross-project role grants.

    Sessions are stored in the project where the human logged in. Project role grants live in
    the central registry, so a project owner can be granted into a newly-created project without
    forcing a second login.
    """
    if not session_token:
        return None
    for project in project_ids():
        principal = get_principal_by_session(project, session_token)
        if principal:
            principal["home_project"] = project
            return principal
    return None


def revoke_auth_session(session_token: str, project: str = DEFAULT_PROJECT) -> bool:
    if not session_token:
        return False
    with _conn(project) as c:
        cur = c.execute(
            "UPDATE auth_sessions SET revoked_at=? "
            "WHERE session_hash=? AND project=? AND revoked_at IS NULL",
            (time.time(), hash_token(session_token), project),
        )
        return cur.rowcount > 0


def revoke_principal_sessions(principal_id: str, project: str = DEFAULT_PROJECT) -> int:
    with _conn(project) as c:
        cur = c.execute(
            "UPDATE auth_sessions SET revoked_at=? "
            "WHERE principal_id=? AND project=? AND revoked_at IS NULL",
            (time.time(), principal_id, project),
        )
        return cur.rowcount


def revoke_principal(principal_id: str, project: str = DEFAULT_PROJECT) -> bool:
    with _conn(project) as c:
        cur = c.execute("UPDATE principals SET revoked_at=? WHERE id=?",
                        (time.time(), principal_id))
        return cur.rowcount > 0


def revoke_principal_token(principal_id: str, project: str = DEFAULT_PROJECT,
                           actor: str = "system") -> Dict[str, Any]:
    principal = get_principal_by_id(principal_id, project=project)
    if not principal:
        return {"error": "principal not found"}
    if principal.get("project") not in (project, "*"):
        return {"error": "principal is not valid for this project"}
    already_revoked = bool(principal.get("revoked_at"))
    revoked = revoke_principal(principal_id, project=project)
    session_count = revoke_principal_sessions(principal_id, project=project)
    updated = get_principal_by_id(principal_id, project=project) or principal
    public = public_principal_record(updated, project=project)
    append_activity(
        "access.token_revoked",
        actor,
        {"principal": public, "sessions_revoked": session_count,
         "already_revoked": already_revoked},
        task_id=None,
        project=project,
    )
    return {"revoked": bool(revoked), "already_revoked": already_revoked,
            "sessions_revoked": session_count, "principal": public}


def protocol_envelope() -> Dict[str, Any]:
    return json.loads(json.dumps(PROTOCOL_ENVELOPE))


def check_protocol_compatibility(advertised: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not advertised:
        return {"compatible": True, "mode": "legacy_assumed",
                "warnings": ["agent did not advertise protocol; treating as pre-PROTO-2"]}
    version = advertised.get("version") or advertised.get("ixp_version")
    supported = PROTOCOL_ENVELOPE["compatible_versions"]
    if version not in supported:
        return {"compatible": False, "mode": "reject",
                "reason": f"unsupported protocol version {version!r}; supported={supported}"}
    return {"compatible": True, "mode": "exact", "version": version,
            "profile": advertised.get("profile")}


def register_agent(agent_id: str, runtime: str, model: str = "", lane: str = "",
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
    if not row:
        return {
            "agent_id": agent_id,
            "status": "unreachable",
            "reason": "not_registered",
            "reachable": False,
            "message": "No active or historical registration exists for this agent_id.",
        }
    presence = _presence_row(row, now=now)
    delivery = {
        "agent_id": agent_id,
        "runtime": presence.get("runtime"),
        "lane": presence.get("lane"),
        "task_id": presence.get("task_id"),
        "heartbeat_at": presence.get("heartbeat_at"),
        "expires_at": presence.get("expires_at"),
        "ttl_s": presence.get("ttl_s"),
    }
    if presence.get("stale"):
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
    return delivery


def _is_unbound_system_actor(actor: str) -> bool:
    actor = (actor or "").strip()
    return actor in {"env-mcp-token", "env-auth-token"} or (
        actor.startswith("env-") and actor.endswith("-token")
    )


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


def resolve_write_actor(actor: str,
                        project: str = DEFAULT_PROJECT,
                        task_id: str = "",
                        agent_id: str = "",
                        system_actor: str = "",
                        system_reason: str = "",
                        principal_id: str = "") -> Dict[str, Any]:
    """Resolve a public write actor, binding shared env tokens before mutation.

    Compatibility env tokens are intentionally broad system principals. They
    must not leave task/activity rows authored as a naked `env-*-token`: callers
    either bind them to a live registered agent, or declare an explicit system
    actor and reason that remains audit-visible.
    """
    now = time.time()
    actor = (actor or "").strip() or "unknown"
    task_id = (task_id or "").strip()
    agent_id = (agent_id or "").strip()
    system_actor = (system_actor or "").strip()
    system_reason = (system_reason or "").strip()
    if not _is_unbound_system_actor(actor):
        return {"ok": True, "actor": actor, "binding": "principal", "principal_id": principal_id}

    base_error = {
        "ok": False,
        "error": "shared_token_requires_bound_actor",
        "failure_class": "unbound_identity",
        "expected_signal": FAIL_FIX_FAILURE_CLASSES["unbound_identity"]["expected_signal"],
        "principal_actor": actor,
        "principal_id": principal_id,
        "task_id": task_id or None,
        "remediation": [
            "Pass agent_id for a live registered agent before mutating task state.",
            "Or pass system_actor plus system_reason for deliberate automation/system writes.",
            "Register/heartbeat the runtime first if this is agent work.",
        ],
    }

    if system_actor:
        if _is_unbound_system_actor(system_actor):
            return {
                **base_error,
                "error": "system_actor_must_be_explicit",
                "message": "system_actor must name the automation, not the shared env token.",
            }
        if not system_reason:
            return {
                **base_error,
                "error": "system_reason_required",
                "message": "system_actor writes through a shared token require system_reason.",
            }
        return {
            "ok": True,
            "actor": system_actor,
            "binding": "explicit_system_actor",
            "principal_actor": actor,
            "principal_id": principal_id,
            "system_reason": system_reason,
        }

    with _conn(project) as c:
        if agent_id:
            presence = _active_agent_presence_in(c, agent_id, now)
            if not presence:
                return {
                    **base_error,
                    "error": "agent_not_registered",
                    "agent_id": agent_id,
                    "message": "agent_id is not currently registered/heartbeat-active.",
                }
            presence_task = (presence.get("task_id") or "").strip()
            if task_id and presence_task and presence_task != task_id:
                return {
                    **base_error,
                    "error": "agent_registered_on_different_task",
                    "agent_id": agent_id,
                    "registered_task_id": presence_task,
                    "message": "agent_id is live but not bound to this task.",
                }
            return {
                "ok": True,
                "actor": agent_id,
                "binding": "registered_agent",
                "principal_actor": actor,
                "principal_id": principal_id,
                "agent_id": agent_id,
            }
        if task_id:
            active_agents = _active_agent_ids_for_task(c, task_id, now)
            if len(active_agents) == 1:
                return {
                    "ok": True,
                    "actor": active_agents[0],
                    "binding": "inferred_registered_agent",
                    "principal_actor": actor,
                    "principal_id": principal_id,
                    "agent_id": active_agents[0],
                }
            if len(active_agents) > 1:
                return {
                    **base_error,
                    "error": "agent_id_required",
                    "active_agents": active_agents,
                    "message": "multiple live agents are bound to this task; pass agent_id.",
                }
    return {
        **base_error,
        "message": "shared-token writes require a bound live agent or explicit system actor/reason.",
    }


def write_binding_activity_payload(binding: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "binding": binding.get("binding"),
        "actor": binding.get("actor"),
        "agent_id": binding.get("agent_id"),
        "principal_actor": binding.get("principal_actor"),
        "principal_id": binding.get("principal_id"),
        "system_reason": binding.get("system_reason"),
    }


def claim_binding_target(claim_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    claim_id = (claim_id or "").strip()
    if not claim_id:
        return {}
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM task_claims WHERE id=?", (claim_id,)).fetchone()
    if not row:
        return {}
    return {
        "claim_id": row["id"],
        "task_id": row["task_id"],
        "agent_id": row["agent_id"],
        "active": row["status"] == "active" and float(row["expires_at"] or 0) > now,
        "principal_id": row["principal_id"],
    }


def _identity_risk_window_s() -> int:
    try:
        return max(60, int(os.environ.get("PM_IDENTITY_RISK_WINDOW_S", "1800")))
    except (TypeError, ValueError):
        return 1800


IDENTITY_RISK_WINDOW_S = _identity_risk_window_s()


def _task_identity_state_in(c: sqlite3.Connection, task_id: str,
                            now: float, window_s: int = IDENTITY_RISK_WINDOW_S) -> Dict[str, Any]:
    """Summarize whether a task has recent unbound runtime activity.

    Registered heartbeats are a liveness signal, not the only evidence of work.
    A shared-token write without a bound live agent means another runtime may be
    visibly active to the human while invisible to Switchboard coordination.
    """
    active_agents = _active_agent_ids_for_task(c, task_id, now)
    cutoff = now - max(60, int(window_s or IDENTITY_RISK_WINDOW_S))
    rows = c.execute(
        "SELECT id, actor, payload, created_at FROM activity "
        "WHERE task_id=? AND kind='principal.unbound_write' AND created_at>=? "
        "ORDER BY created_at DESC LIMIT 5",
        (task_id, cutoff),
    ).fetchall()
    recent = []
    for row in rows:
        payload = _json_payload(row["payload"])
        recent.append({
            "activity_id": row["id"],
            "actor": (payload or {}).get("actor") or row["actor"],
            "created_at": row["created_at"],
            "reason": (payload or {}).get("reason") or "principal.unbound_write",
        })
    state = {
        "active_agents": active_agents,
        "recent_unbound_activity": recent,
        "risk_window_seconds": max(60, int(window_s or IDENTITY_RISK_WINDOW_S)),
        "takeover_safe": True,
        "status": "clear",
    }
    if recent and not active_agents:
        state.update({
            "status": "unbound_live_runtime_possible",
            "takeover_safe": False,
            "reason": "recent_unbound_activity_without_active_registration",
            "message": (
                "Recent task activity came from a shared system principal, but no "
                "live agent session is registered on this task. Another runtime may "
                "be active outside Switchboard identity binding."
            ),
            "remediation": [
                "Ask the visible runtime to run register_agent and drain its inbox.",
                "Bind/re-register the runtime under its intended agent_id.",
                "Use an explicit human override only after confirming takeover is safe.",
            ],
        })
    elif recent:
        state.update({
            "status": "bound_after_unbound_activity",
            "reason": "recent_unbound_activity_with_active_registration",
        })
    return state


def _identity_takeover_risk_in(c: sqlite3.Connection, task_id: str,
                               now: float) -> Optional[Dict[str, Any]]:
    state = _task_identity_state_in(c, task_id, now)
    if state.get("takeover_safe"):
        return None
    return {
        "reason": "identity_unknown_recent_activity",
        "task_id": task_id,
        "identity": state,
        "message": (
            "Recent unbound activity exists on this task, but no active registered "
            "agent is bound to it. Refusing takeover without explicit override."
        ),
    }


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


def _wake_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["selector"] = _json_obj(d.pop("selector_json", "{}"), {})
    d["policy"] = _json_obj(d.pop("policy_json", "{}"), {})
    d["result"] = _json_obj(d.pop("result_json", "{}"), {})
    return d


def _normalize_runner_control(control: Dict[str, Any], host_id: str) -> Dict[str, Any]:
    """Fail closed on T3 claims.

    A session may advertise runner_kill only when it is both host-owned and explicitly
    managed by a supervisor/process handle. Unmanaged sessions can still be listed, but they
    cannot make the UI/API show a kill button.
    """
    raw = dict(control or {})
    managed = bool(
        raw.get("managed_process")
        or raw.get("managed")
        or raw.get("supervised")
        or str(raw.get("tier") or "").upper() == "T3"
    )
    runner_kill = bool(raw.get("runner_kill")) and managed and bool(host_id)
    runner_restart = False  # fail closed until supervisor restart is implemented end-to-end
    raw["managed_process"] = managed
    raw["runner_kill"] = runner_kill
    raw["runner_restart"] = runner_restart
    if runner_kill:
        raw.setdefault("tier", "T3")
    return raw


def _runner_available_actions(session: Dict[str, Any]) -> List[str]:
    control = session.get("control") or {}
    metadata = session.get("metadata") or {}
    status = str(session.get("status") or "").lower()
    if session.get("stale") or status in {"exited", "killed", "failed", "completed"}:
        return []
    actions: List[str] = []
    has_host = bool(session.get("host_id"))
    if control.get("managed_process") and session.get("host_id"):
        actions.extend(["health", "snapshot"])
    if has_host and (metadata.get("log_path") or control.get("runner_logs")):
        actions.append("logs")
    if has_host and control.get("runner_open"):
        actions.append("open")
    if control.get("runner_kill"):
        actions.append("kill")
    if control.get("runner_restart"):
        actions.append("restart")
    return sorted(dict.fromkeys(actions))


def _runner_control_capabilities(session: Dict[str, Any]) -> Dict[str, str]:
    available = set(session.get("available_actions") or [])
    return {action: ("supported" if action in available else "not_supported")
            for action in sorted(RUNNER_CONTROL_ACTIONS)}


def _runner_environment(session: Dict[str, Any], now: float) -> Dict[str, Any]:
    metadata = session.get("metadata") or {}
    snapshot = session.get("last_snapshot") or {}
    status = "stale" if session.get("stale") else (session.get("status") or "unknown")
    started_at = session.get("started_at")
    uptime = None
    if started_at:
        try:
            uptime = max(0.0, now - float(started_at))
        except (TypeError, ValueError):
            uptime = None
    last_result = (
        metadata.get("last_result")
        or snapshot.get("last_result")
        or snapshot.get("result")
        or {}
    )
    failure_reason = (
        metadata.get("failure_reason")
        or metadata.get("last_error")
        or snapshot.get("failure_reason")
        or snapshot.get("error")
        or (last_result.get("error") if isinstance(last_result, dict) else "")
    )
    return {
        "status": status,
        "uptime_seconds": uptime,
        "failure_reason": failure_reason or None,
        "last_command": metadata.get("command") or session.get("command"),
        "last_result": last_result or None,
        "log_tail": _text_tail(snapshot.get("log_tail") or metadata.get("log_tail") or ""),
        "log_path": metadata.get("log_path"),
        "capabilities": _runner_control_capabilities(session),
    }


def _runner_session_row(row: sqlite3.Row, now: Optional[float] = None,
                        include_claim: bool = False,
                        c: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    d = dict(row)
    ttl_s = d.get("heartbeat_ttl_s") or 60
    expires_at = (d.get("heartbeat_at") or 0) + ttl_s
    d["control"] = _json_obj(d.pop("control_json", "{}"), {})
    d["metadata"] = _json_obj(d.pop("metadata_json", "{}"), {})
    d["last_snapshot"] = _json_obj(d.pop("last_snapshot_json", "{}"), {})
    d["expires_at"] = expires_at
    d["stale"] = now >= expires_at
    d["available_actions"] = _runner_available_actions(d)
    d["environment"] = _runner_environment(d, now)
    if include_claim and c is not None and d.get("claim_id"):
        claim = c.execute("SELECT * FROM task_claims WHERE id=?", (d["claim_id"],)).fetchone()
        d["claim"] = dict(claim) if claim else None
    return d


def work_session_contract(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    topology = get_project_repo_topology(project)
    roles = sorted((topology.get("roles") or {}).keys())
    policy_profiles = get_session_policy_profiles(project)
    return {
        "schema": WORK_SESSION_SCHEMA,
        "project": project,
        "purpose": (
            "Bind agent code work to an explicit project, repo role, branch, workspace path, "
            "hygiene state, and lifecycle before claim/complete/merge gates enforce it."
        ),
        "lifecycle_states": sorted(WORK_SESSION_STATUSES),
        "storage_modes": sorted(WORK_SESSION_STORAGE_MODES),
        "dirty_statuses": sorted(WORK_SESSION_DIRTY_STATUSES),
        "repo_roles": roles,
        "policy_profiles": policy_profiles,
        "required_for_modes": {
            "worktree": ["worktree_path"],
            "clone": ["clone_path"],
            "external": [],
        },
        "managed_workspace": {
            "schema": MANAGED_WORK_SESSION_SCHEMA,
            "storage_modes": ["worktree", "clone"],
            "default_workspace_root": _managed_workspace_root(project, {}),
            "archive_tool": "archive_work_session_workspace",
        },
        "health": {
            "session_schema": WORK_SESSION_HEALTH_SCHEMA,
            "task_schema": TASK_SESSION_HEALTH_SCHEMA,
            "tools": ["get_work_session_health", "list_session_health"],
            "rest": [
                "GET /ixp/v1/work_sessions/{work_session_id}/health",
                "GET /ixp/v1/session_health",
            ],
            "blocking_statuses": ["unsafe"],
            "warning_statuses": ["warning", "no_sessions"],
        },
        "fail_closed_rules": [
            "unknown project ids are rejected",
            "task_id must exist when supplied",
            "repo_role must exist in the project repo_topology",
            "storage_mode, status, and dirty_status must be recognized values",
            "worktree and clone sessions require their matching path",
            "managed workspace paths must stay inside workspace_root",
            "JSON fields must decode to their expected object/list shapes",
            "session_token is never stored raw; only session_token_hash may persist",
        ],
        "audit_events": [
            "work_session.created",
            "work_session.updated",
            "work_session.completed",
            "work_session.expired",
            "work_session.managed_created",
            "work_session.workspace_archived",
        ],
    }


def _work_session_json(value: Any, default: Any, expected: type, field: str) -> Tuple[Any, str]:
    if value in (None, ""):
        return copy.deepcopy(default), ""
    if isinstance(value, expected):
        return copy.deepcopy(value), ""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return copy.deepcopy(default), f"{field} must be valid JSON"
        if isinstance(parsed, expected):
            return parsed, ""
    expected_name = "array" if expected is list else "object"
    return copy.deepcopy(default), f"{field} must be a JSON {expected_name}"


def _work_session_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["hygiene"] = _json_obj(d.pop("hygiene_json", "{}"), {})
    d["file_leases"] = _json_obj(d.pop("file_leases_json", "[]"), [])
    d["resource_leases"] = _json_obj(d.pop("resource_leases_json", "[]"), [])
    d["env"] = _json_obj(d.pop("env_json", "{}"), {})
    d["schema"] = WORK_SESSION_SCHEMA
    d["session_token_hash_present"] = bool(d.pop("session_token_hash", None))
    d["health"] = _work_session_health(d)
    return d


def _session_health_finding(code: str, message: str, failure_class: str,
                            severity: str = "high", blocking: bool = True,
                            repair: str = "", **details: Any) -> Dict[str, Any]:
    detail = FAIL_FIX_FAILURE_CLASSES.get(failure_class) or {}
    return {
        "code": code,
        "message": message,
        "failure_class": failure_class,
        "severity": severity,
        "blocking": bool(blocking),
        "expected_signal": detail.get("expected_signal"),
        "repair": repair or None,
        **details,
    }


def _work_session_health(session: Dict[str, Any],
                         now: Optional[float] = None) -> Dict[str, Any]:
    """Summarize whether a Work Session is safe for humans/coordinators to trust."""
    now = time.time() if now is None else now
    findings: List[Dict[str, Any]] = []
    session = dict(session or {})
    work_session_id = session.get("work_session_id")
    status = (session.get("status") or "").strip().lower()
    dirty = (session.get("dirty_status") or "unknown").strip().lower()
    path = session.get("worktree_path") or session.get("clone_path") or ""
    preflight = ((session.get("hygiene") or {}).get("repo_preflight") or {})

    if status == "active" and session.get("expires_at") and float(session.get("expires_at") or 0) < now:
        findings.append(_session_health_finding(
            "expired_active_session",
            "Work Session is active but its lease/expiry timestamp is in the past.",
            "stale_branch",
            repair="Renew the session/claim or archive the abandoned workspace.",
            work_session_id=work_session_id,
        ))
    if status in {"blocked", "expired"}:
        findings.append(_session_health_finding(
            f"{status}_work_session",
            f"Work Session status is {status}.",
            "failed_gate",
            repair="Repair the recorded blocker or create a fresh managed Work Session.",
            work_session_id=work_session_id,
        ))
    if status == "active" and not path:
        findings.append(_session_health_finding(
            "work_session_missing_path",
            "Active Work Session has no worktree_path or clone_path.",
            "missing_data",
            repair="Bind the session to a workspace path or create a managed Work Session.",
            work_session_id=work_session_id,
        ))
    if dirty == "dirty":
        findings.append(_session_health_finding(
            "dirty_work_session",
            "Work Session reports a dirty worktree.",
            "failed_gate",
            repair="Commit, stash, or clean the workspace, then rerun preflight.",
            work_session_id=work_session_id,
        ))
    elif dirty == "unknown" and status == "active":
        findings.append(_session_health_finding(
            "unknown_dirty_status",
            "Active Work Session has not recorded a clean/dirty verdict.",
            "missing_data",
            severity="medium",
            blocking=False,
            repair="Run preflight_work_session to refresh repo hygiene.",
            work_session_id=work_session_id,
        ))
    conflict_count = int(session.get("conflict_marker_count") or 0)
    if conflict_count > 0:
        findings.append(_session_health_finding(
            "conflict_markers",
            f"Work Session reports {conflict_count} file(s) with conflict markers.",
            "failed_gate",
            repair="Resolve conflict markers, rerun tests, then rerun preflight.",
            work_session_id=work_session_id,
            conflict_marker_count=conflict_count,
        ))

    if preflight:
        verdict = (preflight.get("verdict") or "").strip().lower()
        if verdict == "deny" or preflight.get("ok") is False:
            findings.append(_session_health_finding(
                "work_session_preflight_failed",
                "Recorded repo preflight is not clean.",
                "failed_gate",
                repair="Repair the preflight findings and rerun preflight_work_session.",
                work_session_id=work_session_id,
                preflight_verdict=verdict or None,
            ))
        elif verdict == "warn":
            findings.append(_session_health_finding(
                "work_session_preflight_warn",
                "Recorded repo preflight has warnings.",
                "failed_gate",
                severity="medium",
                blocking=False,
                repair="Review warnings before merge or completion.",
                work_session_id=work_session_id,
                preflight_verdict=verdict,
            ))
        for finding in preflight.get("findings") or []:
            code = str(finding.get("code") or "preflight_finding")
            blocking = bool(finding.get("blocking", True))
            severity = str(finding.get("severity") or ("high" if blocking else "medium"))
            failure_class = (
                "stale_branch" if code == "stale_base"
                else "failed_gate" if blocking
                else "missing_data"
            )
            findings.append(_session_health_finding(
                code,
                str(finding.get("message") or code),
                failure_class,
                severity=severity,
                blocking=blocking,
                repair="Repair the repo preflight finding and rerun preflight_work_session.",
                work_session_id=work_session_id,
                preflight_finding=finding,
            ))
    elif status == "active":
        findings.append(_session_health_finding(
            "missing_work_session_preflight",
            "Active Work Session has no recorded repo preflight.",
            "missing_data",
            severity="medium",
            blocking=False,
            repair="Run preflight_work_session before completion or merge.",
            work_session_id=work_session_id,
        ))

    # Deduplicate when a failed preflight finding mirrors explicit dirty/conflict state.
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for finding in findings:
        key = (finding.get("code"), finding.get("work_session_id"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    blocking = [f for f in deduped if f.get("blocking")]
    warnings = [f for f in deduped if not f.get("blocking")]
    status_value = "unsafe" if blocking else ("warning" if warnings else "healthy")
    return {
        "schema": WORK_SESSION_HEALTH_SCHEMA,
        "work_session_id": work_session_id,
        "project_id": session.get("project_id"),
        "task_id": session.get("task_id"),
        "agent_id": session.get("agent_id"),
        "status": status_value,
        "safe": not blocking,
        "blocking": bool(blocking),
        "finding_count": len(deduped),
        "blocking_count": len(blocking),
        "warning_count": len(warnings),
        "findings": deduped,
        "workspace": {
            "storage_mode": session.get("storage_mode"),
            "path": path or None,
            "branch": session.get("branch"),
            "repo_role": session.get("repo_role"),
            "repo": session.get("repo"),
            "head_sha": session.get("head_sha"),
            "base_sha": session.get("base_sha"),
            "upstream": session.get("upstream"),
        },
        "recommended_repair": (
            (blocking or warnings)[0].get("repair") if (blocking or warnings) else
            "No repair needed."
        ),
        "checked_at": now,
    }


def _validate_work_session_payload(payload: Dict[str, Any], project: str,
                                   partial: bool = False) -> Tuple[Dict[str, Any], List[str]]:
    errors: List[str] = []
    if not has_project(project):
        return {}, [f"unknown project: {project}"]

    topology = get_project_repo_topology(project)
    roles = topology.get("roles") or {}
    normalized: Dict[str, Any] = {}

    def text(key: str, default: str = "") -> str:
        if partial and key not in payload:
            return ""
        return str(payload.get(key, default) or "").strip()

    repo_role = text("repo_role", "canonical")
    if not partial or "repo_role" in payload:
        if repo_role not in roles:
            errors.append("repo_role must exist in repo_topology.roles")
        normalized["repo_role"] = repo_role
        role = roles.get(repo_role) or {}
        normalized["repo"] = text("repo", role.get("repo") or "")
        normalized["default_branch"] = text("default_branch", role.get("default_branch") or "")

    for key in (
        "work_session_id", "task_id", "claim_id", "agent_id", "runtime", "branch", "upstream",
        "base_sha", "head_sha", "worktree_path", "clone_path", "policy_profile",
        "principal_id",
    ):
        if not partial or key in payload:
            normalized[key] = text(key)

    if not partial or "agent_id" in payload:
        if not normalized.get("agent_id"):
            errors.append("agent_id required")

    task_id = normalized.get("task_id") if (not partial or "task_id" in payload) else None
    if task_id:
        task = get_task(task_id, project=project)
        if not task:
            errors.append("task_id must exist in project")

    claim_id = normalized.get("claim_id") if (not partial or "claim_id" in payload) else None
    if claim_id:
        with _conn(project) as c:
            row = c.execute("SELECT task_id, agent_id FROM task_claims WHERE id=?",
                            (claim_id,)).fetchone()
        if not row:
            errors.append("claim_id must exist in project")
        else:
            if task_id and row["task_id"] != task_id:
                errors.append("claim_id must belong to task_id")
            agent = normalized.get("agent_id")
            if agent and row["agent_id"] != agent:
                errors.append("claim_id must belong to agent_id")

    if not partial or "storage_mode" in payload:
        storage_mode = text("storage_mode", "worktree").lower()
        if storage_mode not in WORK_SESSION_STORAGE_MODES:
            errors.append("storage_mode must be one of: " + ", ".join(sorted(WORK_SESSION_STORAGE_MODES)))
        normalized["storage_mode"] = storage_mode
    else:
        storage_mode = ""

    if not partial or "status" in payload:
        status = text("status", "active").lower()
        if status not in WORK_SESSION_STATUSES:
            errors.append("status must be one of: " + ", ".join(sorted(WORK_SESSION_STATUSES)))
        normalized["status"] = status

    if not partial or "dirty_status" in payload:
        dirty_status = text("dirty_status", "unknown").lower()
        if dirty_status not in WORK_SESSION_DIRTY_STATUSES:
            errors.append("dirty_status must be one of: " + ", ".join(sorted(WORK_SESSION_DIRTY_STATUSES)))
        normalized["dirty_status"] = dirty_status

    mode_for_path = storage_mode or text("storage_mode")
    if not partial or mode_for_path in WORK_SESSION_REQUIRED_PATH_MODES:
        if mode_for_path == "worktree" and not (normalized.get("worktree_path") or text("worktree_path")):
            errors.append("worktree_path required when storage_mode=worktree")
        if mode_for_path == "clone" and not (normalized.get("clone_path") or text("clone_path")):
            errors.append("clone_path required when storage_mode=clone")

    if not partial or "conflict_marker_count" in payload:
        raw_count = payload.get("conflict_marker_count", 0)
        try:
            count = int(raw_count or 0)
            if count < 0:
                raise ValueError
            normalized["conflict_marker_count"] = count
        except (TypeError, ValueError):
            errors.append("conflict_marker_count must be a non-negative integer")

    json_specs = [
        ("hygiene", "hygiene_json", {}, dict),
        ("file_leases", "file_leases_json", [], list),
        ("resource_leases", "resource_leases_json", [], list),
        ("env", "env_json", {}, dict),
    ]
    for public_key, stored_key, default, expected in json_specs:
        if partial and public_key not in payload and stored_key not in payload:
            continue
        value = payload.get(public_key, payload.get(stored_key))
        parsed, err = _work_session_json(value, default, expected, public_key)
        if err:
            errors.append(err)
        normalized[stored_key] = json.dumps(parsed, sort_keys=True)

    if not partial or "expires_at" in payload:
        raw_expires = payload.get("expires_at")
        if raw_expires in (None, ""):
            normalized["expires_at"] = None
        else:
            try:
                normalized["expires_at"] = float(raw_expires)
            except (TypeError, ValueError):
                errors.append("expires_at must be a unix timestamp")

    token = str(payload.get("session_token") or "").strip()
    token_hash = str(payload.get("session_token_hash") or "").strip()
    if token:
        token_hash = hash_token(token)
    if token_hash:
        normalized["session_token_hash"] = token_hash

    return normalized, errors


def create_work_session(payload: Dict[str, Any], actor: str = "system",
                        principal_id: str = "", project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    data, errors = _validate_work_session_payload(payload or {}, project, partial=False)
    if errors:
        return {"error": "invalid_work_session", "errors": errors,
                "contract": work_session_contract(project) if has_project(project) else None}
    with _conn(project) as c:
        return _insert_work_session_in(c, data, actor=actor,
                                       principal_id=principal_id, project=project)


def _insert_work_session_in(c: sqlite3.Connection, data: Dict[str, Any],
                            actor: str = "system", principal_id: str = "",
                            project: str = DEFAULT_PROJECT,
                            now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    data = dict(data or {})
    work_session_id = data.get("work_session_id") or f"worksession-{uuid.uuid4().hex[:16]}"
    data["work_session_id"] = work_session_id
    data["project_id"] = project
    data["principal_id"] = principal_id or data.get("principal_id") or ""
    data["created_by"] = actor
    data["updated_by"] = actor
    data["created_at"] = now
    data["updated_at"] = now
    if data.get("status") == "completed":
        data["completed_at"] = now
    else:
        data["completed_at"] = None
    columns = [
        "work_session_id", "project_id", "task_id", "claim_id", "agent_id", "runtime",
        "repo_role", "repo", "default_branch", "branch", "upstream", "base_sha", "head_sha",
        "worktree_path", "clone_path", "storage_mode", "status", "dirty_status",
        "conflict_marker_count", "hygiene_json", "file_leases_json", "resource_leases_json",
        "env_json", "policy_profile", "session_token_hash", "principal_id", "created_by",
        "updated_by", "created_at", "updated_at", "expires_at", "completed_at",
    ]
    existing = c.execute("SELECT 1 FROM work_sessions WHERE work_session_id=?",
                         (work_session_id,)).fetchone()
    if existing:
        return {"error": "duplicate_work_session", "work_session_id": work_session_id}
    c.execute(
        f"INSERT INTO work_sessions({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        [data.get(col) for col in columns],
    )
    event = {
        "work_session_id": work_session_id,
        "agent_id": data.get("agent_id"),
        "repo_role": data.get("repo_role"),
        "branch": data.get("branch"),
        "storage_mode": data.get("storage_mode"),
        "status": data.get("status"),
    }
    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
              (data.get("task_id") or None, actor, "work_session.created",
               json.dumps(event, sort_keys=True), now))
    row = c.execute("SELECT * FROM work_sessions WHERE work_session_id=?",
                    (work_session_id,)).fetchone()
    return {"created": True, "work_session": _work_session_row(row)}


def get_work_session(work_session_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    if not has_project(project):
        return None
    with _conn(project) as c:
        row = c.execute("SELECT * FROM work_sessions WHERE work_session_id=?",
                        ((work_session_id or "").strip(),)).fetchone()
    return _work_session_row(row) if row else None


def list_work_sessions(project: str = DEFAULT_PROJECT, task_id: str = "",
                       agent_id: str = "", status: str = "",
                       repo_role: str = "", include_expired: bool = True) -> List[Dict[str, Any]]:
    if not has_project(project):
        return []
    where = ["1=1"]
    params: List[Any] = []
    if task_id:
        where.append("task_id=?")
        params.append(task_id.strip().upper())
    if agent_id:
        where.append("agent_id=?")
        params.append(agent_id.strip())
    if status:
        where.append("status=?")
        params.append(status.strip().lower())
    if repo_role:
        where.append("repo_role=?")
        params.append(repo_role.strip())
    if not include_expired:
        now = time.time()
        where.append("(expires_at IS NULL OR expires_at>?)")
        params.append(now)
    with _conn(project) as c:
        rows = c.execute(
            "SELECT * FROM work_sessions WHERE " + " AND ".join(where) +
            " ORDER BY updated_at DESC, work_session_id",
            params,
        ).fetchall()
    return [_work_session_row(row) for row in rows]


def get_work_session_health(work_session_id: str,
                            project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    session = get_work_session(work_session_id, project=project)
    if not session:
        return None
    return session.get("health") or _work_session_health(session)


def list_session_health(project: str = DEFAULT_PROJECT, task_id: str = "",
                        agent_id: str = "", status: str = "",
                        only_unsafe: bool = False) -> Dict[str, Any]:
    sessions = list_work_sessions(
        project=project, task_id=task_id, agent_id=agent_id, status=status)
    health_rows = [s.get("health") or _work_session_health(s) for s in sessions]
    if only_unsafe:
        health_rows = [h for h in health_rows if h.get("status") == "unsafe"]
    task_health = None
    if task_id:
        task = get_task(task_id, project=project)
        task_health = (task or {}).get("session_health")
    return {
        "schema": "switchboard.session_health_list.v1",
        "project_id": project,
        "task_id": (task_id or "").strip().upper() or None,
        "agent_id": (agent_id or "").strip() or None,
        "only_unsafe": bool(only_unsafe),
        "count": len(health_rows),
        "unsafe_count": sum(1 for h in health_rows if h.get("status") == "unsafe"),
        "warning_count": sum(1 for h in health_rows if h.get("status") == "warning"),
        "task_session_health": task_health,
        "session_health": health_rows,
    }


def update_work_session(work_session_id: str, payload: Dict[str, Any], actor: str = "system",
                        project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    work_session_id = (work_session_id or "").strip()
    existing = get_work_session(work_session_id, project=project)
    if not existing:
        return {"error": "work_session_not_found", "work_session_id": work_session_id}
    data, errors = _validate_work_session_payload(payload or {}, project, partial=True)
    if errors:
        return {"error": "invalid_work_session", "errors": errors,
                "contract": work_session_contract(project)}
    if not data:
        return {"updated": False, "work_session": existing}
    now = time.time()
    data["updated_at"] = now
    data["updated_by"] = actor
    status = data.get("status")
    if status == "completed" and not existing.get("completed_at"):
        data["completed_at"] = now
    sets = [f"{key}=?" for key in data]
    vals = list(data.values()) + [work_session_id]
    event_kind = "work_session.completed" if status == "completed" else "work_session.updated"
    if status == "expired":
        event_kind = "work_session.expired"
    with _conn(project) as c:
        c.execute(f"UPDATE work_sessions SET {', '.join(sets)} WHERE work_session_id=?", vals)
        event = {
            "work_session_id": work_session_id,
            "updated_fields": sorted(data.keys()),
            "status": status or existing.get("status"),
        }
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (existing.get("task_id") or None, actor, event_kind,
                   json.dumps(event, sort_keys=True), now))
        row = c.execute("SELECT * FROM work_sessions WHERE work_session_id=?",
                        (work_session_id,)).fetchone()
    return {"updated": True, "work_session": _work_session_row(row)}


def _managed_workspace_error(code: str, message: str, failure_class: str = "failed_gate",
                             **details: Any) -> Dict[str, Any]:
    return {
        "schema": MANAGED_WORK_SESSION_SCHEMA,
        "created": False,
        "error": code,
        "message": message,
        "failure_class": failure_class,
        **details,
    }


def _managed_workspace_git(repo_path: str, args: List[str],
                           timeout_seconds: int = 60) -> Dict[str, Any]:
    return _repo_git(repo_path, args, timeout_seconds=timeout_seconds)


def _managed_workspace_slug(value: str, fallback: str = "work") -> str:
    slug = normalize_project_id(value or "")
    return (slug or fallback)[:48].strip("-_") or fallback


def _managed_workspace_branch(task: Dict[str, Any], agent_id: str,
                              requested: str = "") -> str:
    branch = (requested or "").strip()
    if branch:
        return branch
    runtime = (agent_id or "agent").split("/", 1)[0].strip() or "agent"
    task_id = task.get("task_id") or "TASK"
    slug = _managed_workspace_slug(task.get("title") or task_id)
    return f"{runtime}/{task_id}-{slug}"


def _managed_workspace_root(project: str, payload: Dict[str, Any]) -> str:
    configured = str(payload.get("workspace_root") or "").strip()
    if configured:
        return configured
    suffix = _project_env_suffix(project)
    return (
        os.environ.get(f"PM_WORKSPACE_ROOT_{suffix}") if suffix else ""
    ) or os.environ.get("PM_WORKSPACE_ROOT") or "/var/lib/projectplanner/workspaces"


def _managed_workspace_source_path(project: str, payload: Dict[str, Any]) -> str:
    configured = str(payload.get("source_path") or payload.get("repo_path") or "").strip()
    if configured:
        return configured
    suffix = _project_env_suffix(project)
    return (
        os.environ.get(f"PM_REPO_PATH_{suffix}") if suffix else ""
    ) or os.environ.get("PM_REPO_PATH") or os.path.dirname(__file__)


def _managed_workspace_path(root: str, project: str, task_id: str, branch: str,
                            requested: str = "") -> Tuple[str, str]:
    root_abs = os.path.abspath(os.path.expanduser(root))
    if requested:
        path_abs = os.path.abspath(os.path.expanduser(requested))
    else:
        safe_branch = branch.replace("/", "__")
        path_abs = os.path.join(root_abs, project, task_id.lower(), safe_branch)
    try:
        common = os.path.commonpath([root_abs, path_abs])
    except ValueError:
        common = ""
    if common != root_abs:
        return path_abs, "workspace_path must be inside workspace_root"
    return path_abs, ""


def _managed_role(project: str, repo_role: str) -> Tuple[Dict[str, Any], str]:
    topology = get_project_repo_topology(project)
    role = (topology.get("roles") or {}).get(repo_role) or {}
    if not role:
        return {}, "repo_role must exist in repo_topology.roles"
    if repo_role == "canonical" and not (topology.get("code_repo_gate") or {}).get("passed"):
        return role, "canonical repo is not configured"
    if not (role.get("repo") or "").strip():
        return role, f"repo_topology.roles.{repo_role}.repo is not configured"
    return role, ""


def _managed_verify_source_repo(source_path: str, project: str,
                                repo_role: str) -> Tuple[Dict[str, Any], str]:
    source_path = os.path.abspath(os.path.expanduser(source_path))
    inside = _repo_git(source_path, ["rev-parse", "--is-inside-work-tree"])
    if not inside.get("ok") or inside.get("stdout") != "true":
        return {}, "source_path is not a git worktree"
    top = _repo_git(source_path, ["rev-parse", "--show-toplevel"])
    repo_path = os.path.abspath(top.get("stdout") or source_path)
    remote = _repo_git(repo_path, ["remote", "get-url", "origin"])
    remote_url = remote.get("stdout") if remote.get("ok") else ""
    expected_repo = (((get_project_repo_topology(project).get("roles") or {})
                      .get(repo_role) or {}).get("repo") or "").strip()
    actual_slug = _repo_remote_slug(remote_url)
    expected_slug = _repo_remote_slug(expected_repo)
    if actual_slug and expected_slug and actual_slug.lower() != expected_slug.lower():
        return {
            "repo_path": repo_path,
            "remote": {"url": remote_url, "repo": actual_slug},
            "expected_repo": expected_repo,
        }, "source_path origin does not match project repo topology"
    return {
        "repo_path": repo_path,
        "remote": {"url": remote_url, "repo": actual_slug},
        "expected_repo": expected_repo,
    }, ""


def _managed_prepare_worktree(source_path: str, workspace_path: str, branch: str,
                              base_ref: str, fetch: bool) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    steps: List[Dict[str, Any]] = []
    if fetch and base_ref.startswith("origin/"):
        fetched = _managed_workspace_git(source_path, ["fetch", "origin", base_ref.split("/", 1)[1]])
        steps.append({"cmd": "git fetch", "ok": fetched.get("ok"), "stderr": fetched.get("stderr")})
        if not fetched.get("ok"):
            return fetched, steps
    base = _managed_workspace_git(source_path, ["rev-parse", f"{base_ref}^{{commit}}"])
    steps.append({"cmd": "git rev-parse base", "ok": base.get("ok"), "stderr": base.get("stderr")})
    if not base.get("ok"):
        return base, steps
    existing_branch = _managed_workspace_git(
        source_path, ["rev-parse", "--verify", f"refs/heads/{branch}"])
    if existing_branch.get("ok"):
        return {"ok": False, "stderr": "branch already exists", "code": "branch_exists"}, steps
    added = _managed_workspace_git(
        source_path, ["worktree", "add", "-b", branch, workspace_path, base_ref],
        timeout_seconds=120,
    )
    steps.append({"cmd": "git worktree add", "ok": added.get("ok"), "stderr": added.get("stderr")})
    if not added.get("ok"):
        return added, steps
    if base_ref.startswith("origin/"):
        upstream = _managed_workspace_git(
            workspace_path, ["branch", "--set-upstream-to", base_ref, branch])
        steps.append({"cmd": "git branch --set-upstream-to", "ok": upstream.get("ok"),
                      "stderr": upstream.get("stderr")})
    return {"ok": True, "base_sha": base.get("stdout")}, steps


def _managed_prepare_clone(source_path: str, workspace_path: str, branch: str,
                           base_ref: str, fetch: bool,
                           role_repo: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    steps: List[Dict[str, Any]] = []
    clone_source = source_path or (f"https://github.com/{role_repo}.git" if role_repo else "")
    if not clone_source:
        return {"ok": False, "stderr": "clone source unavailable"}, steps
    parent = os.path.dirname(workspace_path)
    os.makedirs(parent, exist_ok=True)
    cloned = subprocess.run(
        ["git", "clone", clone_source, workspace_path],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
    )
    clone_result = {"ok": cloned.returncode == 0, "stdout": (cloned.stdout or "").strip(),
                    "stderr": (cloned.stderr or "").strip(), "returncode": cloned.returncode}
    steps.append({"cmd": "git clone", "ok": clone_result.get("ok"),
                  "stderr": clone_result.get("stderr")})
    if not clone_result.get("ok"):
        return clone_result, steps
    if fetch and base_ref.startswith("origin/"):
        fetched = _managed_workspace_git(workspace_path, ["fetch", "origin", base_ref.split("/", 1)[1]])
        steps.append({"cmd": "git fetch", "ok": fetched.get("ok"), "stderr": fetched.get("stderr")})
        if not fetched.get("ok"):
            return fetched, steps
    base = _managed_workspace_git(workspace_path, ["rev-parse", f"{base_ref}^{{commit}}"])
    steps.append({"cmd": "git rev-parse base", "ok": base.get("ok"), "stderr": base.get("stderr")})
    if not base.get("ok"):
        return base, steps
    checked = _managed_workspace_git(workspace_path, ["checkout", "-b", branch, base_ref])
    steps.append({"cmd": "git checkout -b", "ok": checked.get("ok"), "stderr": checked.get("stderr")})
    if not checked.get("ok"):
        return checked, steps
    if base_ref.startswith("origin/"):
        upstream = _managed_workspace_git(
            workspace_path, ["branch", "--set-upstream-to", base_ref, branch])
        steps.append({"cmd": "git branch --set-upstream-to", "ok": upstream.get("ok"),
                      "stderr": upstream.get("stderr")})
    return {"ok": True, "base_sha": base.get("stdout")}, steps


def create_managed_work_session(payload: Dict[str, Any], actor: str = "system",
                                principal_id: str = "",
                                project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Create a git-backed isolated workspace and persist it as a Work Session."""
    payload = dict(payload or {})
    if not has_project(project):
        return _managed_workspace_error("unknown_project", f"Unknown project: {project}",
                                        "invalid_input")
    task_id = str(payload.get("task_id") or "").strip().upper()
    agent_id = str(payload.get("agent_id") or "").strip()
    if not task_id:
        return _managed_workspace_error("task_id_required", "task_id is required.",
                                        "missing_data")
    if not agent_id:
        return _managed_workspace_error("agent_id_required", "agent_id is required.",
                                        "missing_data")
    task = get_task(task_id, project=project)
    if not task:
        return _managed_workspace_error("task_not_found", "task_id must exist in project.",
                                        "invalid_input", task_id=task_id)
    repo_role = str(payload.get("repo_role") or "canonical").strip()
    role, role_error = _managed_role(project, repo_role)
    if role_error:
        return _managed_workspace_error("repo_role_unavailable", role_error,
                                        "missing_data", repo_role=repo_role)
    storage_mode = str(payload.get("storage_mode") or role.get("workspace_mode")
                       or payload.get("default_mode") or "worktree").strip().lower()
    if storage_mode not in {"worktree", "clone"}:
        return _managed_workspace_error(
            "managed_storage_mode_not_allowed",
            "Managed workspace creation supports storage_mode=worktree or clone.",
            "invalid_input",
            storage_mode=storage_mode,
        )
    source_path = _managed_workspace_source_path(project, payload)
    source_info, source_error = _managed_verify_source_repo(source_path, project, repo_role)
    if source_error:
        return _managed_workspace_error("source_repo_invalid", source_error,
                                        "wrong_repo", source_path=source_path,
                                        source=source_info)
    source_path = source_info.get("repo_path") or os.path.abspath(source_path)
    branch = _managed_workspace_branch(task, agent_id, str(payload.get("branch") or ""))
    workspace_root = _managed_workspace_root(project, payload)
    requested_path = str(payload.get("workspace_path") or payload.get("worktree_path")
                         or payload.get("clone_path") or "").strip()
    workspace_path, path_error = _managed_workspace_path(
        workspace_root, project, task_id, branch, requested_path)
    if path_error:
        return _managed_workspace_error("workspace_path_invalid", path_error,
                                        "invalid_input", workspace_path=workspace_path,
                                        workspace_root=os.path.abspath(workspace_root))
    if os.path.exists(workspace_path):
        return _managed_workspace_error("workspace_path_exists",
                                        "Managed workspace path already exists.",
                                        "failed_gate",
                                        workspace_path=workspace_path)
    conflicts = check_resources("worktree", [workspace_path], project=project)
    if conflicts:
        return _managed_workspace_error("workspace_path_leased",
                                        "Managed workspace path is already leased.",
                                        "failed_gate",
                                        workspace_path=workspace_path,
                                        conflicts=conflicts)
    default_branch = str(role.get("default_branch") or "master").strip() or "master"
    base_ref = str(payload.get("base_ref") or f"origin/{default_branch}").strip()
    fetch = _merge_gate_bool(payload.get("fetch"), default=True)
    os.makedirs(os.path.dirname(workspace_path), exist_ok=True)
    if storage_mode == "worktree":
        prepared, steps = _managed_prepare_worktree(
            source_path, workspace_path, branch, base_ref, bool(fetch))
    else:
        prepared, steps = _managed_prepare_clone(
            source_path, workspace_path, branch, base_ref, bool(fetch), role.get("repo") or "")
    if not prepared.get("ok"):
        if os.path.exists(workspace_path):
            shutil.rmtree(workspace_path, ignore_errors=True)
        code = prepared.get("code") or "workspace_create_failed"
        return _managed_workspace_error(
            code,
            "Git workspace creation failed.",
            "failed_gate",
            git_error=prepared,
            git_steps=steps,
        )
    ttl_seconds = int(payload.get("ttl_seconds") or payload.get("ttl_s") or 7200)
    session_token = "wst-" + uuid.uuid4().hex
    env = dict(payload.get("env") or {})
    env.update({
        "managed_workspace": True,
        "workspace_root": os.path.abspath(workspace_root),
        "workspace_namespace": _managed_workspace_slug(f"{project}-{task_id}"),
        "port_namespace": 10000 + (int(hashlib.sha1(f"{project}:{task_id}".encode()).hexdigest()[:4], 16) % 50000),
        "source_path": source_path,
        "base_ref": base_ref,
        "git_steps": steps,
    })
    lease = claim_resources(
        agent_id=agent_id,
        resource_type="worktree",
        names=[workspace_path],
        task_id=task_id,
        ttl_seconds=ttl_seconds,
        principal_id=principal_id,
        actor=actor,
        idem_key=str(payload.get("idem_key") or f"managed-workspace:{project}:{task_id}:{agent_id}:{workspace_path}"),
        project=project,
    )
    if lease.get("conflict") or lease.get("error"):
        if storage_mode == "worktree":
            _managed_workspace_git(source_path, ["worktree", "remove", "--force", workspace_path])
        else:
            shutil.rmtree(workspace_path, ignore_errors=True)
        return _managed_workspace_error("workspace_lease_failed",
                                        "Could not claim managed workspace lease.",
                                        "failed_gate", lease=lease)
    preflight = repo_preflight(
        workspace_path,
        project=project,
        task_id=task_id,
        agent_id=agent_id,
        repo_role=repo_role,
        expected_branch=branch,
        expected_base_ref=base_ref,
    )
    session_payload = {
        "task_id": task_id,
        "agent_id": agent_id,
        "runtime": payload.get("runtime") or agent_id.split("/", 1)[0],
        "repo_role": repo_role,
        "branch": branch,
        "upstream": preflight.get("upstream") or base_ref,
        "base_sha": preflight.get("base_sha") or prepared.get("base_sha") or "",
        "head_sha": preflight.get("head_sha") or prepared.get("base_sha") or "",
        "worktree_path": workspace_path if storage_mode == "worktree" else "",
        "clone_path": workspace_path if storage_mode == "clone" else "",
        "storage_mode": storage_mode,
        "status": "active",
        "dirty_status": "dirty" if preflight.get("dirty") else "clean",
        "conflict_marker_count": int(preflight.get("conflict_marker_count") or 0),
        "hygiene": {"repo_preflight": preflight, "managed": {"schema": MANAGED_WORK_SESSION_SCHEMA}},
        "resource_leases": [lease],
        "env": env,
        "policy_profile": payload.get("policy_profile") or "code_strict",
        "expires_at": time.time() + max(1, ttl_seconds),
        "session_token": session_token,
    }
    created = create_work_session(
        session_payload, actor=actor, principal_id=principal_id, project=project)
    if created.get("error"):
        release_resource_lease(lease.get("lease_id") or "", actor=actor, project=project)
        if storage_mode == "worktree":
            _managed_workspace_git(source_path, ["worktree", "remove", "--force", workspace_path])
        else:
            shutil.rmtree(workspace_path, ignore_errors=True)
        return _managed_workspace_error("work_session_create_failed",
                                        "Managed workspace was created but Work Session persist failed.",
                                        "failed_gate", result=created)
    append_activity(
        "work_session.managed_created",
        actor,
        {
            "schema": MANAGED_WORK_SESSION_SCHEMA,
            "work_session_id": created["work_session"]["work_session_id"],
            "task_id": task_id,
            "agent_id": agent_id,
            "storage_mode": storage_mode,
            "workspace_path": workspace_path,
            "branch": branch,
            "base_ref": base_ref,
            "lease_id": lease.get("lease_id"),
        },
        task_id=task_id,
        project=project,
    )
    return {
        "schema": MANAGED_WORK_SESSION_SCHEMA,
        "created": True,
        "managed": True,
        "project": project,
        "task_id": task_id,
        "agent_id": agent_id,
        "storage_mode": storage_mode,
        "branch": branch,
        "workspace_path": workspace_path,
        "base_ref": base_ref,
        "lease": lease,
        "session_token": session_token,
        "work_session": created["work_session"],
    }


def archive_work_session_workspace(work_session_id: str, remove_workspace: bool = False,
                                   actor: str = "system",
                                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    session = get_work_session(work_session_id, project=project)
    if not session:
        return {"error": "work_session_not_found", "work_session_id": work_session_id}
    env = session.get("env") or {}
    workspace_root = os.path.abspath(os.path.expanduser(str(env.get("workspace_root") or "")))
    workspace_path = os.path.abspath(os.path.expanduser(
        session.get("worktree_path") or session.get("clone_path") or ""))
    if remove_workspace:
        if not env.get("managed_workspace"):
            return {"error": "not_managed_workspace", "work_session_id": work_session_id}
        if not workspace_root or not workspace_path:
            return {"error": "managed_workspace_path_missing",
                    "work_session_id": work_session_id}
        try:
            common = os.path.commonpath([workspace_root, workspace_path])
        except ValueError:
            common = ""
        if common != workspace_root:
            return {"error": "workspace_path_outside_root",
                    "work_session_id": work_session_id,
                    "workspace_path": workspace_path,
                    "workspace_root": workspace_root}
        if os.path.exists(workspace_path):
            if session.get("storage_mode") == "worktree":
                source_path = str(env.get("source_path") or "").strip()
                removed = _managed_workspace_git(
                    source_path or workspace_path,
                    ["worktree", "remove", "--force", workspace_path],
                    timeout_seconds=120,
                )
                if not removed.get("ok") and os.path.exists(workspace_path):
                    return {"error": "workspace_remove_failed",
                            "work_session_id": work_session_id,
                            "git_error": removed}
            else:
                shutil.rmtree(workspace_path, ignore_errors=True)
    updated = update_work_session(
        work_session_id,
        {"status": "archived", "hygiene": {**(session.get("hygiene") or {}),
                                           "archived_workspace": {
                                               "removed_workspace": bool(remove_workspace),
                                               "workspace_path": workspace_path,
                                           }}},
        actor=actor,
        project=project,
    )
    append_activity(
        "work_session.workspace_archived",
        actor,
        {
            "work_session_id": work_session_id,
            "remove_workspace": bool(remove_workspace),
            "removed_workspace": bool(remove_workspace and not os.path.exists(workspace_path)),
            "workspace_path": workspace_path,
        },
        task_id=session.get("task_id") or None,
        project=project,
    )
    return {
        "archived": updated.get("updated") is True,
        "removed_workspace": bool(remove_workspace and not os.path.exists(workspace_path)),
        "work_session": updated.get("work_session"),
    }


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


def preflight_work_session(work_session_id: str, actor: str = "system",
                           project: str = DEFAULT_PROJECT,
                           expected_branch: str = "",
                           expected_base_ref: str = "") -> Dict[str, Any]:
    session = get_work_session(work_session_id, project=project)
    if not session:
        return {"error": "work_session_not_found", "work_session_id": work_session_id}
    worktree_path = session.get("worktree_path") or session.get("clone_path") or ""
    if not worktree_path:
        return {"error": "work_session_missing_path", "work_session_id": work_session_id}
    report = repo_preflight(
        worktree_path,
        project=project,
        task_id=session.get("task_id") or "",
        agent_id=session.get("agent_id") or "",
        repo_role=session.get("repo_role") or "canonical",
        expected_branch=expected_branch or session.get("branch") or "",
        expected_base_ref=expected_base_ref,
    )
    hygiene = dict(session.get("hygiene") or {})
    hygiene["repo_preflight"] = report
    updates = {
        "hygiene": hygiene,
        "dirty_status": "dirty" if report.get("dirty") else "clean",
        "conflict_marker_count": int(report.get("conflict_marker_count") or 0),
        "branch": report.get("branch") or session.get("branch") or "",
        "upstream": report.get("upstream") or session.get("upstream") or "",
        "base_sha": report.get("base_sha") or session.get("base_sha") or "",
        "head_sha": report.get("head_sha") or session.get("head_sha") or "",
    }
    updated = update_work_session(work_session_id, updates, actor=actor, project=project)
    return {
        "work_session_id": work_session_id,
        "project": project,
        "preflight": report,
        "updated": updated,
    }


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


def _coerce_work_session_payload(value: Any) -> Tuple[Dict[str, Any], str]:
    if value in (None, ""):
        return {}, ""
    if isinstance(value, dict):
        return copy.deepcopy(value), ""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}, "work_session must be valid JSON"
        if isinstance(parsed, dict):
            return parsed, ""
    return {}, "work_session must be a JSON object"


def _task_work_session_profile(task: Dict[str, Any],
                               requested_profile: str = "",
                               project: str = DEFAULT_PROJECT) -> str:
    requested = (requested_profile or "").strip().lower()
    if requested:
        return _normalize_session_policy_profile(requested)
    state = task.get("agent_state") or {}
    for key in ("work_session", "session_policy", "dispatch"):
        item = state.get(key) or {}
        if isinstance(item, dict):
            profile = str(item.get("policy_profile") or item.get("profile") or "").strip().lower()
            if profile:
                return _normalize_session_policy_profile(profile)
    text = _session_profile_text(task)
    match = re.search(r"(?:policy_profile|session_profile)\s*[:=]\s*([A-Za-z0-9_-]+)", text)
    if match:
        return _normalize_session_policy_profile(match.group(1))
    defaults = _project_session_policy_defaults(project)
    if _task_looks_like_code_work(task):
        return defaults.get("code_task_default_profile") or "code_strict"
    return defaults.get("default_profile") or "docs_review"


def _work_session_required(task: Dict[str, Any], requested_profile: str = "",
                           require_work_session: bool = False,
                           project: str = DEFAULT_PROJECT) -> Tuple[bool, str]:
    profile = _task_work_session_profile(task, requested_profile, project=project)
    rules = _session_policy_profile_rules(profile, project=project)
    required = bool(require_work_session or rules.get("work_session_required"))
    return required, profile


def _unknown_session_policy_profile(profile: str, project: str) -> Dict[str, Any]:
    known = sorted((get_session_policy_profiles(project).get("profiles") or {}).keys())
    return _work_session_failure(
        "unknown_policy_profile",
        f"Unknown session policy profile: {profile or '<empty>'}.",
        "invalid_input",
        details={"policy_profile": profile or "", "known_profiles": known},
    )


def _branch_matches_task(agent_id: str, task_id: str, branch: str) -> bool:
    branch = (branch or "").strip()
    task_id = (task_id or "").strip()
    if not branch or not task_id:
        return False
    runtime = (agent_id or "").split("/", 1)[0].strip()
    expected_prefix = f"{runtime}/{task_id}" if runtime else task_id
    return branch.startswith(expected_prefix) or f"/{task_id}" in branch


def _work_session_failure(reason: str, message: str, failure_class: str,
                          severity: str = "high",
                          details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "message": message,
        "failure_class": failure_class,
        "severity": severity,
        **(details or {}),
    }


def _active_work_session_row_in(c: sqlite3.Connection, work_session_id: str = "",
                                task_id: str = "", agent_id: str = "",
                                now: Optional[float] = None) -> Optional[sqlite3.Row]:
    now = time.time() if now is None else now
    if work_session_id:
        return c.execute(
            "SELECT * FROM work_sessions WHERE work_session_id=?",
            (work_session_id,),
        ).fetchone()
    return c.execute(
        "SELECT * FROM work_sessions WHERE task_id=? AND agent_id=? "
        "AND status IN ('active','proposed') AND (expires_at IS NULL OR expires_at>?) "
        "ORDER BY updated_at DESC, created_at DESC, work_session_id LIMIT 1",
        (task_id, agent_id, now),
    ).fetchone()


def _validate_work_session_claim_binding_in(
        c: sqlite3.Connection, task: Dict[str, Any], agent_id: str,
        project: str = DEFAULT_PROJECT, work_session_id: str = "",
        work_session: Any = None, policy_profile: str = "",
        require_work_session: bool = False, now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    required, profile = _work_session_required(task, policy_profile, require_work_session,
                                               project=project)
    rules = _session_policy_profile_rules(profile, project=project)
    if not rules:
        return _unknown_session_policy_profile(profile, project)
    task_id = task.get("task_id") or ""
    payload, payload_error = _coerce_work_session_payload(work_session)
    if payload_error:
        return _work_session_failure("malformed_work_session", payload_error,
                                     "malformed_payload", details={"policy_profile": profile})
    if payload:
        payload = {
            **payload,
            "task_id": payload.get("task_id") or task_id,
            "agent_id": payload.get("agent_id") or agent_id,
            "policy_profile": payload.get("policy_profile") or profile,
        }
        data, errors = _validate_work_session_payload(payload, project, partial=False)
        if errors:
            return _work_session_failure(
                "invalid_work_session",
                "Work Session payload failed model validation.",
                "invalid_input",
                details={"errors": errors, "policy_profile": profile},
            )
        storage_mode = data.get("storage_mode") or ""
        allowed_modes = set(rules.get("allowed_storage_modes") or [])
        if allowed_modes and storage_mode not in allowed_modes:
            return _work_session_failure(
                "storage_mode_not_allowed",
                f"Policy profile {profile} does not allow storage_mode={storage_mode}.",
                "invalid_input",
                details={"policy_profile": profile, "storage_mode": storage_mode,
                         "allowed_storage_modes": sorted(allowed_modes)},
            )
        if data.get("work_session_id"):
            existing = c.execute("SELECT 1 FROM work_sessions WHERE work_session_id=?",
                                 (data["work_session_id"],)).fetchone()
            if existing:
                return _work_session_failure(
                    "duplicate_work_session",
                    "Work Session id already exists.",
                    "invalid_input",
                    details={"work_session_id": data["work_session_id"],
                             "policy_profile": profile},
                )
        session = _work_session_row_from_data(data, project=project)
        return _validate_work_session_claim_state(
            session, task, agent_id, project, required=required, profile=profile,
            source="payload", normalized_payload=data, now=now)

    row = _active_work_session_row_in(c, work_session_id=work_session_id,
                                      task_id=task_id, agent_id=agent_id, now=now)
    if not row:
        if required:
            return _work_session_failure(
                "work_session_required",
                "A valid Work Session is required before claiming code-strict work.",
                "missing_data",
                details={"policy_profile": profile, "required": True},
            )
        return {"ok": True, "required": False, "policy_profile": profile,
                "source": "not_required", "work_session": None}
    session = _work_session_row(row)
    return _validate_work_session_claim_state(
        session, task, agent_id, project, required=required, profile=profile,
        source="existing", normalized_payload=None, now=now)


def _work_session_row_from_data(data: Dict[str, Any], project: str) -> Dict[str, Any]:
    return {
        "schema": WORK_SESSION_SCHEMA,
        "work_session_id": data.get("work_session_id") or "",
        "project_id": project,
        "task_id": data.get("task_id") or "",
        "claim_id": data.get("claim_id") or "",
        "agent_id": data.get("agent_id") or "",
        "runtime": data.get("runtime") or "",
        "repo_role": data.get("repo_role") or "",
        "repo": data.get("repo") or "",
        "default_branch": data.get("default_branch") or "",
        "branch": data.get("branch") or "",
        "upstream": data.get("upstream") or "",
        "base_sha": data.get("base_sha") or "",
        "head_sha": data.get("head_sha") or "",
        "worktree_path": data.get("worktree_path") or "",
        "clone_path": data.get("clone_path") or "",
        "storage_mode": data.get("storage_mode") or "",
        "status": data.get("status") or "",
        "dirty_status": data.get("dirty_status") or "",
        "conflict_marker_count": data.get("conflict_marker_count") or 0,
        "hygiene": _json_obj(data.get("hygiene_json") or "{}", {}),
        "file_leases": _json_obj(data.get("file_leases_json") or "[]", []),
        "resource_leases": _json_obj(data.get("resource_leases_json") or "[]", []),
        "env": _json_obj(data.get("env_json") or "{}", {}),
        "policy_profile": data.get("policy_profile") or "",
        "expires_at": data.get("expires_at"),
        "session_token_hash_present": bool(data.get("session_token_hash")),
    }


def _validate_work_session_claim_state(
        session: Dict[str, Any], task: Dict[str, Any], agent_id: str,
        project: str, required: bool, profile: str, source: str,
        normalized_payload: Optional[Dict[str, Any]] = None,
        now: Optional[float] = None,
        allow_dirty: bool = False) -> Dict[str, Any]:
    now = time.time() if now is None else now
    task_id = task.get("task_id") or ""
    rules = _session_policy_profile_rules(profile, project=project)
    if not rules:
        return _unknown_session_policy_profile(profile, project)
    problems: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    def add_problem(reason: str, failure_class: str, message: str) -> None:
        item = {"reason": reason, "failure_class": failure_class, "message": message}
        if reason in set(rules.get("warn_hygiene") or []):
            warnings.append(item)
        else:
            problems.append(item)

    if session.get("project_id") != project:
        add_problem("wrong_project", "invalid_input",
                    "Work Session project_id does not match claim project.")
    if session.get("task_id") and session.get("task_id") != task_id:
        add_problem("wrong_task", "invalid_input",
                    "Work Session task_id does not match claimed task.")
    if session.get("agent_id") and session.get("agent_id") != agent_id:
        add_problem("wrong_agent", "unbound_identity",
                    "Work Session agent_id does not match claiming agent.")
    if session.get("status") not in {"active", "proposed"}:
        add_problem("inactive_work_session", "failed_gate",
                    "Work Session is not active/proposed.")
    expires_at = session.get("expires_at")
    if expires_at is not None:
        try:
            if float(expires_at) <= now:
                add_problem("expired_work_session", "stale_branch",
                            "Work Session is expired.")
        except (TypeError, ValueError):
            add_problem("invalid_work_session_expiry", "invalid_input",
                        "Work Session expires_at is invalid.")
    if session.get("dirty_status") == "dirty" and not allow_dirty:
        add_problem("dirty_work_session", "failed_gate",
                    "Work Session reports a dirty workspace.")
    if int(session.get("conflict_marker_count") or 0) > 0:
        add_problem("conflict_markers", "failed_gate",
                    "Work Session reports conflict markers.")
    if rules.get("requires_branch_task_scope") and not _branch_matches_task(
            agent_id, task_id, session.get("branch") or ""):
        add_problem("wrong_branch", "stale_branch",
                    "Work Session branch must be task-scoped.")
    if rules.get("requires_upstream") and not session.get("upstream"):
        add_problem("missing_upstream", "missing_data",
                    "Work Session upstream is required for this profile.")
    if rules.get("requires_base_sha") and not session.get("base_sha"):
        add_problem("missing_base_sha", "missing_data",
                    "Work Session base_sha is required for this profile.")
    if problems:
        first = problems[0]
        return _work_session_failure(
            first["reason"], first["message"], first["failure_class"],
            details={"problems": problems, "policy_profile": profile,
                     "required": required, "work_session_id": session.get("work_session_id") or None},
        )
    return {
        "ok": True,
        "required": required,
        "policy_profile": profile,
        "policy": rules,
        "warnings": warnings,
        "source": source,
        "work_session": session,
        "normalized_payload": normalized_payload,
    }


def _attach_work_session_claim_in(c: sqlite3.Connection, verdict: Dict[str, Any],
                                  claim_id: str, task_id: str, agent_id: str,
                                  actor: str, principal_id: str = "",
                                  project: str = DEFAULT_PROJECT,
                                  now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    session = verdict.get("work_session")
    if not session:
        return {"work_session_id": None, "status": "not_required",
                "source": verdict.get("source"),
                "policy_profile": verdict.get("policy_profile"),
                "required": verdict.get("required", False)}
    if verdict.get("source") == "payload":
        data = dict(verdict.get("normalized_payload") or {})
        data["claim_id"] = claim_id
        data["task_id"] = data.get("task_id") or task_id
        data["agent_id"] = data.get("agent_id") or agent_id
        data["status"] = "active"
        created = _insert_work_session_in(
            c, data, actor=actor, principal_id=principal_id, project=project, now=now)
        if created.get("error"):
            return {"error": created.get("error"), "work_session_id": data.get("work_session_id")}
        session = created["work_session"]
    else:
        c.execute(
            "UPDATE work_sessions SET claim_id=?, status='active', updated_by=?, updated_at=? "
            "WHERE work_session_id=?",
            (claim_id, actor, now, session["work_session_id"]),
        )
        row = c.execute("SELECT * FROM work_sessions WHERE work_session_id=?",
                        (session["work_session_id"],)).fetchone()
        session = _work_session_row(row)
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "work_session.updated",
                   json.dumps({"work_session_id": session["work_session_id"],
                               "claim_id": claim_id,
                               "updated_fields": ["claim_id", "status"]},
                              sort_keys=True), now))
    return {"work_session_id": session.get("work_session_id"),
            "status": "bound",
            "source": verdict.get("source"),
            "policy_profile": verdict.get("policy_profile"),
            "required": verdict.get("required", False)}


def _runner_control_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["snapshot"] = _json_obj(d.pop("snapshot_json", "{}"), {})
    d["result"] = _json_obj(d.pop("result_json", "{}"), {})
    d["options"] = _json_obj(d.pop("options_json", "{}"), {})
    return d


def _runner_snapshot_from_session(session: Dict[str, Any],
                                  reason: str = "operator_request") -> Dict[str, Any]:
    return {
        "captured_at": time.time(),
        "source": "switchboard_registry",
        "reason": reason,
        "runner_session_id": session.get("runner_session_id"),
        "host_id": session.get("host_id"),
        "agent_id": session.get("agent_id"),
        "runtime": session.get("runtime"),
        "task_id": session.get("task_id"),
        "claim_id": session.get("claim_id"),
        "pid": session.get("pid"),
        "status": session.get("status"),
        "cwd": session.get("cwd"),
        "heartbeat_at": session.get("heartbeat_at"),
        "head_sha": (session.get("last_snapshot") or {}).get("head_sha"),
    }


def _upsert_runner_session_in(c: sqlite3.Connection, record: Dict[str, Any],
                              principal_id: str, actor: str, now: float) -> Dict[str, Any]:
    runner_session_id = (record.get("runner_session_id") or record.get("id") or "").strip()
    if not runner_session_id:
        return {"error": "runner_session_id required"}
    host_id = (record.get("host_id") or "").strip()
    control = _normalize_runner_control(record.get("control") or {}, host_id)
    metadata = dict(record.get("metadata") or {})
    for key in ("command", "log_path", "pgid", "wake_id", "wake_mode", "alive"):
        if key in record and key not in metadata:
            metadata[key] = record.get(key)
    snapshot = record.get("last_snapshot") or record.get("snapshot") or {}
    heartbeat_ttl_s = max(10, int(record.get("heartbeat_ttl_s") or record.get("ttl_s") or 60))
    started_at = record.get("started_at") or now
    heartbeat_at = record.get("heartbeat_at") or now
    c.execute(
        "INSERT INTO runner_sessions(runner_session_id, host_id, agent_id, runtime, task_id, "
        "claim_id, pid, status, cwd, control_json, metadata_json, last_snapshot_json, "
        "principal_id, started_at, heartbeat_at, heartbeat_ttl_s, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(runner_session_id) DO UPDATE SET host_id=excluded.host_id, "
        "agent_id=excluded.agent_id, runtime=excluded.runtime, task_id=excluded.task_id, "
        "claim_id=excluded.claim_id, pid=excluded.pid, status=excluded.status, cwd=excluded.cwd, "
        "control_json=excluded.control_json, metadata_json=excluded.metadata_json, "
        "last_snapshot_json=CASE WHEN excluded.last_snapshot_json!='{}' "
        "THEN excluded.last_snapshot_json ELSE runner_sessions.last_snapshot_json END, "
        "principal_id=excluded.principal_id, heartbeat_at=excluded.heartbeat_at, "
        "heartbeat_ttl_s=excluded.heartbeat_ttl_s, updated_at=excluded.updated_at",
        (
            runner_session_id,
            host_id or None,
            record.get("agent_id") or None,
            record.get("runtime") or None,
            record.get("task_id") or None,
            record.get("claim_id") or None,
            record.get("pid"),
            record.get("status") or ("running" if record.get("alive", True) else "unknown"),
            record.get("cwd") or None,
            json.dumps(control, sort_keys=True),
            json.dumps(metadata, sort_keys=True),
            json.dumps(snapshot or {}, sort_keys=True),
            principal_id or None,
            started_at,
            heartbeat_at,
            heartbeat_ttl_s,
            now,
        ),
    )
    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
              (record.get("task_id") or None, actor, "runner.session_registered",
               json.dumps({"runner_session_id": runner_session_id, "host_id": host_id or None,
                           "agent_id": record.get("agent_id"),
                           "runtime": record.get("runtime"),
                           "control": control,
                           "available_actions": _runner_available_actions({
                               "control": control,
                               "host_id": host_id,
                               "status": record.get("status") or "running",
                               "stale": False,
                           })}, sort_keys=True), now))
    row = c.execute("SELECT * FROM runner_sessions WHERE runner_session_id=?",
                    (runner_session_id,)).fetchone()
    return _runner_session_row(row, now=now, include_claim=True, c=c)


def _selector_runtime_for_agent(agent_id: str) -> str:
    aid = (agent_id or "").lower()
    if aid.startswith("claude"):
        return "claude-code"
    if aid.startswith("codex"):
        return "codex"
    if aid.startswith("cursor"):
        return "cursor"
    if aid.startswith("langgraph"):
        return "langgraph"
    if aid.startswith("openai"):
        return "openai-loop"
    return ""


def _runtime_matches_selector(runtime: Dict[str, Any], selector: Dict[str, Any]) -> bool:
    want_runtime = (selector.get("runtime") or "").strip()
    want_lane = (selector.get("lane") or "").strip()
    want_caps = {str(c).strip() for c in selector.get("capabilities") or [] if str(c).strip()}
    have_runtime = (runtime.get("runtime") or "").strip()
    if want_runtime and have_runtime != want_runtime:
        return False
    lanes = [str(x).strip() for x in runtime.get("lanes") or [] if str(x).strip()]
    if want_lane and lanes and want_lane not in lanes:
        return False
    caps = {str(c).strip() for c in runtime.get("capabilities") or [] if str(c).strip()}
    if want_caps and not want_caps.issubset(caps):
        return False
    return True


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


def _insert_wake_intent(c: sqlite3.Connection, selector: Dict[str, Any],
                        reason: str, source: str, policy: Dict[str, Any],
                        task_id: Optional[str], principal_id: str, actor: str,
                        now: float, idem_key: str = "", effect_key: str = "") -> Dict[str, Any]:
    deadline_s = (policy.get("deadline_seconds") or policy.get("claim_timeout_s") or
                  policy.get("ttl_s"))
    deadline = now + float(deadline_s) if deadline_s else None
    eligible = _eligible_hosts_in(c, selector, now)
    no_host_policy = (policy.get("no_eligible_host") or "wait").strip()
    status = "failed" if no_host_policy == "fail" and not eligible else "pending"
    result = ({"reason": "no_eligible_host", "eligible_host_count": 0}
              if status == "failed" else {})
    wake_id = "wake-" + uuid.uuid4().hex[:16]
    c.execute(
        "INSERT INTO wake_intents(wake_id, source, reason, selector_json, policy_json, "
        "status, requested_at, deadline, result_json, task_id, principal_id, idem_key, effect_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (wake_id, source, reason, json.dumps(selector, sort_keys=True),
         json.dumps(policy, sort_keys=True), status, now, deadline,
         json.dumps(result, sort_keys=True), task_id, principal_id or None,
         idem_key or None, effect_key or None),
    )
    payload = {"wake_id": wake_id, "source": source, "reason": reason,
               "selector": selector, "policy": policy, "status": status,
               "eligible_host_count": len(eligible), "effect_key": effect_key or None}
    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
              (task_id, actor, "wake.requested", json.dumps(payload, sort_keys=True), now))
    if not eligible:
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "wake.no_eligible_host",
                   json.dumps({"wake_id": wake_id, "selector": selector,
                               "status": status}, sort_keys=True), now))
    row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
    wake = _wake_row(row)
    wake["eligible_host_count"] = len(eligible)
    wake["eligible_hosts"] = [h["host_id"] for h in eligible]
    return wake


def request_wake(selector: Dict[str, Any], reason: str = "",
                 source: str = "", policy: Optional[Dict[str, Any]] = None,
                 task_id: Optional[str] = None, principal_id: str = "",
                 actor: str = "system", idem_key: str = "",
                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    started_at = time.time()
    now = time.time()
    policy = dict(policy or {})
    selector = dict(selector or {})
    if not selector.get("runtime") and selector.get("agent_id"):
        runtime = _selector_runtime_for_agent(str(selector.get("agent_id") or ""))
        if runtime:
            selector["runtime"] = runtime
    if not selector.get("runtime") and not selector.get("agent_id"):
        return {"error": "selector.runtime or selector.agent_id required"}
    payload = {"selector": selector, "reason": reason or "wake requested",
               "source": source or actor, "policy": policy, "task_id": task_id}
    try:
        with _control_plane_conn(project) as c:
            hit = _idem_hit(c, "request_wake", idem_key, actor, payload)
            if hit is not None:
                return hit
            effect_claim = _claim_external_effect_in(
                c, "wake", "agent_host", json.dumps(selector, sort_keys=True),
                payload, task_id=task_id, agent_id=selector.get("agent_id") or "",
                idem_key=idem_key, actor=actor, principal_id=principal_id,
                project=project, now=now)
            if not effect_claim.get("claimed"):
                out = {"requested": False, "reason": effect_claim.get("reason"),
                       "effect": effect_claim.get("effect"),
                       "effect_key": effect_claim.get("effect_key"),
                       "readback_required": effect_claim.get("readback_required", False)}
                if effect_claim.get("verified"):
                    out["verified"] = True
                    out["proof"] = effect_claim.get("proof")
                _idem_store(c, "request_wake", idem_key, actor, payload, out)
                return out
            wake = _insert_wake_intent(
                c, selector=selector, reason=reason or "wake requested",
                source=source or actor, policy=policy, task_id=task_id,
                principal_id=principal_id, actor=actor, now=now, idem_key=idem_key,
                effect_key=effect_claim["effect_key"])
            _update_external_effect_in(
                c, effect_claim["effect_key"], "issued",
                readback={"wake_id": wake["wake_id"], "wake_status": wake["status"]},
                actor=actor, task_id=task_id, project=project, now=now)
            wake["effect_key"] = effect_claim["effect_key"]
            _idem_store(c, "request_wake", idem_key, actor, payload, wake)
            return wake
    except sqlite3.OperationalError as exc:
        if _sqlite_busy(exc):
            return _control_plane_unavailable("request_wake", project, started_at, exc)
        raise


def list_wake_intents(status: str = "", host_id: str = "", runtime: str = "",
                      project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    started_at = time.time()
    q = "SELECT * FROM wake_intents WHERE 1=1"
    params: List[Any] = []
    if status:
        q += " AND status=?"; params.append(status)
    if host_id:
        q += " AND claimed_by_host=?"; params.append(host_id)
    q += " ORDER BY requested_at"
    try:
        with _control_plane_conn(project) as c:
            wakes = [_wake_row(r) for r in c.execute(q, params).fetchall()]
    except sqlite3.OperationalError as exc:
        if _sqlite_busy(exc):
            return [_control_plane_unavailable("list_wake_intents", project, started_at, exc)]
        raise
    if runtime:
        wakes = [w for w in wakes if (w.get("selector") or {}).get("runtime") == runtime]
    return wakes


def claim_wake(host_id: str, wake_id: str, actor: str = "system",
               project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    started_at = time.time()
    now = time.time()
    try:
        with _control_plane_conn(project) as c:
            wake_row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
            if not wake_row:
                return {"claimed": False, "error": "wake not found", "wake_id": wake_id}
            wake = _wake_row(wake_row)
            if wake["status"] != "pending":
                return {"claimed": False, "reason": f"wake is {wake['status']}", "wake": wake}
            if wake.get("deadline") and wake["deadline"] <= now:
                result = {"reason": "deadline_expired", "deadline": wake["deadline"]}
                c.execute("UPDATE wake_intents SET status='failed', completed_at=?, result_json=? "
                          "WHERE wake_id=?",
                          (now, json.dumps(result, sort_keys=True), wake_id))
                return {"claimed": False, "reason": "deadline_expired", "wake_id": wake_id}
            host_row = c.execute("SELECT * FROM agent_hosts WHERE host_id=?", (host_id,)).fetchone()
            if not host_row:
                return {"claimed": False, "reason": "host_not_registered", "host_id": host_id}
            host = _host_row(host_row, now=now)
            if not _host_can_handle(host, wake["selector"]):
                return {"claimed": False, "reason": "host_not_eligible",
                        "host_id": host_id, "wake_id": wake_id}
            cur = c.execute(
                "UPDATE wake_intents SET status='claimed', claimed_at=?, claimed_by_host=? "
                "WHERE wake_id=? AND status='pending'",
                (now, host_id, wake_id),
            )
            if cur.rowcount == 0:
                row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
                return {"claimed": False, "reason": "lost_race", "wake": _wake_row(row)}
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (wake.get("task_id"), actor, "wake.claimed",
                       json.dumps({"wake_id": wake_id, "host_id": host_id}, sort_keys=True), now))
            row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if _sqlite_busy(exc):
            err = _control_plane_unavailable("claim_wake", project, started_at, exc)
            return {"claimed": False, **err}
        raise
    return {"claimed": True, "wake": _wake_row(row)}


def complete_wake(wake_id: str, runner_session_id: str = "",
                  agent_id: str = "", result: Optional[Dict[str, Any]] = None,
                  actor: str = "system",
                  project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    started_at = time.time()
    now = time.time()
    result = dict(result or {})
    success = bool(result.get("started") or runner_session_id or agent_id)
    status = "completed" if success else "failed"
    if "reason" not in result:
        result["reason"] = "started" if success else "launch_failed"
    try:
        with _control_plane_conn(project) as c:
            row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
            if not row:
                return {"error": "wake not found", "wake_id": wake_id}
            wake = _wake_row(row)
            c.execute(
                "UPDATE wake_intents SET status=?, completed_at=?, runner_session_id=?, "
                "agent_id=?, result_json=? WHERE wake_id=?",
                (status, now, runner_session_id or None, agent_id or None,
                 json.dumps(result, sort_keys=True), wake_id),
            )
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (wake.get("task_id"), actor,
                       "wake.completed" if status == "completed" else "wake.failed",
                       json.dumps({"wake_id": wake_id, "status": status,
                                   "runner_session_id": runner_session_id or None,
                                   "agent_id": agent_id or None,
                                   "result": result}, sort_keys=True), now))
            if status == "completed" and runner_session_id:
                selector = wake.get("selector") or {}
                _upsert_runner_session_in(
                    c,
                    {
                        "runner_session_id": runner_session_id,
                        "host_id": wake.get("claimed_by_host") or "",
                        "agent_id": agent_id or selector.get("agent_id") or "",
                        "runtime": selector.get("runtime") or "",
                        "task_id": wake.get("task_id") or result.get("task_id") or "",
                        "claim_id": result.get("claim_id") or "",
                        "pid": result.get("pid"),
                        "status": "running" if result.get("started") else "unknown",
                        "cwd": result.get("cwd") or "",
                        "control": result.get("control") or {"managed_process": True,
                                                              "runner_kill": bool(runner_session_id)},
                        "metadata": {"wake_id": wake_id, "wake_result": result},
                        "heartbeat_ttl_s": result.get("heartbeat_ttl_s") or 60,
                    },
                    principal_id=actor,
                    actor=actor,
                    now=now,
                )
            if wake.get("effect_key"):
                effect_readback = {"wake_id": wake_id, "status": status,
                                   "runner_session_id": runner_session_id or None,
                                   "agent_id": agent_id or None, "result": result}
                _update_external_effect_in(
                    c, wake["effect_key"],
                    "verified" if status == "completed" else "failed",
                    readback=effect_readback,
                    last_error="" if status == "completed" else result.get("reason", "launch_failed"),
                    actor=actor, task_id=wake.get("task_id"), project=project, now=now)
            row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if _sqlite_busy(exc):
            return _control_plane_unavailable("complete_wake", project, started_at, exc)
        raise
    return _wake_row(row)


def upsert_runner_session(record: Dict[str, Any], principal_id: str = "",
                          actor: str = "system",
                          project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        return _upsert_runner_session_in(c, record, principal_id, actor, now)


def list_runner_sessions(host_id: str = "", runtime: str = "", task_id: str = "",
                         status: str = "", include_stale: bool = False,
                         project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    q = "SELECT * FROM runner_sessions WHERE 1=1"
    params: List[Any] = []
    if host_id:
        q += " AND host_id=?"; params.append(host_id)
    if runtime:
        q += " AND runtime=?"; params.append(runtime)
    if task_id:
        q += " AND task_id=?"; params.append(task_id)
    if status:
        q += " AND status=?"; params.append(status)
    q += " ORDER BY heartbeat_at DESC, runner_session_id"
    now = time.time()
    with _conn(project) as c:
        rows = c.execute(q, params).fetchall()
        sessions = [_runner_session_row(r, now=now, include_claim=True, c=c) for r in rows]
    if not include_stale:
        sessions = [s for s in sessions if not s.get("stale")]
    return sessions


def get_runner_session(runner_session_id: str,
                       project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM runner_sessions WHERE runner_session_id=?",
                        (runner_session_id,)).fetchone()
        return _runner_session_row(row, now=now, include_claim=True, c=c) if row else None


def request_runner_control(runner_session_id: str, action: str, reason: str = "",
                           options: Optional[Dict[str, Any]] = None,
                           actor: str = "system", principal_id: str = "",
                           project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    action = (action or "").strip().lower()
    if action not in RUNNER_CONTROL_ACTIONS:
        return {"requested": False, "error": "unsupported_action", "action": action}
    with _conn(project) as c:
        row = c.execute("SELECT * FROM runner_sessions WHERE runner_session_id=?",
                        (runner_session_id,)).fetchone()
        if not row:
            return {"requested": False, "error": "runner_session_not_found",
                    "runner_session_id": runner_session_id}
        session = _runner_session_row(row, now=now, include_claim=True, c=c)
        available = set(session.get("available_actions") or [])
        effect_payload = {
            "runner_session_id": runner_session_id,
            "host_id": session.get("host_id"),
            "action": action,
            "options": options or {},
        }
        effect_claim = _claim_external_effect_in(
            c, "runner_control", session.get("host_id") or "agent_host",
            f"{runner_session_id}:{action}", effect_payload,
            task_id=session.get("task_id") or None,
            claim_id=session.get("claim_id") or "",
            agent_id=session.get("agent_id") or "",
            actor=actor, principal_id=principal_id, project=project, now=now)
        if not effect_claim.get("claimed"):
            out = {"requested": False, "reason": effect_claim.get("reason"),
                   "effect": effect_claim.get("effect"),
                   "effect_key": effect_claim.get("effect_key"),
                   "readback_required": effect_claim.get("readback_required", False)}
            if effect_claim.get("verified"):
                out["verified"] = True
                out["proof"] = effect_claim.get("proof")
            return out
        request_id = "runnerreq-" + uuid.uuid4().hex[:16]
        snapshot = _runner_snapshot_from_session(session, reason=f"before_{action}")
        req_status = "pending" if action in available else "refused"
        result = {}
        if req_status == "refused":
            result = {
                "reason": "not_supported",
                "available_actions": sorted(available),
                "capabilities": (session.get("environment") or {}).get("capabilities") or {},
                "control": session.get("control") or {},
            }
        c.execute(
            "INSERT INTO runner_control_requests(request_id, runner_session_id, host_id, "
            "action, status, reason, requested_by, principal_id, requested_at, "
            "snapshot_json, result_json, options_json, effect_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                request_id,
                runner_session_id,
                session.get("host_id") or None,
                action,
                req_status,
                reason or f"operator requested {action}",
                actor,
                principal_id or None,
                now,
                json.dumps(snapshot, sort_keys=True),
                json.dumps(result, sort_keys=True),
                json.dumps(options or {}, sort_keys=True),
                effect_claim["effect_key"],
            ),
        )
        _update_external_effect_in(
            c, effect_claim["effect_key"], "issued" if req_status == "pending" else "failed",
            readback={"request_id": request_id, "status": req_status, "result": result},
            last_error="" if req_status == "pending" else "not_supported",
            actor=actor, task_id=session.get("task_id") or None, project=project, now=now)
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (session.get("task_id") or None, actor,
                   f"runner.{action}_{'requested' if req_status == 'pending' else 'refused'}",
                   json.dumps({"request_id": request_id,
                               "runner_session_id": runner_session_id,
                               "host_id": session.get("host_id"),
                               "status": req_status,
                               "reason": reason or "",
                               "effect_key": effect_claim["effect_key"],
                               "available_actions": sorted(available),
                               "snapshot": snapshot}, sort_keys=True), now))
        out = _runner_control_row(c.execute(
            "SELECT * FROM runner_control_requests WHERE request_id=?",
            (request_id,),
        ).fetchone())
    out["requested"] = req_status == "pending"
    return out


def list_runner_control_requests(status: str = "", host_id: str = "",
                                 runner_session_id: str = "",
                                 project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    q = "SELECT * FROM runner_control_requests WHERE 1=1"
    params: List[Any] = []
    if status:
        q += " AND status=?"; params.append(status)
    if host_id:
        q += " AND host_id=?"; params.append(host_id)
    if runner_session_id:
        q += " AND runner_session_id=?"; params.append(runner_session_id)
    q += " ORDER BY requested_at"
    with _conn(project) as c:
        return [_runner_control_row(r) for r in c.execute(q, params).fetchall()]


def claim_runner_control_request(host_id: str, request_id: str,
                                 actor: str = "system",
                                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM runner_control_requests WHERE request_id=?",
                        (request_id,)).fetchone()
        if not row:
            return {"claimed": False, "error": "runner_control_not_found",
                    "request_id": request_id}
        req = _runner_control_row(row)
        if req["status"] != "pending":
            return {"claimed": False, "reason": f"request is {req['status']}", "request": req}
        if req.get("host_id") and req["host_id"] != host_id:
            return {"claimed": False, "reason": "wrong_host", "host_id": host_id,
                    "request_host_id": req.get("host_id")}
        cur = c.execute(
            "UPDATE runner_control_requests SET status='claimed', claimed_at=?, "
            "claimed_by_host=? WHERE request_id=? AND status='pending'",
            (now, host_id, request_id),
        )
        if cur.rowcount == 0:
            row = c.execute("SELECT * FROM runner_control_requests WHERE request_id=?",
                            (request_id,)).fetchone()
            return {"claimed": False, "reason": "lost_race",
                    "request": _runner_control_row(row)}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "runner.control_claimed",
                   json.dumps({"request_id": request_id, "host_id": host_id}, sort_keys=True), now))
        row = c.execute("SELECT * FROM runner_control_requests WHERE request_id=?",
                        (request_id,)).fetchone()
    return {"claimed": True, "request": _runner_control_row(row)}


def complete_runner_control_request(request_id: str, result: Optional[Dict[str, Any]] = None,
                                    snapshot: Optional[Dict[str, Any]] = None,
                                    status: str = "",
                                    actor: str = "system",
                                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    result = dict(result or {})
    snapshot = dict(snapshot or {})
    with _conn(project) as c:
        row = c.execute("SELECT * FROM runner_control_requests WHERE request_id=?",
                        (request_id,)).fetchone()
        if not row:
            return {"error": "runner_control_not_found", "request_id": request_id}
        req = _runner_control_row(row)
        final_status = status or ("failed" if result.get("error") else "completed")
        if final_status not in {"completed", "failed", "cancelled"}:
            final_status = "completed"
        if not snapshot:
            snapshot = req.get("snapshot") or {}
        merged_result = {**(req.get("result") or {}), **result}
        c.execute(
            "UPDATE runner_control_requests SET status=?, completed_at=?, "
            "snapshot_json=?, result_json=? WHERE request_id=?",
            (final_status, now, json.dumps(snapshot, sort_keys=True),
             json.dumps(merged_result, sort_keys=True), request_id),
        )
        session_status = None
        if req.get("action") == "kill" and final_status == "completed":
            session_status = merged_result.get("status") or "killed"
        elif req.get("action") == "snapshot" and snapshot.get("status"):
            session_status = snapshot.get("status")
        sets = ["last_snapshot_json=?", "updated_at=?"]
        vals: List[Any] = [json.dumps(snapshot, sort_keys=True), now]
        if session_status:
            sets.append("status=?")
            vals.append(session_status)
        vals.append(req["runner_session_id"])
        c.execute(f"UPDATE runner_sessions SET {', '.join(sets)} WHERE runner_session_id=?", vals)
        session_row = c.execute("SELECT * FROM runner_sessions WHERE runner_session_id=?",
                                (req["runner_session_id"],)).fetchone()
        session = _runner_session_row(session_row, now=now, include_claim=True, c=c) if session_row else {}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (session.get("task_id") or None, actor, f"runner.{req['action']}_{final_status}",
                   json.dumps({"request_id": request_id,
                               "runner_session_id": req["runner_session_id"],
                               "effect_key": req.get("effect_key"),
                               "status": final_status,
                               "result": merged_result,
                               "snapshot": snapshot}, sort_keys=True), now))
        if req.get("effect_key"):
            _update_external_effect_in(
                c, req["effect_key"],
                "verified" if final_status == "completed" else "failed",
                readback={"request_id": request_id, "status": final_status,
                          "result": merged_result, "snapshot": snapshot},
                last_error="" if final_status == "completed" else merged_result.get("error", final_status),
                actor=actor, task_id=session.get("task_id") or None, project=project, now=now)
        row = c.execute("SELECT * FROM runner_control_requests WHERE request_id=?",
                        (request_id,)).fetchone()
    return _runner_control_row(row)


def cancel_wake(wake_id: str, reason: str = "cancelled", actor: str = "system",
                project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    started_at = time.time()
    now = time.time()
    try:
        with _control_plane_conn(project) as c:
            row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
            if not row:
                return {"error": "wake not found", "wake_id": wake_id}
            wake = _wake_row(row)
            if wake["status"] in ("completed", "failed", "cancelled"):
                return wake | {"note": "already terminal"}
            result = dict(wake.get("result") or {})
            result.update({"reason": reason, "cancelled_by": actor})
            c.execute("UPDATE wake_intents SET status='cancelled', completed_at=?, result_json=? "
                      "WHERE wake_id=?",
                      (now, json.dumps(result, sort_keys=True), wake_id))
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (wake.get("task_id"), actor, "wake.cancelled",
                       json.dumps({"wake_id": wake_id, "reason": reason}, sort_keys=True), now))
            if wake.get("effect_key"):
                _update_external_effect_in(
                    c, wake["effect_key"], "void",
                    readback={"wake_id": wake_id, "status": "cancelled", "reason": reason},
                    actor=actor, task_id=wake.get("task_id"), project=project, now=now)
            row = c.execute("SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if _sqlite_busy(exc):
            return _control_plane_unavailable("cancel_wake", project, started_at, exc)
        raise
    return _wake_row(row)


def sweep_wake_intents(project: str = DEFAULT_PROJECT,
                       now: Optional[float] = None) -> Dict[str, Any]:
    started_at = time.time()
    now = time.time() if now is None else float(now)
    failed = 0
    events: List[Dict[str, Any]] = []
    try:
        with _control_plane_conn(project) as c:
            rows = c.execute(
                "SELECT * FROM wake_intents WHERE status IN ('pending','claimed') "
                "AND deadline IS NOT NULL AND deadline<=?",
                (now,),
            ).fetchall()
            for row in rows:
                wake = _wake_row(row)
                result = dict(wake.get("result") or {})
                result.update({"reason": "deadline_expired", "deadline": wake.get("deadline")})
                c.execute("UPDATE wake_intents SET status='failed', completed_at=?, result_json=? "
                          "WHERE wake_id=?",
                          (now, json.dumps(result, sort_keys=True), wake["wake_id"]))
                c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                          (wake.get("task_id"), "switchboard/wake", "wake.failed",
                           json.dumps({"wake_id": wake["wake_id"], "reason": "deadline_expired"},
                                      sort_keys=True), now))
                if wake.get("effect_key"):
                    _update_external_effect_in(
                        c, wake["effect_key"], "failed",
                        readback={"wake_id": wake["wake_id"], "status": "failed",
                                  "reason": "deadline_expired"},
                        last_error="deadline_expired",
                        actor="switchboard/wake", task_id=wake.get("task_id"),
                        project=project, now=now)
                failed += 1
                events.append({"wake_id": wake["wake_id"], "status": "failed",
                               "reason": "deadline_expired"})
    except sqlite3.OperationalError as exc:
        if _sqlite_busy(exc):
            err = _control_plane_unavailable("sweep_wake_intents", project, started_at, exc)
            return {"project": project, "failed": failed, "events": events, **err}
        raise
    return {"project": project, "failed": failed, "events": events}


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


def _deps_done(task: Dict[str, Any], by_id: Dict[str, Dict[str, Any]]) -> bool:
    for dep in task.get("depends_on") or []:
        if by_id.get(dep, {}).get("status") != "Done":
            return False
    return True


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


def _same_pr_reference(current: Dict[str, Any], evidence_obj: Dict[str, Any]) -> bool:
    current_pr = current.get("pr_number")
    incoming_pr = evidence_obj.get("pr_number")
    if current_pr is not None and incoming_pr is not None and str(current_pr) == str(incoming_pr):
        return True
    current_url = (current.get("pr_url") or "").strip()
    incoming_url = (evidence_obj.get("pr_url") or "").strip()
    return bool(current_url and incoming_url and current_url == incoming_url)


def _preserve_provider_pr_evidence(current: Dict[str, Any],
                                   updates: Dict[str, Any],
                                   evidence_obj: Dict[str, Any]) -> Dict[str, Any]:
    """Keep webhook/GitHub PR evidence authoritative over later stale claim evidence."""
    if not _same_pr_reference(current, evidence_obj):
        return updates
    provider = {
        field: current.get(field)
        for field in ("branch", "head_sha", "pr_number", "pr_url")
        if current.get(field) not in (None, "")
    }
    if not provider:
        return updates
    claim_evidence = dict(evidence_obj)
    conflicts = {}
    for field, provider_value in provider.items():
        claim_value = evidence_obj.get(field)
        if claim_value not in (None, "") and str(claim_value) != str(provider_value):
            conflicts[field] = {"claim": claim_value, "provider": provider_value}
        updates[field] = provider_value
    if current.get("pushed_at"):
        updates["pushed_at"] = current.get("pushed_at")
    preserved_evidence = dict(evidence_obj)
    preserved_evidence.update(provider)
    if conflicts:
        preserved_evidence["claim_evidence"] = claim_evidence
        preserved_evidence["provider_evidence_preserved"] = {
            "source": "existing_pr_evidence",
            "conflicts": conflicts,
        }
    updates["evidence"] = preserved_evidence
    return updates


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


def _work_session_stale_lease_problems(session: Dict[str, Any],
                                       now: float) -> List[Dict[str, Any]]:
    problems: List[Dict[str, Any]] = []
    for field in ("file_leases", "resource_leases"):
        for lease in session.get(field) or []:
            if not isinstance(lease, dict):
                continue
            if lease.get("released_at") or str(lease.get("status") or "").lower() in {"released", "completed"}:
                continue
            expires_at = lease.get("expires_at")
            if expires_at in (None, ""):
                continue
            try:
                expired = float(expires_at) <= now
            except (TypeError, ValueError):
                expired = True
            if expired:
                problems.append({
                    "reason": "stale_work_session_lease",
                    "failure_class": "stale_branch",
                    "message": f"Work Session has an expired unreleased {field[:-1]}.",
                    "lease": lease,
                })
    return problems


def _complete_claim_work_session_gate_in(
        c: sqlite3.Connection, claim: sqlite3.Row, task: Dict[str, Any],
        evidence_obj: Dict[str, Any], project: str, now: float) -> Dict[str, Any]:
    row = c.execute(
        "SELECT * FROM work_sessions WHERE claim_id=? "
        "ORDER BY updated_at DESC, created_at DESC, work_session_id LIMIT 1",
        (claim["id"],),
    ).fetchone()
    if not row:
        row = _active_work_session_row_in(
            c, task_id=claim["task_id"], agent_id=claim["agent_id"], now=now)
    session = _work_session_row(row) if row else None
    requested_profile = (
        evidence_obj.get("session_policy_profile")
        or evidence_obj.get("policy_profile")
        or (session or {}).get("policy_profile")
        or evidence_obj.get("completion_profile")
        or ""
    )
    required, profile = _work_session_required(task, str(requested_profile or ""),
                                               project=project)
    rules = _session_policy_profile_rules(profile, project=project)
    if not rules:
        return _unknown_session_policy_profile(profile, project)
    if session and _session_policy_profile_rules(
            session.get("policy_profile") or profile, project=project).get("work_session_required"):
        required = True
        profile = _normalize_session_policy_profile(session.get("policy_profile") or profile)
        rules = _session_policy_profile_rules(profile, project=project)
    completion_profile = _normalize_session_policy_profile(
        str(evidence_obj.get("completion_profile") or ""))
    if completion_profile == "offline_evidence" and not required:
        if not (evidence_obj.get("offline_evidence") or evidence_obj.get("artifact_url") or evidence_obj.get("verification")):
            return _work_session_failure(
                "missing_offline_evidence",
                "Offline completion profile requires explicit evidence before claim completion.",
                "missing_data",
                details={"required": False, "policy_profile": completion_profile},
            )
        return {"ok": True, "required": False, "policy_profile": completion_profile,
                "source": "offline_profile", "work_session": None}
    if not required:
        return {"ok": True, "required": False, "policy_profile": profile,
                "source": "not_required", "work_session": session}
    if not session:
        return _work_session_failure(
            "work_session_required",
            "A bound Work Session is required before completing code-strict work.",
            "missing_data",
            details={"required": True, "policy_profile": profile},
        )

    allow_dirty = _evidence_truthy(evidence_obj.get("allow_dirty"))
    if allow_dirty and not str(evidence_obj.get("allow_dirty_reason") or "").strip():
        return _work_session_failure(
            "missing_dirty_allowance_reason",
            "Dirty completion requires allow_dirty_reason evidence.",
            "missing_data",
            details={"required": True, "policy_profile": profile,
                     "work_session_id": session.get("work_session_id")},
        )
    state = _validate_work_session_claim_state(
        session, task, claim["agent_id"], project, required=required, profile=profile,
        source="complete_claim", normalized_payload=None, now=now,
        allow_dirty=allow_dirty)
    if not state.get("ok"):
        return state

    problems: List[Dict[str, Any]] = []
    evidence_branch = str(evidence_obj.get("branch") or "").strip()
    evidence_head = str(evidence_obj.get("head_sha") or "").strip()
    session_branch = str(session.get("branch") or "").strip()
    session_head = str(session.get("head_sha") or "").strip()
    if rules.get("merge_authority") != "offline_verifier" and not evidence_branch:
        problems.append({"reason": "missing_completion_branch", "failure_class": "missing_data",
                         "message": "Completion evidence must include branch."})
    elif session_branch and evidence_branch != session_branch:
        problems.append({"reason": "stale_branch", "failure_class": "stale_branch",
                         "message": "Completion branch does not match the bound Work Session.",
                         "evidence_branch": evidence_branch, "work_session_branch": session_branch})
    if rules.get("merge_authority") != "offline_verifier" and not evidence_head:
        problems.append({"reason": "missing_completion_head_sha", "failure_class": "missing_data",
                         "message": "Completion evidence must include head_sha."})
    elif not session_head:
        problems.append({"reason": "missing_work_session_head_sha", "failure_class": "missing_data",
                         "message": "Bound Work Session must record head_sha before completion."})
    elif evidence_head != session_head:
        problems.append({"reason": "stale_head_sha", "failure_class": "stale_branch",
                         "message": "Completion head_sha does not match the bound Work Session.",
                         "evidence_head_sha": evidence_head, "work_session_head_sha": session_head})
    if rules.get("merge_authority") != "none" and not _completion_has_push_or_review_evidence(evidence_obj):
        problems.append({"reason": "missing_push_or_review_evidence",
                         "failure_class": "missing_data",
                         "message": "Completion evidence must include PR, pushed branch, or offline evidence."})
    executed_test_gate = None
    if rules.get("requires_executed_tests"):
        executed_test_gate = _executed_test_run_gate(evidence_obj, session)
        if not executed_test_gate.get("ok"):
            problems.append({"reason": executed_test_gate.get("reason") or "missing_executed_test_run",
                             "failure_class": "missing_data",
                             "message": executed_test_gate.get("message"),
                             "executed_test_gate": executed_test_gate})
    elif rules.get("requires_tests") and not _completion_evidence_has_tests(evidence_obj, session):
        problems.append({"reason": "missing_test_evidence", "failure_class": "missing_data",
                         "message": "Completion evidence must record relevant tests or verification."})
    if rules.get("requires_diff_check") and not _completion_evidence_has_diff_check(evidence_obj, session):
        problems.append({"reason": "missing_diff_check", "failure_class": "missing_data",
                         "message": "Completion evidence must record git diff --check as clean."})
    problems.extend(_work_session_stale_lease_problems(session, now))
    if problems:
        first = problems[0]
        return _work_session_failure(
            first["reason"], first["message"], first["failure_class"],
            details={"problems": problems, "required": required,
                     "policy_profile": profile,
                     "work_session_id": session.get("work_session_id")},
        )
    response = {"ok": True, "required": required, "policy_profile": profile,
            "policy": rules,
            "source": "complete_claim", "work_session": session,
            "allow_dirty": allow_dirty}
    if executed_test_gate:
        response["executed_test_gate"] = executed_test_gate
    return response


def _task_tally_snapshot(c: sqlite3.Connection, task_id: str) -> Dict[str, Any]:
    outcomes = [_outcome_row(r) for r in c.execute(
        "SELECT * FROM outcomes WHERE task_id=?", (task_id,)).fetchall()]
    return {"spend": _spend_summary(_spend_for_task(c, task_id, outcomes)),
            "outcomes": outcomes}


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


READY_TASK_STATUSES = {"Not Started", "Ready", "Todo", "Backlog"}


def claim_task(task_id: str, agent_id: str,
               principal_id: str = "", actor: str = "system",
               ttl_seconds: int = 1800, idem_key: str = "",
               override_identity_risk: bool = False,
               work_session_id: str = "", work_session: Any = None,
               session_policy_profile: str = "",
               require_work_session: bool = False,
               project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Atomically claim one specific ready, unblocked task.

    Use this when a human/operator has already selected the task. Unlike claim_next,
    this never substitutes a different scheduler-preferred task.
    """
    now = time.time()
    task_id = (task_id or "").strip()
    payload = {"task_id": task_id, "agent_id": agent_id,
               "ttl_seconds": ttl_seconds,
               "override_identity_risk": bool(override_identity_risk),
               "work_session_id": work_session_id,
               "work_session": work_session,
               "session_policy_profile": session_policy_profile,
               "require_work_session": bool(require_work_session)}
    with _conn(project) as c:
        hit = _idem_hit(c, "claim_task", idem_key, actor, payload)
        if hit is not None:
            return hit
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            response = {"claimed": False, "reason": "task_not_found", "task_id": task_id}
            _idem_store(c, "claim_task", idem_key, actor, payload, response)
            return response
        task = _task_row(row)
        active = c.execute(
            "SELECT * FROM task_claims WHERE task_id=? AND status='active' AND expires_at>?",
            (task_id, now),
        ).fetchone()
        if active:
            response = {"claimed": False, "reason": "active_claim",
                        "task_id": task_id, "claim_id": active["id"],
                        "agent_id": active["agent_id"]}
            _idem_store(c, "claim_task", idem_key, actor, payload, response)
            return response
        if task.get("status") not in READY_TASK_STATUSES:
            response = {"claimed": False, "reason": "status_not_ready",
                        "task_id": task_id, "status": task.get("status")}
            _idem_store(c, "claim_task", idem_key, actor, payload, response)
            return response
        rows = c.execute("SELECT * FROM tasks ORDER BY sort_order, task_id").fetchall()
        by_id = {t["task_id"]: t for t in [_task_row(r) for r in rows]}
        if not _deps_done(task, by_id):
            response = {"claimed": False, "reason": "dependencies_unmet",
                        "task_id": task_id, "depends_on": task.get("depends_on") or []}
            _idem_store(c, "claim_task", idem_key, actor, payload, response)
            return response
        gate = _task_human_gate_state(task)
        if gate["blocked"]:
            response = {"claimed": False, "reason": "human_approval_required",
                        "task_id": task_id, "human_gate": gate}
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (task_id, actor, "task.claim_blocked_human_gate",
                       json.dumps({"agent_id": agent_id, **response}, sort_keys=True), now))
            _idem_store(c, "claim_task", idem_key, actor, payload, response)
            return response
        risk = _identity_takeover_risk_in(c, task_id, now)
        if risk and not override_identity_risk:
            response = {"claimed": False, **risk,
                        "override_field": "override_identity_risk",
                        "override_required": True}
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (task_id, "switchboard/identity", "task.claim_blocked_identity",
                       json.dumps({"agent_id": agent_id, **response}, sort_keys=True), now))
            _idem_store(c, "claim_task", idem_key, actor, payload, response)
            return response

        session_verdict = _validate_work_session_claim_binding_in(
            c, task, agent_id, project=project,
            work_session_id=work_session_id,
            work_session=work_session,
            policy_profile=session_policy_profile,
            require_work_session=require_work_session,
            now=now)
        if not session_verdict.get("ok"):
            response = {"claimed": False,
                        "reason": session_verdict.get("reason") or "invalid_work_session",
                        "failure_class": session_verdict.get("failure_class"),
                        "severity": session_verdict.get("severity"),
                        "message": session_verdict.get("message"),
                        "task_id": task_id,
                        "work_session": session_verdict,
                        "override_field": "work_session",
                        "override_required": True}
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (task_id, actor, "task.claim_blocked_work_session",
                       json.dumps({"agent_id": agent_id, **response}, sort_keys=True), now))
            _idem_store(c, "claim_task", idem_key, actor, payload, response)
            return response

        claim_id = "taskclaim-" + uuid.uuid4().hex[:16]
        lease_id = "lease-" + uuid.uuid4().hex[:16]
        ttl = max(60, int(ttl_seconds or 1800))
        expires_at = now + ttl
        c.execute(
            "INSERT INTO task_claims(id, task_id, agent_id, principal_id, status, claimed_at, "
            "expires_at, idem_key) VALUES (?,?,?,?,?,?,?,?)",
            (claim_id, task_id, agent_id, principal_id or None, "active",
             now, expires_at, idem_key or None),
        )
        c.execute(
            "INSERT INTO resource_leases(id, agent_id, principal_id, task_id, resource_type, "
            "names, claimed_at, ttl_seconds) VALUES (?,?,?,?,?,?,?,?)",
            (lease_id, agent_id, principal_id or None, task_id, "task",
             json.dumps([task_id]), now, ttl),
        )
        c.execute("UPDATE tasks SET status='In Progress', assignee=?, updated_at=? WHERE task_id=?",
                  (agent_id, now, task_id))
        dispatch_reason = {"policy": "exact.v1", "requested_task_id": task_id,
                           "dependency_checked": True}
        if risk and override_identity_risk:
            dispatch_reason["identity_override"] = risk
        work_session_binding = _attach_work_session_claim_in(
            c, session_verdict, claim_id, task_id, agent_id, actor,
            principal_id=principal_id, project=project, now=now)
        if work_session_binding.get("error"):
            response = {"claimed": False, "reason": "work_session_bind_failed",
                        "task_id": task_id, "work_session": work_session_binding}
            _idem_store(c, "claim_task", idem_key, actor, payload, response)
            return response
        dispatch_reason["work_session"] = work_session_binding
        payload_event = {"claim_id": claim_id, "lease_id": lease_id,
                         "task_id": task_id, "agent_id": agent_id,
                         "dispatch_reason": dispatch_reason}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "task.claimed",
                   json.dumps(payload_event, sort_keys=True), now))
        claimed_task = _task_row(c.execute("SELECT * FROM tasks WHERE task_id=?",
                                           (task_id,)).fetchone())
        response = {
            "claimed": True,
            "claim_id": claim_id,
            "task": claimed_task,
            "lease": {"lease_id": lease_id, "resource_type": "task",
                      "names": [task_id], "expires_at": expires_at},
            "dispatch_reason": dispatch_reason,
            "work_session_id": work_session_binding.get("work_session_id"),
            "work_session": work_session_binding,
        }
        _idem_store(c, "claim_task", idem_key, actor, payload, response)
        return response


def claim_next(agent_id: str, lanes: Any = None,
               capabilities: Any = None,
               max_risk: str = "", max_budget_usd: Optional[float] = None,
               principal_id: str = "", actor: str = "system",
               ttl_seconds: int = 1800, idem_key: str = "",
               override_identity_risk: bool = False,
               work_session_id: str = "", work_session: Any = None,
               session_policy_profile: str = "",
               require_work_session: bool = False,
               project: str = DEFAULT_PROJECT,
               deliverable_id: str = "", board_id: str = "",
               mission_id: str = "", milestone_id: str = "") -> Dict[str, Any]:
    """Atomically claim the highest-priority unblocked task for an agent.

    This is the first TXP slice: deterministic, dependency-aware, and intentionally
    conservative. More sophisticated cost/reliability scoring can layer onto the same
    task_claims/activity records.

    When deliverable_id or board_id/mission_id is provided, only linked mission tasks
    are eligible — the scheduler never wanders outside that deliverable scope.
    """
    if (deliverable_id or board_id or mission_id):
        return _claim_next_mission_scoped(
            agent_id, lanes=lanes, capabilities=capabilities,
            max_risk=max_risk, max_budget_usd=max_budget_usd,
            principal_id=principal_id, actor=actor,
            ttl_seconds=ttl_seconds, idem_key=idem_key,
            override_identity_risk=override_identity_risk,
            work_session_id=work_session_id,
            work_session=work_session,
            session_policy_profile=session_policy_profile,
            require_work_session=require_work_session,
            mission_project=project,
            deliverable_id=deliverable_id, board_id=board_id,
            mission_id=mission_id, milestone_id=milestone_id)
    now = time.time()
    lanes = coerce_csv_list(lanes)
    capabilities = coerce_csv_list(capabilities)
    lane_set = {x.strip().upper() for x in lanes}
    cap_set = {x.strip().lower() for x in capabilities}
    max_risk_value = _risk_value(max_risk)
    payload = {"agent_id": agent_id, "lanes": sorted(lane_set),
               "capabilities": sorted(capabilities or []), "max_risk": max_risk,
               "max_budget_usd": max_budget_usd, "ttl_seconds": ttl_seconds,
               "override_identity_risk": bool(override_identity_risk),
               "work_session_id": work_session_id,
               "work_session": work_session,
               "session_policy_profile": session_policy_profile,
               "require_work_session": bool(require_work_session)}
    with _conn(project) as c:
        hit = _idem_hit(c, "claim_next", idem_key, actor, payload)
        if hit is not None:
            return hit
        active_claims = {
            r["task_id"] for r in c.execute(
                "SELECT task_id FROM task_claims WHERE status='active' AND expires_at>?",
                (now,),
            ).fetchall()
        }
        rows = c.execute("SELECT * FROM tasks ORDER BY sort_order, task_id").fetchall()
        tasks = [_task_row(r) for r in rows]
        by_id = {t["task_id"]: t for t in tasks}
        eligible = []
        skipped = {"active_claim": 0, "status": 0, "lane": 0, "dependencies": 0,
                   "human_approval": 0, "capability_mismatch": 0, "risk": 0, "budget": 0,
                   "identity_unknown": 0, "work_session": 0}
        identity_risks: Dict[str, Dict[str, Any]] = {}
        human_gates: Dict[str, Dict[str, Any]] = {}
        work_session_findings: Dict[str, Dict[str, Any]] = {}
        for t in tasks:
            if t["task_id"] in active_claims:
                skipped["active_claim"] += 1
                continue
            if t.get("status") not in READY_TASK_STATUSES:
                skipped["status"] += 1
                continue
            if lane_set and (t.get("_wsId") or "").upper() not in lane_set:
                skipped["lane"] += 1
                continue
            if not _deps_done(t, by_id):
                skipped["dependencies"] += 1
                continue
            gate = _task_human_gate_state(t)
            if gate["blocked"]:
                skipped["human_approval"] += 1
                human_gates[t["task_id"]] = gate
                continue
            identity_risk = _identity_takeover_risk_in(c, t["task_id"], now)
            if identity_risk and not override_identity_risk:
                skipped["identity_unknown"] += 1
                identity_risks[t["task_id"]] = identity_risk
                continue
            session_verdict = _validate_work_session_claim_binding_in(
                c, t, agent_id, project=project,
                work_session_id=work_session_id,
                work_session=work_session,
                policy_profile=session_policy_profile,
                require_work_session=require_work_session,
                now=now)
            if not session_verdict.get("ok"):
                skipped["work_session"] += 1
                work_session_findings[t["task_id"]] = session_verdict
                continue
            required_caps = _task_required_capabilities(t)
            if required_caps and not set(required_caps).issubset(cap_set):
                skipped["capability_mismatch"] += 1
                continue
            if max_risk_value and _risk_value(t.get("risk_level") or "") > max_risk_value:
                skipped["risk"] += 1
                continue
            tally = _task_tally_snapshot(c, t["task_id"])
            score = _dispatch_score(t, lane_set, cap_set, tally, max_budget_usd)
            if score["budget"]["status"] == "over_budget":
                skipped["budget"] += 1
                continue
            if identity_risk and override_identity_risk:
                score["identity_override"] = identity_risk
            eligible.append((score["score"], -int(t.get("sort_order") or 0), t["task_id"], t, score))
        if not eligible:
            response = {"claimed": False, "reason": "no_unblocked_work",
                        "retry_after_seconds": 60,
                        "cursor": c.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0],
                        "dispatch_reason": {"policy": "score.v1", "skipped": skipped,
                                            "candidate_count": 0,
                                            "human_gates": human_gates,
                                            "identity_risks": identity_risks,
                                            "work_session_findings": work_session_findings}}
            _idem_store(c, "claim_next", idem_key, actor, payload, response)
            return response
        _, _, _, task, selected_score = sorted(
            eligible, key=lambda x: (-x[0], -x[1], x[2]))[0]
        claim_id = "taskclaim-" + uuid.uuid4().hex[:16]
        lease_id = "lease-" + uuid.uuid4().hex[:16]
        expires_at = now + max(60, int(ttl_seconds or 1800))
        c.execute(
            "INSERT INTO task_claims(id, task_id, agent_id, principal_id, status, claimed_at, "
            "expires_at, idem_key) VALUES (?,?,?,?,?,?,?,?)",
            (claim_id, task["task_id"], agent_id, principal_id or None, "active",
             now, expires_at, idem_key or None),
        )
        c.execute(
            "INSERT INTO resource_leases(id, agent_id, principal_id, task_id, resource_type, "
            "names, claimed_at, ttl_seconds) VALUES (?,?,?,?,?,?,?,?)",
            (lease_id, agent_id, principal_id or None, task["task_id"], "task",
             json.dumps([task["task_id"]]), now, max(60, int(ttl_seconds or 1800))),
        )
        c.execute("UPDATE tasks SET status='In Progress', assignee=?, updated_at=? WHERE task_id=?",
                  (agent_id, now, task["task_id"]))
        dispatch_reason = {"policy": "score.v1",
                           "score": selected_score["score"],
                           "factors": selected_score["factors"],
                           "required_capabilities": selected_score["required_capabilities"],
                           "matched_capabilities": selected_score["matched_capabilities"],
                           "skipped": skipped,
                           "candidate_count": len(eligible)}
        if selected_score.get("identity_override"):
            dispatch_reason["identity_override"] = selected_score["identity_override"]
        session_verdict = _validate_work_session_claim_binding_in(
            c, task, agent_id, project=project,
            work_session_id=work_session_id,
            work_session=work_session,
            policy_profile=session_policy_profile,
            require_work_session=require_work_session,
            now=now)
        work_session_binding = _attach_work_session_claim_in(
            c, session_verdict, claim_id, task["task_id"], agent_id, actor,
            principal_id=principal_id, project=project, now=now)
        if work_session_binding.get("error"):
            response = {"claimed": False, "reason": "work_session_bind_failed",
                        "task_id": task["task_id"], "work_session": work_session_binding}
            _idem_store(c, "claim_next", idem_key, actor, payload, response)
            return response
        dispatch_reason["work_session"] = work_session_binding
        payload_event = {"claim_id": claim_id, "lease_id": lease_id,
                         "task_id": task["task_id"], "agent_id": agent_id,
                         "dispatch_reason": dispatch_reason}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task["task_id"], actor, "task.claimed",
                   json.dumps(payload_event, sort_keys=True), now))
        claimed_task = _task_row(c.execute("SELECT * FROM tasks WHERE task_id=?",
                                           (task["task_id"],)).fetchone())
        response = {
            "claimed": True,
            "claim_id": claim_id,
            "task": claimed_task,
            "lease": {"lease_id": lease_id, "resource_type": "task",
                      "names": [task["task_id"]], "expires_at": expires_at},
            "budget": selected_score["budget"],
            "dispatch_reason": dispatch_reason,
            "recommendation": _model_recommendation(task, selected_score),
            "work_session_id": work_session_binding.get("work_session_id"),
            "work_session": work_session_binding,
        }
        _idem_store(c, "claim_next", idem_key, actor, payload, response)
        return response


def complete_claim(claim_id: str, evidence: str = "", final_status: str = "",
                   actor: str = "system",
                   project: str = DEFAULT_PROJECT,
                   mission_project: str = "") -> Dict[str, Any]:
    now = time.time()
    evidence_obj = _parse_evidence(evidence)
    requested_status = (final_status or evidence_obj.get("final_status") or evidence_obj.get("status") or "").strip()
    done_requested = requested_status.lower() == "done" or str(evidence_obj.get("done", "")).lower() in ("1", "true", "yes")
    if done_requested and not evidence_obj:
        return {"error": "evidence required for final_status=Done", "claim_id": claim_id}
    done_gate = None
    if done_requested:
        done_gate = {
            "code": "done_requires_merge_provenance",
            "message": "Agent completion records evidence and moves to In Review; Done requires GitHub/default-branch merge provenance.",
            "requested_status": requested_status or "Done",
        }
    next_status = "In Review"
    pushed_at = evidence_obj.get("pushed_at")
    if pushed_at is None and evidence_obj.get("head_sha"):
        pushed_at = now
    merged_at = evidence_obj.get("merged_at")
    if merged_at is None and evidence_obj.get("merged_sha"):
        merged_at = now
    with _conn(project) as c:
        row = c.execute("SELECT * FROM task_claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            return {"error": "claim not found", "claim_id": claim_id}
        if row["status"] != "active":
            return {"error": "claim is not active", "claim_id": claim_id,
                    "status": row["status"]}
        task_row = c.execute("SELECT * FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone()
        task_for_gate = _task_row(task_row) if task_row else {"task_id": row["task_id"]}
        work_session_gate = _complete_claim_work_session_gate_in(
            c, row, task_for_gate, evidence_obj, project, now)
        if not work_session_gate.get("ok"):
            response = {"completed": False,
                        "reason": work_session_gate.get("reason") or "work_session_completion_gate_failed",
                        "failure_class": work_session_gate.get("failure_class"),
                        "severity": work_session_gate.get("severity"),
                        "message": work_session_gate.get("message"),
                        "claim_id": claim_id,
                        "task_id": row["task_id"],
                        "work_session_gate": work_session_gate,
                        "override_field": "completion_evidence",
                        "override_required": True}
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"], actor, "task.complete_blocked_work_session",
                       json.dumps({"evidence": evidence_obj, **response}, sort_keys=True), now))
            return response
        c.execute("UPDATE task_claims SET status='completed', completed_at=? WHERE id=?",
                  (now, claim_id))
        c.execute("UPDATE resource_leases SET released_at=? WHERE resource_type='task' "
                  "AND task_id=? AND agent_id=? AND released_at IS NULL",
                  (now, row["task_id"], row["agent_id"]))
        c.execute("UPDATE tasks SET status=?, updated_at=? WHERE task_id=? "
                  "AND status NOT IN ('Done', 'Cancelled', 'Canceled')",
                  (next_status, now, row["task_id"]))
        current_git = _load_git_state(c, row["task_id"])
        git_updates = {
            "branch": evidence_obj.get("branch"),
            "head_sha": evidence_obj.get("head_sha"),
            "pushed_at": pushed_at,
            "pr_number": evidence_obj.get("pr_number"),
            "pr_url": evidence_obj.get("pr_url"),
            "merged_sha": evidence_obj.get("merged_sha"),
            "merged_at": merged_at,
            "in_main_content": True if evidence_obj.get("merged_sha") else None,
            "evidence": evidence_obj,
        }
        git_updates = _preserve_provider_pr_evidence(current_git, git_updates, evidence_obj)
        git_state = _upsert_git_state(c, row["task_id"], git_updates)
        task_snapshot_row = c.execute("SELECT * FROM tasks WHERE task_id=?",
                                      (row["task_id"],)).fetchone()
        task_snapshot = _task_row(task_snapshot_row) if task_snapshot_row else {"task_id": row["task_id"]}
        task_snapshot["git_state"] = git_state
        external_ci_gate = _external_ci_review_gate(
            task_snapshot, evidence=evidence_obj, c=c, project=project)
        publication_gate = _publication_review_gate(
            task_snapshot, evidence=evidence_obj, c=c, project=project)
        status_row = c.execute("SELECT status FROM tasks WHERE task_id=?",
                               (row["task_id"],)).fetchone()
        stored_status = status_row["status"] if status_row else next_status
        terminal_status_preserved = (
            stored_status == "Done" and _has_done_provenance(git_state)
        )
        if terminal_status_preserved:
            next_status = "Done"
        elif stored_status in ("Cancelled", "Canceled"):
            next_status = stored_status
        gated_work_session = work_session_gate.get("work_session") or {}
        if work_session_gate.get("required") and gated_work_session.get("work_session_id"):
            c.execute(
                "UPDATE work_sessions SET status='completed', completed_at=?, updated_by=?, updated_at=? "
                "WHERE work_session_id=?",
                (now, actor, now, gated_work_session["work_session_id"]),
            )
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"], actor, "work_session.completed",
                       json.dumps({"work_session_id": gated_work_session["work_session_id"],
                                   "claim_id": claim_id,
                                   "source": "complete_claim",
                                   "policy_profile": work_session_gate.get("policy_profile")},
                                  sort_keys=True), now))
        if done_gate:
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"], actor, "task.done_blocked",
                       json.dumps({"claim_id": claim_id, "evidence": evidence_obj,
                                   "done_gate": done_gate,
                                   "source": "complete_claim"}, sort_keys=True), now))
        if external_ci_gate.get("required"):
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"], actor, "task.review_gate",
                       json.dumps({"claim_id": claim_id,
                                   "gate": external_ci_gate.get("gate"),
                                   "external_ci": external_ci_gate,
                                   "source": "complete_claim"}, sort_keys=True), now))
        if publication_gate.get("required"):
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"], actor, "task.review_gate",
                       json.dumps({"claim_id": claim_id,
                                   "gate": publication_gate.get("gate"),
                                   "publication": publication_gate,
                                   "source": "complete_claim"}, sort_keys=True), now))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (row["task_id"], actor, "task.claim.completed",
                   json.dumps({"claim_id": claim_id, "evidence": evidence_obj,
                               "requested_status": requested_status or None,
                               "next_status": next_status,
                               "done_gate": done_gate,
                               "review_gate": (
                                   external_ci_gate.get("gate")
                                   if external_ci_gate.get("required") else
                                   publication_gate.get("gate")
                                   if publication_gate.get("required") else None),
                               "review_gates": [
                                   gate for gate in (
                                       external_ci_gate.get("gate")
                                       if external_ci_gate.get("required") else None,
                                       publication_gate.get("gate")
                                       if publication_gate.get("required") else None,
                                   ) if gate
                               ],
                               "work_session_gate": {
                                   key: value for key, value in work_session_gate.items()
                                   if key != "work_session"
                               },
                               "terminal_status_preserved": terminal_status_preserved},
                              sort_keys=True), now))
    response = {"completed": True, "claim_id": claim_id, "task_id": row["task_id"],
                "status": next_status, "git_state": git_state,
                "work_session_gate": {
                    key: value for key, value in work_session_gate.items()
                    if key != "work_session"
                }}
    if external_ci_gate.get("required"):
        response["review_gate"] = external_ci_gate.get("gate")
        response["external_ci"] = external_ci_gate
    if publication_gate.get("required"):
        response.setdefault("review_gate", publication_gate.get("gate"))
        response["publication"] = publication_gate
        response["review_gates"] = [
            gate for gate in (
                response.get("review_gate") if external_ci_gate.get("required") else None,
                publication_gate.get("gate"),
            ) if gate
        ]
    if done_gate:
        response["done_gate"] = done_gate
        response["warning"] = done_gate["message"]
    deliverable_id = (evidence_obj.get("deliverable_id") or "").strip()
    milestone_id = (evidence_obj.get("milestone_id") or "").strip()
    mp = (evidence_obj.get("mission_project") or mission_project or "").strip()
    if not deliverable_id or not mp:
        matches = _find_deliverable_links_for_task(project, row["task_id"],
                                                   mission_project=mp,
                                                   deliverable_id=deliverable_id)
        if len(matches) == 1:
            deliverable_id = matches[0]["deliverable_id"]
            mp = matches[0]["mission_project"]
            if not milestone_id:
                milestone_id = (matches[0].get("milestone_id") or "").strip()
    if deliverable_id and mp:
        response["mission"] = _record_mission_claim_completion(
            mp, deliverable_id, project, row["task_id"], claim_id, next_status,
            milestone_id=milestone_id, actor=actor)
    return response


def abandon_claim(claim_id: str, reason: str,
                  actor: str = "system",
                  project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM task_claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            return {"error": "claim not found", "claim_id": claim_id}
        if row["status"] != "active":
            return {"error": "claim is not active", "claim_id": claim_id,
                    "status": row["status"]}
        c.execute("UPDATE task_claims SET status='abandoned', abandon_reason=? WHERE id=?",
                  (reason, claim_id))
        c.execute("UPDATE resource_leases SET released_at=? WHERE resource_type='task' "
                  "AND task_id=? AND agent_id=? AND released_at IS NULL",
                  (now, row["task_id"], row["agent_id"]))
        c.execute("UPDATE tasks SET status='Not Started', "
                  "assignee=CASE WHEN assignee=? THEN NULL ELSE assignee END, "
                  "updated_at=? WHERE task_id=? AND status='In Progress'",
                  (row["agent_id"], now, row["task_id"]))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (row["task_id"], actor, "task.claim.abandoned",
                   json.dumps({"claim_id": claim_id, "reason": reason}, sort_keys=True), now))
    return {"abandoned": True, "claim_id": claim_id, "task_id": row["task_id"]}


def revoke_claim(claim_id: str, reason: str,
                 reassign_to: str = "", sort_order: Optional[int] = None,
                 partial_evidence: Any = None, notify: bool = True,
                 ack_deadline_minutes: float = 5,
                 expected_task_id: str = "",
                 actor: str = "switchboard/operator",
                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Operator override for an active task claim.

    Unlike abandon_claim(), revoke_claim() records that a human/operator took
    control, preserves partial evidence, optionally redirects the task, and
    sends the displaced holder an ack-required stop signal.
    """
    now = time.time()
    reason = (reason or "").strip() or "operator override"
    reassignee = (reassign_to or "").strip()
    evidence_obj = _parse_evidence(partial_evidence)
    notification = None
    with _conn(project) as c:
        row = c.execute("SELECT * FROM task_claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            return {"error": "claim not found", "claim_id": claim_id}
        if row["status"] != "active":
            return {"error": "claim is not active", "claim_id": claim_id,
                    "status": row["status"]}
        task_id = row["task_id"]
        if expected_task_id and task_id != expected_task_id:
            return {"error": "claim belongs to a different task", "claim_id": claim_id,
                    "task_id": task_id, "expected_task_id": expected_task_id}
        task = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not task:
            return {"error": "task not found", "task_id": task_id, "claim_id": claim_id}

        c.execute(
            "UPDATE task_claims SET status='revoked', completed_at=?, abandon_reason=? "
            "WHERE id=?",
            (now, f"revoked by {actor}: {reason}", claim_id),
        )
        c.execute(
            "UPDATE resource_leases SET released_at=? WHERE resource_type='task' "
            "AND task_id=? AND agent_id=? AND released_at IS NULL",
            (now, task_id, row["agent_id"]),
        )

        sets = ["status='Not Started'", "assignee=?", "updated_at=?"]
        vals: List[Any] = [reassignee or None, now]
        if sort_order is not None:
            sets.append("sort_order=?")
            vals.append(int(sort_order))
        vals.append(task_id)
        c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=? "
                  "AND status NOT IN ('Done', 'Cancelled', 'Canceled')", vals)

        git_state = None
        if evidence_obj:
            git_updates = {
                "branch": evidence_obj.get("branch"),
                "head_sha": evidence_obj.get("head_sha"),
                "pushed_at": now if evidence_obj.get("head_sha") else None,
                "pr_number": evidence_obj.get("pr_number"),
                "pr_url": evidence_obj.get("pr_url"),
                "evidence": {"operator_revoke": evidence_obj},
            }
            if any(v is not None for v in git_updates.values()):
                git_state = _upsert_git_state(c, task_id, git_updates)

        payload = {
            "claim_id": claim_id,
            "task_id": task_id,
            "revoked_agent": row["agent_id"],
            "reason": reason,
            "reassigned_to": reassignee or None,
            "sort_order": sort_order,
            "partial_evidence": evidence_obj,
        }
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "task.claim.revoked",
                   json.dumps(payload, sort_keys=True), now))
        updated_task = _task_row(c.execute("SELECT * FROM tasks WHERE task_id=?",
                                           (task_id,)).fetchone())
        updated_task["git_state"] = git_state or _load_git_state(c, task_id)
        updated_task["active_claims"] = _active_task_claims_in(c, task_id, now)

    if notify:
        msg = (f"Operator revoked claim {claim_id} on {updated_task['task_id']}. "
               f"Stop work, preserve any local evidence, and ack this message. "
               f"Reason: {reason}.")
        if reassignee:
            msg += f" Redirected to {reassignee}."
        notification = send_agent_message(
            actor,
            row["agent_id"],
            msg,
            task_id=updated_task["task_id"],
            requires_ack=True,
            ack_deadline_minutes=ack_deadline_minutes,
            signal="claim_revoked",
            priority=20,
            project=project,
        )
    return {
        "revoked": True,
        "claim_id": claim_id,
        "task_id": updated_task["task_id"],
        "revoked_agent": row["agent_id"],
        "reassigned_to": reassignee or None,
        "task": updated_task,
        "notification": notification,
    }


def mark_task_pr_opened(task_id: str, pr_number: int, pr_url: str = "",
                        branch: str = "", head_sha: str = "",
                        actor: str = "github-webhook",
                        project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return {"error": "task not found", "task_id": task_id}
        current = _load_git_state(c, task_id)
        same_pr = (
            current.get("pr_number") == pr_number and
            (not pr_url or current.get("pr_url") == pr_url) and
            (not branch or current.get("branch") == branch) and
            (not head_sha or current.get("head_sha") == head_sha)
        )
        if row["status"] in ("In Review", "Done") and same_pr:
            task = _task_row(row)
            return {"task_id": task_id, "status": task["status"],
                    "git_state": current, "idempotent": True}
        if row["status"] == "Done":
            return {"task_id": task_id, "status": "Done", "git_state": current,
                    "skipped": True, "reason": "task_already_done"}
        c.execute("UPDATE tasks SET status='In Review', updated_at=? WHERE task_id=? "
                  "AND status NOT IN ('Done', 'Cancelled', 'Canceled')",
                  (now, task_id))
        git_state = _upsert_git_state(c, task_id, {
            "branch": branch or None,
            "head_sha": head_sha or None,
            "pushed_at": now if head_sha else None,
            "pr_number": pr_number,
            "pr_url": pr_url or None,
            "evidence": {"pr_number": pr_number, "pr_url": pr_url,
                         "branch": branch, "head_sha": head_sha},
        })
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "git.pr_opened",
                   json.dumps({"pr_number": pr_number, "pr_url": pr_url,
                               "branch": branch, "head_sha": head_sha}, sort_keys=True), now))
    return {"task_id": task_id, "status": "In Review", "git_state": git_state}


def mark_task_merged(task_id: str, merged_sha: str, pr_number: Optional[int] = None,
                     pr_url: str = "", branch: str = "", head_sha: str = "",
                     actor: str = "github-webhook",
                     project: str = DEFAULT_PROJECT,
                     provenance_source: str = "",
                     task_ids_found: Any = None) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return {"error": "task not found", "task_id": task_id}
        current = _load_git_state(c, task_id)
        same_merge = (
            row["status"] == "Done" and
            current.get("merged_sha") == merged_sha and
            (pr_number is None or current.get("pr_number") == pr_number) and
            (not pr_url or current.get("pr_url") == pr_url) and
            (not branch or current.get("branch") == branch) and
            (not head_sha or current.get("head_sha") == head_sha)
        )
        if same_merge:
            return {"task_id": task_id, "status": "Done",
                    "git_state": current, "idempotent": True}
        c.execute("UPDATE tasks SET status='Done', updated_at=? WHERE task_id=?",
                  (now, task_id))
        git_state = _upsert_git_state(c, task_id, {
            "branch": branch or None,
            "head_sha": head_sha or None,
            "pushed_at": now if head_sha else None,
            "pr_number": pr_number,
            "pr_url": pr_url or None,
            "merged_sha": merged_sha,
            "merged_at": now,
            "in_main_content": True,
            "evidence": {
                "merged_sha": merged_sha,
                "pr_number": pr_number,
                "pr_url": pr_url,
                "branch": branch,
                "head_sha": head_sha,
                **({"source": provenance_source} if provenance_source else {}),
                **({"task_ids_found": task_ids_found} if task_ids_found else {}),
            },
        })
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "git.pr_merged",
                   json.dumps({"merged_sha": merged_sha, "pr_number": pr_number,
                               "pr_url": pr_url}, sort_keys=True), now))
    return {"task_id": task_id, "status": "Done", "git_state": git_state}


def mark_task_default_branch_commit(task_id: str, commit_sha: str,
                                    branch: str = "master", subject: str = "",
                                    actor: str = "default-branch-backfill",
                                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Bootstrap-only provenance repair for direct default-branch commits.

    Normal flow remains complete_claim -> In Review -> PR merge webhook -> Done. This is a
    system/reconcile escape hatch for pre-flow dogfood commits that are already on the default
    branch and mention a task id in their commit subject.
    """
    if not commit_sha:
        return {"error": "commit_sha required", "task_id": task_id}
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return {"error": "task not found", "task_id": task_id}
        if row["status"] == "Done":
            return {"skipped": True, "reason": "already_done", "task_id": task_id}
        if row["status"] != "In Review":
            return {"skipped": True, "reason": "status_not_in_review",
                    "task_id": task_id, "status": row["status"]}
        c.execute("UPDATE tasks SET status='Done', updated_at=? WHERE task_id=?",
                  (now, task_id))
        evidence = {"source": "default_branch_backfill", "commit_sha": commit_sha,
                    "branch": branch, "subject": subject}
        git_state = _upsert_git_state(c, task_id, {
            "branch": branch or None,
            "head_sha": commit_sha,
            "pushed_at": now,
            "merged_sha": commit_sha,
            "merged_at": now,
            "in_main_content": True,
            "evidence": evidence,
        })
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "git.default_branch_backfilled",
                   json.dumps(evidence, sort_keys=True), now))
    return {"task_id": task_id, "status": "Done", "git_state": git_state}


def mark_task_offline_done(task_id: str, evidence: Any = None,
                           artifact_url: str = "", evidence_hash: str = "",
                           verifier: str = "", reviewed_at: Optional[float] = None,
                           actor: str = "switchboard/operator",
                           project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Verify a non-PR/offline task as Done with explicit operator evidence.

    Agents still complete claims to In Review. This path is intentionally separate: a
    verifier/system actor reviews evidence and stamps a non-code provenance record so
    Done means "verified outcome" instead of "agent asked nicely."
    """
    now = time.time()
    evidence_obj = _parse_evidence(evidence)
    artifact_url = (artifact_url or evidence_obj.get("artifact_url") or "").strip()
    evidence_hash = (evidence_hash or evidence_obj.get("evidence_hash") or "").strip()
    verifier = (verifier or evidence_obj.get("verifier") or actor or "").strip()
    if not evidence_obj and not artifact_url and not evidence_hash:
        return {"error": "offline evidence required", "task_id": task_id}
    if evidence_hash and not _valid_evidence_hash(evidence_hash):
        return {
            "error": "invalid_evidence_hash",
            "task_id": task_id,
            "message": "evidence_hash must be a 64-character SHA-256 hex digest, optionally prefixed with sha256:",
        }
    if not evidence_hash and evidence_obj:
        evidence_hash = hashlib.sha256(
            json.dumps(evidence_obj, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    try:
        reviewed = float(reviewed_at) if reviewed_at not in (None, "") else now
    except (TypeError, ValueError):
        return {"error": "reviewed_at must be a unix timestamp", "task_id": task_id}
    offline_payload = {
        "provenance_type": "offline_evidence",
        "evidence": evidence_obj,
        "artifact_url": artifact_url or None,
        "evidence_hash": evidence_hash or None,
        "verifier": verifier,
        "reviewed_at": reviewed,
        "source": "offline_verifier",
    }
    with _conn(project) as c:
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return {"error": "task not found", "task_id": task_id}
        current = _load_git_state(c, task_id)
        if row["status"] == "Done":
            existing_offline = _offline_evidence_from_state(current)
            if existing_offline:
                if existing_offline == offline_payload:
                    return {"task_id": task_id, "status": "Done", "git_state": current,
                            "provenance": _provenance_summary(current), "idempotent": True}
                corrected_payload = {
                    **offline_payload,
                    "corrects": existing_offline,
                    "corrected_at": now,
                }
                git_state = _upsert_git_state(c, task_id, {
                    "evidence": {"offline_evidence": corrected_payload},
                })
                c.execute(
                    "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                    (task_id, actor, "task.offline_evidence_corrected",
                     json.dumps({"previous": existing_offline, "current": corrected_payload},
                                sort_keys=True), now),
                )
                return {"task_id": task_id, "status": "Done", "git_state": git_state,
                        "provenance": _provenance_summary(git_state), "corrected": True}
            if current.get("merged_sha"):
                return {"skipped": True, "reason": "already_done_with_git_provenance",
                        "task_id": task_id, "git_state": current}
        if row["status"] != "In Review":
            return {"error": "offline_done_requires_in_review", "task_id": task_id,
                    "status": row["status"],
                    "message": "Offline Done verification requires the task to be In Review first."}
        c.execute("UPDATE tasks SET status='Done', updated_at=? WHERE task_id=?", (now, task_id))
        git_state = _upsert_git_state(c, task_id, {
            "evidence": {"offline_evidence": offline_payload},
        })
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "task.offline_verified",
                   json.dumps(offline_payload, sort_keys=True), now))
    return {"task_id": task_id, "status": "Done", "git_state": git_state,
            "provenance": _provenance_summary(git_state)}


def backfill_default_branch_commits(commits: List[Dict[str, Any]],
                                    branch: str = "master",
                                    actor: str = "github-webhook",
                                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Stamp In Review tasks referenced by commits that already reached the default branch."""
    direct_backfilled: List[str] = []
    direct_backfill_skipped: List[Dict[str, str]] = []
    seen = set()
    for commit in commits or []:
        message = commit.get("message") or commit.get("subject") or ""
        sha = commit.get("id") or commit.get("sha") or commit.get("commit_sha") or ""
        if not sha:
            continue
        for task_id in dict.fromkeys(TASK_ID_RE.findall(message)):
            key = (task_id, sha)
            if key in seen:
                continue
            seen.add(key)
            res = mark_task_default_branch_commit(
                task_id, sha, branch=branch, subject=message,
                actor=actor, project=project)
            if res.get("status") == "Done":
                direct_backfilled.append(task_id)
            elif res.get("skipped") or res.get("reason") or res.get("error"):
                direct_backfill_skipped.append({
                    "task_id": task_id,
                    "reason": res.get("reason") or res.get("error") or "skipped",
                })
    return {"direct_backfilled_tasks": list(dict.fromkeys(direct_backfilled)),
            "direct_backfill_skipped": direct_backfill_skipped}


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
    spend = {"cost_usd": 0.0, "total_tokens": 0, "by_source": {}}
    seen = set()
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        source = row["source"]
        bucket = spend["by_source"].setdefault(source, {"cost_usd": 0.0, "total_tokens": 0,
                                                        "confidence": row["confidence"]})
        bucket["cost_usd"] += float(row["cost_usd"] or 0.0)
        bucket["total_tokens"] += int(row["total_tokens"] or 0)
        spend["cost_usd"] += float(row["cost_usd"] or 0.0)
        spend["total_tokens"] += int(row["total_tokens"] or 0)
    spend["cost_usd"] = round(spend["cost_usd"], 6)
    for bucket in spend["by_source"].values():
        bucket["cost_usd"] = round(bucket["cost_usd"], 6)
    return spend


def task_tally(task_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    with _conn(project) as c:
        outcome_rows = c.execute("SELECT * FROM outcomes WHERE task_id=? ORDER BY created_at",
                                 (task_id,)).fetchall()
        outcomes = [_outcome_row(r) for r in outcome_rows]
        rows = _spend_for_task(c, task_id, outcomes)
        links: List[Dict[str, Any]] = []
        if outcomes:
            outcome_ids = [o["id"] for o in outcomes]
            link_rows = c.execute(
                "SELECT l.*, k.name, k.unit, k.direction FROM outcome_kpi_links l "
                "JOIN kpis k ON k.id=l.kpi_id WHERE l.outcome_id IN (%s)"
                % ",".join("?" for _ in outcome_ids), outcome_ids).fetchall()
            links = [dict(r) for r in link_rows]
    spend = _spend_summary(rows)
    outcome_counts = {"verified": 0, "proposed": 0, "rejected": 0, "superseded": 0}
    by_outcome = {o["id"]: o for o in outcomes}
    for outcome in outcomes:
        outcome_counts[outcome["status"]] = outcome_counts.get(outcome["status"], 0) + 1
    verified_count = outcome_counts.get("verified", 0)
    cost_per_outcome = (round(spend["cost_usd"] / verified_count, 6)
                        if verified_count else None)
    kpi_groups: Dict[str, Dict[str, Any]] = {}
    for link in links:
        outcome = by_outcome.get(link["outcome_id"]) or {}
        group = kpi_groups.setdefault(link["kpi_id"], {
            "kpi_id": link["kpi_id"],
            "name": link["name"],
            "unit": link["unit"],
            "direction": link["direction"],
            "verified_contribution": 0.0,
            "links": [],
            "cost_per_contribution_unit": None,
        })
        link_payload = {k: link.get(k) for k in ("id", "outcome_id", "contribution",
                                                 "contribution_unit", "confidence", "rationale")}
        link_payload["outcome_status"] = outcome.get("status")
        group["links"].append(link_payload)
        if outcome.get("status") == "verified" and link.get("contribution") is not None:
            group["verified_contribution"] += float(link["contribution"] or 0.0)
    for group in kpi_groups.values():
        if group["verified_contribution"]:
            group["cost_per_contribution_unit"] = round(
                spend["cost_usd"] / group["verified_contribution"], 6)
    return {"task_id": task_id, "spend": spend,
            "unit_cost": {"cost_per_verified_outcome": cost_per_outcome},
            "outcomes": outcome_counts,
            "outcome_records": outcomes,
            "kpis": list(kpi_groups.values())}


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


def project_tally(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Project-level economic surface for TALLY-3.

    This intentionally derives from task_tally/kpi_tally so the board UI and API present the
    same semantics as the lower-level OXP/Tally primitives: verified outcomes are the denominator,
    proposed outcomes stay visible but do not count, and spend remains separated by source.
    """
    tasks = list_tasks(project=project)
    totals = {
        "task_count": len(tasks),
        "tasks_with_spend": 0,
        "tasks_with_verified_outcomes": 0,
        "verified_outcomes": 0,
        "proposed_outcomes": 0,
        "rejected_outcomes": 0,
        "superseded_outcomes": 0,
        "verified_kpi_contribution": 0.0,
        "spend": {"cost_usd": 0.0, "total_tokens": 0, "by_source": {}},
        "unit_cost": {
            "cost_per_verified_outcome": None,
            "cost_per_kpi_contribution_unit": None,
        },
    }
    by_workstream: Dict[str, Dict[str, Any]] = {}
    by_task: List[Dict[str, Any]] = []

    for task in tasks:
        tid = task["task_id"]
        tally = task_tally(tid, project=project)
        spend = tally.get("spend") or {}
        outcomes = tally.get("outcomes") or {}
        verified = int(outcomes.get("verified") or 0)
        proposed = int(outcomes.get("proposed") or 0)
        rejected = int(outcomes.get("rejected") or 0)
        superseded = int(outcomes.get("superseded") or 0)
        cost = float(spend.get("cost_usd") or 0.0)
        tokens = int(spend.get("total_tokens") or 0)
        kpi_groups = tally.get("kpis") or []
        kpi_contribution = round(sum(float(k.get("verified_contribution") or 0.0)
                                     for k in kpi_groups), 6)
        _merge_spend_totals(totals["spend"], spend)
        totals["verified_outcomes"] += verified
        totals["proposed_outcomes"] += proposed
        totals["rejected_outcomes"] += rejected
        totals["superseded_outcomes"] += superseded
        totals["verified_kpi_contribution"] = round(
            totals["verified_kpi_contribution"] + kpi_contribution, 6)
        if cost:
            totals["tasks_with_spend"] += 1
        if verified:
            totals["tasks_with_verified_outcomes"] += 1

        ws_id = task.get("_wsId") or task.get("workstream_id") or "UNKNOWN"
        ws = by_workstream.setdefault(ws_id, {
            "workstream_id": ws_id,
            "name": task.get("_wsName") or task.get("workstream_name") or ws_id,
            "task_count": 0,
            "tasks_with_spend": 0,
            "verified_outcomes": 0,
            "proposed_outcomes": 0,
            "verified_kpi_contribution": 0.0,
            "spend": {"cost_usd": 0.0, "total_tokens": 0, "by_source": {}},
            "unit_cost": {"cost_per_verified_outcome": None},
        })
        ws["task_count"] += 1
        if cost:
            ws["tasks_with_spend"] += 1
        ws["verified_outcomes"] += verified
        ws["proposed_outcomes"] += proposed
        ws["verified_kpi_contribution"] = round(ws["verified_kpi_contribution"] + kpi_contribution, 6)
        _merge_spend_totals(ws["spend"], spend)

        if cost or tokens or verified or proposed or rejected or superseded or kpi_groups:
            by_task.append({
                "task_id": tid,
                "title": task.get("title"),
                "workstream_id": ws_id,
                "workstream_name": task.get("_wsName") or task.get("workstream_name"),
                "status": task.get("status"),
                "spend": spend,
                "outcomes": outcomes,
                "unit_cost": tally.get("unit_cost") or {},
                "verified_kpi_contribution": kpi_contribution,
                "kpis": kpi_groups,
            })

    if totals["verified_outcomes"]:
        totals["unit_cost"]["cost_per_verified_outcome"] = round(
            totals["spend"]["cost_usd"] / totals["verified_outcomes"], 6)
    if totals["verified_kpi_contribution"]:
        totals["unit_cost"]["cost_per_kpi_contribution_unit"] = round(
            totals["spend"]["cost_usd"] / totals["verified_kpi_contribution"], 6)
    for ws in by_workstream.values():
        if ws["verified_outcomes"]:
            ws["unit_cost"]["cost_per_verified_outcome"] = round(
                ws["spend"]["cost_usd"] / ws["verified_outcomes"], 6)

    with _conn(project) as c:
        kpi_ids = [r["id"] for r in c.execute("SELECT id FROM kpis ORDER BY name").fetchall()]
    kpis = []
    for kpi_id in kpi_ids:
        kt = kpi_tally(kpi_id, project=project)
        kpis.append({
            "kpi": kt.get("kpi"),
            "spend": kt.get("spend"),
            "outcomes": kt.get("outcomes"),
            "verified_contribution": kt.get("verified_contribution"),
            "unit_cost": kt.get("unit_cost"),
        })

    return {
        "project": project,
        "totals": totals,
        "by_workstream": sorted(by_workstream.values(),
                                key=lambda x: (-float(x["spend"]["cost_usd"] or 0.0),
                                               x["workstream_id"])),
        "by_task": sorted(by_task, key=lambda x: (-float(x["spend"]["cost_usd"] or 0.0),
                                                  x["task_id"])),
        "kpis": kpis,
    }


_AUDIT_REDACT_KEYS = {
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


def _evidence_claim_reports(c: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = _audit_table_rows(c, "activity", order_by="created_at, id")
    for row in rows:
        row["payload"] = _json_payload(row.get("payload") or "")
    return evidence_claims.evaluate_activities(rows, os.path.dirname(__file__))


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
    return _audit_redact({
        "project_access": dict(project_access) if project_access else None,
        "project_role_grants": [dict(r) for r in role_grants],
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
            activity, os.path.dirname(__file__))
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


def replay_verify(project: str = DEFAULT_PROJECT, from_cursor: int = 0,
                  until_cursor: Optional[int] = None,
                  task_id: str = "") -> Dict[str, Any]:
    """Replay activity events and compare derived task/git state to the live board."""
    import event_replay
    return event_replay.verify_board(
        project,
        from_cursor=from_cursor,
        until_cursor=until_cursor,
        task_id=task_id,
    )


def simulate_dispatch(project: str = DEFAULT_PROJECT, agent_id: str = "",
                      from_cursor: int = 0, until_cursor: Optional[int] = None,
                      lanes: Any = None, capabilities: Any = None,
                      max_risk: str = "", max_budget_usd: Optional[float] = None,
                      deliverable_id: str = "") -> Dict[str, Any]:
    """Dry-run claim_next dispatch against a replayed historical snapshot (no writes)."""
    import event_replay
    if not (agent_id or "").strip():
        return {"error": "agent_id required", "project": project}
    return event_replay.simulate_dispatch(
        project,
        agent_id.strip(),
        from_cursor=from_cursor,
        until_cursor=until_cursor,
        lanes=lanes,
        capabilities=capabilities,
        max_risk=max_risk,
        max_budget_usd=max_budget_usd,
        deliverable_id=deliverable_id,
    )


def project_task_receipts(project: str = DEFAULT_PROJECT, task_id: str = "",
                          from_cursor: int = 0,
                          until_cursor: Optional[int] = None,
                          claim_id: str = "") -> List[Dict[str, Any]]:
    """Project all coordination receipts for one task from activity history."""
    import coordination_receipts
    if not (task_id or "").strip():
        return []
    return coordination_receipts.project_task_receipts(
        project,
        task_id.strip(),
        from_cursor=from_cursor,
        until_cursor=until_cursor,
        claim_id=claim_id,
    )


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


def request_unblock(requesting_agent: str, blocking_task_id: str,
                    blocked_task_id: str, message: str,
                    owner_agent: str, ack_deadline_minutes: int = 60,
                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Send a blocking dep request: agent on blocked_task_id asks owner_agent (working
    on blocking_task_id) to unblock. Returns message record with id to poll via
    get_message_status. Records the request as a 'dep_request' activity on both tasks."""
    payload = (f"[DEP REQUEST] Agent {requesting_agent} is blocked on {blocking_task_id} "
               f"while working on {blocked_task_id}. {message}")
    msg = send_agent_message(requesting_agent, owner_agent, payload,
                             task_id=blocked_task_id,
                             requires_ack=True,
                             ack_deadline_minutes=ack_deadline_minutes,
                             project=project)
    # Activity trail on both tasks
    for tid in (blocked_task_id, blocking_task_id):
        add_comment(tid, requesting_agent,
                    f"Unblock request sent to {owner_agent} re {blocking_task_id}: {message[:120]}",
                    kind="dep_request", project=project)
    return {"request_id": msg["id"], "from": requesting_agent, "to": owner_agent,
            "blocking_task_id": blocking_task_id, "blocked_task_id": blocked_task_id,
            "poll_with": "get_message_status"}


def list_unblock_requests(owner_agent: str,
                          project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """Return unacked blocking dep requests directed to this agent."""
    msgs = list_unacked_messages(owner_agent, project=project)
    return [m for m in msgs if "[DEP REQUEST]" in (m.get("message") or "")]


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


def send_agent_message(from_agent: str, to_agent: str, message: str,
                       task_id: Optional[str] = None, requires_ack: bool = False,
                       ack_deadline_minutes: Optional[int] = None,
                       ack_timeout_seconds: Optional[float] = None,
                       signal: Optional[str] = None, priority: int = 0,
                       on_ack_timeout: str = "notify_sender",
                       principal_id: str = "", idem_key: str = "",
                       project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Send a directed message from one agent to another. Returns the message record."""
    now = time.time()
    if ack_deadline_minutes is None and ack_timeout_seconds is not None:
        ack_deadline_minutes = float(ack_timeout_seconds) / 60.0
    deadline = (now + ack_deadline_minutes * 60) if ack_deadline_minutes else None
    payload = {"from_agent": from_agent, "to_agent": to_agent, "message": message,
               "task_id": task_id, "requires_ack": requires_ack,
               "ack_deadline_minutes": ack_deadline_minutes,
               "ack_timeout_seconds": ack_timeout_seconds,
               "signal": signal, "priority": priority,
               "on_ack_timeout": on_ack_timeout}
    with _conn(project) as c:
        hit = _idem_hit(c, "send", idem_key, from_agent, payload)
        if hit is not None:
            return hit
        delivery = _agent_delivery_state(c, to_agent, now)
        identity_state = (_task_identity_state_in(c, task_id, now)
                          if task_id else {"status": "clear", "takeover_safe": True})
        if (not delivery.get("reachable") and
                identity_state.get("status") == "unbound_live_runtime_possible"):
            delivery = dict(delivery)
            delivery.update({
                "status": "identity_unbound",
                "reason": "not_registered_but_recent_unbound_activity",
                "identity": identity_state,
                "takeover_safe": False,
                "message": (
                    "Target agent_id is not registered, but this task has recent "
                    "unbound activity. The runtime may be live outside Switchboard "
                    "identity binding; require re-registration or human override "
                    "before takeover."
                ),
            })
        cur = c.execute(
            "INSERT INTO agent_messages(from_agent, to_agent, task_id, message, requires_ack, "
            "ack_deadline, sent_at, signal, priority, idem_key, principal_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (from_agent, to_agent, task_id, message, 1 if requires_ack else 0, deadline, now,
             signal or None, int(priority or 0), idem_key or None, principal_id or None),
        )
        msg_id = cur.lastrowid
        task_exists = bool(
            task_id and c.execute("SELECT 1 FROM tasks WHERE task_id=?",
                                  (task_id,)).fetchone()
        )
        response = {"id": msg_id, "from_agent": from_agent, "to_agent": to_agent,
                    "task_id": task_id, "message": message, "requires_ack": requires_ack,
                    "ack_deadline": deadline, "sent_at": now, "acked_at": None,
                    "signal": signal, "priority": int(priority or 0),
                    "mailbox_stored": True,
                    "delivery": delivery,
                    "delivery_status": delivery["status"]}
        if identity_state.get("status") != "clear":
            response["identity"] = identity_state
        if not delivery.get("reachable"):
            failure_class = (
                "unbound_identity"
                if delivery.get("status") == "identity_unbound"
                else "unreachable_agent"
            )
            response["warning"] = delivery.get("message")
            response["fallback"] = {
                "task_comment": task_exists,
                "reason": delivery.get("reason"),
                "takeover_safe": delivery.get("takeover_safe", True),
                "failure_class": failure_class,
                "expected_signal": FAIL_FIX_FAILURE_CLASSES[failure_class]["expected_signal"],
            }
        if requires_ack:
            monitor = _create_ack_monitor(c, msg_id, from_agent, to_agent, task_id,
                                          deadline, now, on_ack_timeout=on_ack_timeout)
            response["monitor_id"] = monitor["id"]
            response["monitor"] = monitor
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, from_agent, "message.sent", json.dumps(response, sort_keys=True), now))
        if not delivery.get("reachable"):
            c.execute(
                "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                (task_id, "switchboard/delivery", "message.delivery_unreachable",
                 json.dumps({
                     "message_id": msg_id,
                     "from_agent": from_agent,
                     "to_agent": to_agent,
                     "delivery": delivery,
                     "failure_class": failure_class,
                     "expected_signal": FAIL_FIX_FAILURE_CLASSES[failure_class]["expected_signal"],
                 }, sort_keys=True), now),
            )
            if task_exists:
                fallback_text = (
                    f"Directed message #{msg_id} to `{to_agent}` was queued in the "
                    f"durable inbox, but the target is not currently reachable "
                    f"({delivery.get('reason')}). Treat this task comment as the "
                    "visible fallback until that runtime registers, heartbeats, and "
                    "drains its Switchboard inbox."
                )
                if delivery.get("takeover_safe") is False:
                    fallback_text += (
                        " Recent unbound activity exists on this task, so do not "
                        "treat absence from active_agents as proof that takeover is safe."
                    )
                c.execute(
                    "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (task_id, "switchboard/delivery", "comment",
                     json.dumps({
                         "text": fallback_text,
                         "failure_class": failure_class,
                         "expected_signal": FAIL_FIX_FAILURE_CLASSES[failure_class]["expected_signal"],
                     }, sort_keys=True), now),
                )
        _idem_store(c, "send", idem_key, from_agent, payload, response)
        return response


def _monitor_row(r: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if not r:
        return None
    d = dict(r)
    for k in ("condition_json", "on_timeout_json", "result_json"):
        raw = d.pop(k, "{}")
        d[k[:-5] if k.endswith("_json") else k] = json.loads(raw or "{}")
    return d


def _create_ack_monitor(c: sqlite3.Connection, message_id: int, from_agent: str,
                        to_agent: str, task_id: Optional[str], deadline: Optional[float],
                        now: float, on_ack_timeout: str = "notify_sender") -> Dict[str, Any]:
    monitor_id = f"mon-{uuid.uuid4().hex[:16]}"
    condition = {"type": "message_ack", "message_id": message_id}
    action = (on_ack_timeout or "notify_sender").strip()
    if action not in ("notify_sender", "wake_target", "wake_or_operator_alert"):
        action = "notify_sender"
    on_timeout = {"action": action, "signal": "ack_timeout"}
    c.execute(
        "INSERT INTO coordination_monitors"
        "(id, kind, target_type, target_id, task_id, owner_agent, subject_agent, status, "
        "deadline, condition_json, on_timeout_json, result_json, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (monitor_id, "ack_deadline", "agent_message", str(message_id), task_id,
         from_agent, to_agent, "pending", deadline,
         json.dumps(condition, sort_keys=True), json.dumps(on_timeout, sort_keys=True),
         "{}", now, now),
    )
    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
              (task_id, "switchboard/monitor", "monitor.created",
               json.dumps({"monitor_id": monitor_id, "kind": "ack_deadline",
                           "message_id": message_id, "deadline": deadline,
                           "owner_agent": from_agent, "subject_agent": to_agent},
                          sort_keys=True), now))
    return _monitor_row(c.execute("SELECT * FROM coordination_monitors WHERE id=?",
                                  (monitor_id,)).fetchone()) or {}


def _load_monitor_for_message(c: sqlite3.Connection, message_id: int) -> Optional[Dict[str, Any]]:
    return _monitor_row(c.execute(
        "SELECT * FROM coordination_monitors WHERE kind='ack_deadline' "
        "AND target_type='agent_message' AND target_id=? ORDER BY created_at DESC LIMIT 1",
        (str(message_id),),
    ).fetchone())


def ack_message(message_id: int, response: str = "",
                actor: str = "system",
                project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Mark a message as acknowledged by the receiving agent. Returns updated record."""
    now = time.time()
    with _conn(project) as c:
        cur = c.execute(
            "UPDATE agent_messages SET acked_at=?, ack_response=? WHERE id=? AND acked_at IS NULL",
            (now, response or None, message_id),
        )
        if cur.rowcount == 0:
            r = c.execute("SELECT * FROM agent_messages WHERE id=?", (message_id,)).fetchone()
            if r:
                msg = dict(r) | {"note": "already acked"}
                msg["monitor"] = _load_monitor_for_message(c, message_id)
                return msg
            return {"error": "message not found", "id": message_id}
        r = c.execute("SELECT * FROM agent_messages WHERE id=?", (message_id,)).fetchone()
        mon = _load_monitor_for_message(c, message_id)
        if mon and mon.get("status") in ("pending", "fired"):
            c.execute(
                "UPDATE coordination_monitors SET status='resolved', resolved_at=?, "
                "updated_at=?, last_checked_at=?, result_json=? WHERE id=?",
                (now, now, now,
                 json.dumps({"acked_at": now, "ack_response": response}, sort_keys=True),
                 mon["id"]),
            )
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (r["task_id"], "switchboard/monitor", "monitor.resolved",
                       json.dumps({"monitor_id": mon["id"], "message_id": message_id,
                                   "reason": "acked"}, sort_keys=True), now))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (r["task_id"], actor, "message.acked",
                   json.dumps({"message_id": message_id, "response": response}, sort_keys=True), now))
        out = dict(r)
        out["monitor"] = _load_monitor_for_message(c, message_id)
    return out


def list_unacked_messages(to_agent: str, project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """Messages directed to this agent that have not been acknowledged yet."""
    with _conn(project) as c:
        rows = c.execute(
            "SELECT * FROM agent_messages WHERE to_agent=? AND requires_ack=1 "
            "AND acked_at IS NULL "
            "ORDER BY priority DESC, id",
            (to_agent,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_message_status(message_id: int, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    """Sender polls this to see whether a message has been acked."""
    now = time.time()
    with _conn(project) as c:
        r = c.execute("SELECT * FROM agent_messages WHERE id=?", (message_id,)).fetchone()
        if not r:
            return None
        out = dict(r)
        out["monitor"] = _load_monitor_for_message(c, message_id)
        out["mailbox_stored"] = True
        out["delivery"] = _agent_delivery_state(c, out.get("to_agent") or "", now)
        out["delivery_status"] = out["delivery"]["status"]
        return out


def list_pending_acks(agent_id: str = "", project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """Unacked required messages plus their durable monitor state."""
    q = ("SELECT * FROM agent_messages WHERE requires_ack=1 AND acked_at IS NULL")
    params: List[Any] = []
    if agent_id:
        q += " AND (from_agent=? OR to_agent=?)"
        params.extend([agent_id, agent_id])
    q += " ORDER BY COALESCE(ack_deadline, 9999999999999), priority DESC, id"
    with _conn(project) as c:
        rows = c.execute(q, params).fetchall()
        out = []
        for r in rows:
            msg = dict(r)
            msg["monitor"] = _load_monitor_for_message(c, int(r["id"]))
            out.append(msg)
        return out


def list_coordination_monitors(status: str = "", kind: str = "", task_id: str = "",
                               project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    q = "SELECT * FROM coordination_monitors WHERE 1=1"
    params: List[Any] = []
    if status:
        q += " AND status=?"; params.append(status)
    if kind:
        q += " AND kind=?"; params.append(kind)
    if task_id:
        q += " AND task_id=?"; params.append(task_id)
    q += " ORDER BY COALESCE(deadline, 9999999999999), created_at"
    with _conn(project) as c:
        return [_monitor_row(r) or {} for r in c.execute(q, params).fetchall()]


def resolve_monitor(monitor_id: str, reason: str = "manual",
                    actor: str = "system",
                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        r = c.execute("SELECT * FROM coordination_monitors WHERE id=?", (monitor_id,)).fetchone()
        if not r:
            return {"error": "monitor not found", "monitor_id": monitor_id}
        mon = _monitor_row(r) or {}
        if mon.get("status") == "resolved":
            return mon | {"note": "already resolved"}
        result = dict(mon.get("result") or {})
        result.update({"resolved_by": actor, "reason": reason})
        c.execute(
            "UPDATE coordination_monitors SET status='resolved', resolved_at=?, "
            "updated_at=?, last_checked_at=?, result_json=? WHERE id=?",
            (now, now, now, json.dumps(result, sort_keys=True), monitor_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (mon.get("task_id"), actor, "monitor.resolved",
                   json.dumps({"monitor_id": monitor_id, "reason": reason}, sort_keys=True), now))
        return _monitor_row(c.execute("SELECT * FROM coordination_monitors WHERE id=?",
                                      (monitor_id,)).fetchone()) or {}


def cancel_monitor(monitor_id: str, reason: str = "cancelled",
                   actor: str = "system",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        r = c.execute("SELECT * FROM coordination_monitors WHERE id=?", (monitor_id,)).fetchone()
        if not r:
            return {"error": "monitor not found", "monitor_id": monitor_id}
        mon = _monitor_row(r) or {}
        if mon.get("status") == "cancelled":
            return mon | {"note": "already cancelled"}
        result = dict(mon.get("result") or {})
        result.update({"cancelled_by": actor, "reason": reason})
        c.execute(
            "UPDATE coordination_monitors SET status='cancelled', resolved_at=?, "
            "updated_at=?, last_checked_at=?, result_json=? WHERE id=?",
            (now, now, now, json.dumps(result, sort_keys=True), monitor_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (mon.get("task_id"), actor, "monitor.cancelled",
                   json.dumps({"monitor_id": monitor_id, "reason": reason}, sort_keys=True), now))
        return _monitor_row(c.execute("SELECT * FROM coordination_monitors WHERE id=?",
                                      (monitor_id,)).fetchone()) or {}


def sweep_coordination_monitors(project: str = DEFAULT_PROJECT,
                                now: Optional[float] = None) -> Dict[str, Any]:
    """Evaluate durable monitors. Designed for a Switchboard-owned timer or explicit tool call."""
    now = time.time() if now is None else float(now)
    checked = resolved = fired = 0
    events: List[Dict[str, Any]] = []
    with _conn(project) as c:
        rows = c.execute(
            "SELECT * FROM coordination_monitors WHERE status='pending' ORDER BY created_at"
        ).fetchall()
        for row in rows:
            checked += 1
            mon = _monitor_row(row) or {}
            if mon.get("kind") != "ack_deadline":
                c.execute("UPDATE coordination_monitors SET last_checked_at=?, updated_at=? WHERE id=?",
                          (now, now, mon["id"]))
                continue
            msg = c.execute("SELECT * FROM agent_messages WHERE id=?",
                            (int(mon.get("target_id") or 0),)).fetchone()
            if not msg:
                result = {
                    "reason": "target_missing",
                    "failure_class": "missing_data",
                    "expected_signal": FAIL_FIX_FAILURE_CLASSES["missing_data"]["expected_signal"],
                }
                c.execute(
                    "UPDATE coordination_monitors SET status='cancelled', resolved_at=?, "
                    "last_checked_at=?, updated_at=?, result_json=? WHERE id=?",
                    (now, now, now, json.dumps(result, sort_keys=True), mon["id"]),
                )
                events.append({"monitor_id": mon["id"], "status": "cancelled",
                               "reason": "target_missing",
                               "failure_class": "missing_data"})
                continue
            if msg["acked_at"] is not None:
                result = {"acked_at": msg["acked_at"], "ack_response": msg["ack_response"]}
                c.execute(
                    "UPDATE coordination_monitors SET status='resolved', resolved_at=?, "
                    "last_checked_at=?, updated_at=?, result_json=? WHERE id=?",
                    (now, now, now, json.dumps(result, sort_keys=True), mon["id"]),
                )
                c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                          (mon.get("task_id"), "switchboard/monitor", "monitor.resolved",
                           json.dumps({"monitor_id": mon["id"], "message_id": msg["id"],
                                       "reason": "acked"}, sort_keys=True), now))
                resolved += 1
                events.append({"monitor_id": mon["id"], "status": "resolved",
                               "message_id": msg["id"]})
                continue
            deadline = mon.get("deadline")
            if deadline is not None and deadline <= now:
                action = (mon.get("on_timeout") or {}).get("action") or "notify_sender"
                result = {"reason": "ack_timeout", "deadline": deadline, "fired_at": now,
                          "on_timeout": action,
                          "failure_class": "unreachable_agent",
                          "expected_signal": FAIL_FIX_FAILURE_CLASSES["unreachable_agent"]["expected_signal"]}
                c.execute(
                    "UPDATE coordination_monitors SET status='fired', fired_at=?, "
                    "last_checked_at=?, updated_at=?, result_json=? WHERE id=?",
                    (now, now, now, json.dumps(result, sort_keys=True), mon["id"]),
                )
                payload = {"monitor_id": mon["id"], "message_id": msg["id"],
                           "from_agent": msg["from_agent"], "to_agent": msg["to_agent"],
                           "deadline": deadline,
                           "failure_class": "unreachable_agent",
                           "expected_signal": FAIL_FIX_FAILURE_CLASSES["unreachable_agent"]["expected_signal"]}
                c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                          (msg["task_id"], "switchboard/monitor", "monitor.timeout",
                           json.dumps(payload, sort_keys=True), now))
                notice = (f"Ack timeout for message {msg['id']} to {msg['to_agent']} "
                          f"on task {msg['task_id'] or '(none)'}.")
                cur = c.execute(
                    "INSERT INTO agent_messages(from_agent, to_agent, task_id, message, "
                    "requires_ack, ack_deadline, sent_at, signal, priority, idem_key, principal_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("switchboard/monitor", msg["from_agent"], msg["task_id"], notice,
                     1, None, now, "ack_timeout", 100, None, None),
                )
                notice_payload = {"id": cur.lastrowid, "from_agent": "switchboard/monitor",
                                  "to_agent": msg["from_agent"], "task_id": msg["task_id"],
                                  "message": notice, "requires_ack": True,
                                  "signal": "ack_timeout", "priority": 100,
                                  "sent_at": now,
                                  "failure_class": "unreachable_agent",
                                  "expected_signal": FAIL_FIX_FAILURE_CLASSES["unreachable_agent"]["expected_signal"]}
                c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                          (msg["task_id"], "switchboard/monitor", "message.sent",
                           json.dumps(notice_payload, sort_keys=True), now))
                wake = None
                if action in ("wake_target", "wake_or_operator_alert"):
                    selector = {"agent_id": msg["to_agent"]}
                    runtime = _selector_runtime_for_agent(msg["to_agent"])
                    if runtime:
                        selector["runtime"] = runtime
                    wake = _insert_wake_intent(
                        c, selector=selector, reason="ack_timeout",
                        source=f"monitor:{mon['id']}",
                        policy={"no_eligible_host": "wait",
                                "operator_alert": action == "wake_or_operator_alert"},
                        task_id=msg["task_id"], principal_id="",
                        actor="switchboard/monitor", now=now,
                        idem_key=f"ack-timeout:{mon['id']}")
                    result["wake_id"] = wake["wake_id"]
                    result["wake_status"] = wake["status"]
                    c.execute(
                        "UPDATE coordination_monitors SET result_json=? WHERE id=?",
                        (json.dumps(result, sort_keys=True), mon["id"]),
                    )
                fired += 1
                event = {"monitor_id": mon["id"], "status": "fired",
                         "message_id": msg["id"], "notice_id": cur.lastrowid,
                         "failure_class": "unreachable_agent"}
                if wake:
                    event["wake_id"] = wake["wake_id"]
                    event["wake_status"] = wake["status"]
                events.append(event)
            else:
                c.execute("UPDATE coordination_monitors SET last_checked_at=?, updated_at=? WHERE id=?",
                          (now, now, mon["id"]))
    wake_sweep = sweep_wake_intents(project=project, now=now)
    return {"project": project, "checked": checked, "resolved": resolved,
            "fired": fired, "events": events, "wake_sweep": wake_sweep}


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


def delete_task(task_id: str, project: str = DEFAULT_PROJECT) -> bool:
    with _conn(project) as c:
        cur = c.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        c.execute("DELETE FROM activity WHERE task_id=?", (task_id,))
        return cur.rowcount > 0


TASK_MOVE_TABLES = (
    "activity",
    "task_git_state",
    "task_summaries",
    "task_narrations",
    "pending_narrations",
    "llm_spend",
    "outcomes",
    "task_claims",
    "file_leases",
    "resource_leases",
    "decisions",
)
AUTOINCREMENT_TASK_TABLES = {"activity", "llm_spend", "decisions"}


def _rows_for_task(c: sqlite3.Connection, table: str, task_id: str) -> List[Dict[str, Any]]:
    return [dict(r) for r in c.execute(f"SELECT * FROM {table} WHERE task_id=?",
                                       (task_id,)).fetchall()]


def _task_snapshot_in(c: sqlite3.Connection, task_id: str) -> Optional[Dict[str, Any]]:
    task = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not task:
        return None
    snapshot: Dict[str, Any] = {"task": dict(task)}
    for table in TASK_MOVE_TABLES:
        snapshot[table] = _rows_for_task(c, table, task_id)
    outcome_ids = [r["id"] for r in snapshot.get("outcomes", [])]
    if outcome_ids:
        placeholders = ",".join("?" for _ in outcome_ids)
        snapshot["outcome_kpi_links"] = [
            dict(r) for r in c.execute(
                f"SELECT * FROM outcome_kpi_links WHERE outcome_id IN ({placeholders})",
                outcome_ids,
            ).fetchall()
        ]
    else:
        snapshot["outcome_kpi_links"] = []
    kpi_ids = sorted({r["kpi_id"] for r in snapshot.get("outcome_kpi_links", [])
                      if r.get("kpi_id")})
    if kpi_ids:
        placeholders = ",".join("?" for _ in kpi_ids)
        snapshot["kpis"] = [
            dict(r) for r in c.execute(
                f"SELECT * FROM kpis WHERE id IN ({placeholders})", kpi_ids,
            ).fetchall()
        ]
    else:
        snapshot["kpis"] = []
    snapshot["agent_messages"] = _rows_for_task(c, "agent_messages", task_id)
    snapshot["coordination_monitors"] = _rows_for_task(c, "coordination_monitors", task_id)
    return snapshot


def _active_task_state_in(c: sqlite3.Connection, task_id: str, now: float) -> Dict[str, Any]:
    active_claims = [dict(r) for r in c.execute(
        "SELECT id, agent_id, expires_at FROM task_claims "
        "WHERE task_id=? AND status='active' AND expires_at>?",
        (task_id, now),
    ).fetchall()]
    active_resource_leases = [dict(r) for r in c.execute(
        "SELECT id, agent_id, resource_type, names, claimed_at, ttl_seconds FROM resource_leases "
        "WHERE task_id=? AND released_at IS NULL AND claimed_at + ttl_seconds > ?",
        (task_id, now),
    ).fetchall()]
    active_file_leases = [dict(r) for r in c.execute(
        "SELECT id, agent_id, files, claimed_at, ttl_minutes FROM file_leases "
        "WHERE task_id=? AND released_at IS NULL AND claimed_at + (ttl_minutes * 60) > ?",
        (task_id, now),
    ).fetchall()]
    return {"claims": active_claims, "resource_leases": active_resource_leases,
            "file_leases": active_file_leases}


def _insert_archive_in(c: sqlite3.Connection, task_id: str, operation: str, actor: str,
                       reason: str, source_project: str, destination_project: str,
                       snapshot: Dict[str, Any], now: float) -> str:
    archive_id = "archive-" + uuid.uuid4().hex[:16]
    c.execute(
        "INSERT INTO archived_tasks(archive_id, task_id, operation, actor, reason, "
        "source_project, destination_project, snapshot_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (archive_id, task_id, operation, actor, reason or None, source_project,
         destination_project or None, json.dumps(snapshot, sort_keys=True), now),
    )
    return archive_id


def _delete_task_related_in(c: sqlite3.Connection, task_id: str, snapshot: Dict[str, Any]) -> None:
    outcome_ids = [r["id"] for r in snapshot.get("outcomes", [])]
    if outcome_ids:
        placeholders = ",".join("?" for _ in outcome_ids)
        c.execute(f"DELETE FROM outcome_kpi_links WHERE outcome_id IN ({placeholders})",
                  outcome_ids)
    for table in (
        "activity",
        "task_git_state",
        "task_summaries",
        "task_narrations",
        "pending_narrations",
        "llm_spend",
        "outcomes",
        "task_claims",
        "file_leases",
        "resource_leases",
        "decisions",
        "agent_messages",
        "coordination_monitors",
    ):
        c.execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))
    c.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))


def _apply_task_id(row: Dict[str, Any], old_task_id: str, new_task_id: str) -> Dict[str, Any]:
    out = dict(row)
    if out.get("task_id") == old_task_id:
        out["task_id"] = new_task_id
    return out


def _missing_dependencies(depends_on: List[str], project: str) -> List[str]:
    return [dep for dep in depends_on if not get_task(dep, project=project)]


def get_archived_task(archive_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        row = c.execute("SELECT * FROM archived_tasks WHERE archive_id=?",
                        (archive_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["snapshot"] = json.loads(out.pop("snapshot_json") or "{}")
        return out


def archive_task(task_id: str, reason: str = "", actor: str = "system",
                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    if not has_project(project):
        return {"error": f"unknown project: {project}", "project": project}
    now = time.time()
    with _conn(project) as c:
        snapshot = _task_snapshot_in(c, task_id)
        if not snapshot:
            return {"error": "task not found", "task_id": task_id, "project": project}
        active = _active_task_state_in(c, task_id, now)
        if active["claims"] or active["resource_leases"] or active["file_leases"]:
            return {"error": "task has active claims or leases", "task_id": task_id,
                    "project": project, "active": active}
        archive_id = _insert_archive_in(
            c, task_id, "archive", actor, reason, project, "", snapshot, now)
        _delete_task_related_in(c, task_id, snapshot)
    return {"archived": True, "archive_id": archive_id, "task_id": task_id,
            "project": project, "reason": reason or None}


TERMINAL_TASK_STATUSES = {"Done", "Cancelled", "Canceled"}
TERMINAL_WAKE_STATUSES = {"completed", "failed", "cancelled"}
TERMINAL_RUNNER_STATUSES = {"exited", "killed", "failed", "completed", "expired"}
RUNNER_CONTROL_ACTIONS = {"snapshot", "kill", "restart", "health", "logs", "open"}


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


def _is_cleanup_proof_task(task: Dict[str, Any]) -> bool:
    task_id = (task.get("task_id") or "").upper()
    ws = (task.get("workstream_id") or "").upper()
    title = (task.get("title") or "").lower()
    return (
        task_id.startswith("PROOF-")
        or ws in {"PROOF", "SENTINEL"}
        or "proof" in title
        or "sentinel" in title
    )


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


def move_task(task_id: str, project_from: str, project_to: str, reason: str = "",
              actor: str = "system", new_task_id: str = "",
              dependency_policy: str = "fail") -> Dict[str, Any]:
    if not has_project(project_from):
        return {"error": f"unknown source project: {project_from}", "project": project_from}
    if not has_project(project_to):
        return {"error": f"unknown destination project: {project_to}", "project": project_to}
    if project_from == project_to:
        return {"error": "source and destination projects must differ",
                "project": project_from, "task_id": task_id}
    now = time.time()
    new_task_id = (new_task_id or task_id).strip()
    dependency_policy = (dependency_policy or "fail").strip().lower()
    if dependency_policy not in {"fail", "clear"}:
        return {"error": "dependency_policy must be 'fail' or 'clear'",
                "dependency_policy": dependency_policy}

    with _conn(project_from) as source:
        snapshot = _task_snapshot_in(source, task_id)
        if not snapshot:
            return {"error": "task not found", "task_id": task_id,
                    "project": project_from}
        active = _active_task_state_in(source, task_id, now)
        if active["claims"] or active["resource_leases"] or active["file_leases"]:
            return {"error": "task has active claims or leases", "task_id": task_id,
                    "project": project_from, "active": active}

    task_row = dict(snapshot["task"])
    depends_on = json.loads(task_row.get("depends_on") or "[]")
    missing_deps = _missing_dependencies(depends_on, project_to)
    cleared_deps: List[str] = []
    if missing_deps:
        if dependency_policy == "fail":
            return {"error": "destination is missing dependency id(s)",
                    "task_id": task_id, "project_from": project_from,
                    "project_to": project_to, "missing_dependencies": missing_deps,
                    "hint": "create dependencies first or pass dependency_policy='clear'"}
        cleared_deps = missing_deps
        depends_on = [dep for dep in depends_on if dep not in set(missing_deps)]

    try:
        with _conn(project_to) as dest:
            if dest.execute("SELECT 1 FROM tasks WHERE task_id=?",
                            (new_task_id,)).fetchone():
                return {"error": "destination task id already exists",
                        "task_id": new_task_id, "project_to": project_to}
            outcome_ids = [r["id"] for r in snapshot.get("outcomes", [])]
            if outcome_ids:
                placeholders = ",".join("?" for _ in outcome_ids)
                conflicts = [r["id"] for r in dest.execute(
                    f"SELECT id FROM outcomes WHERE id IN ({placeholders})",
                    outcome_ids,
                ).fetchall()]
                if conflicts:
                    return {"error": "destination outcome id conflict",
                            "project_to": project_to, "outcome_ids": conflicts}
            moved_task = _apply_task_id(task_row, task_id, new_task_id)
            moved_task["depends_on"] = json.dumps(depends_on)
            moved_task["updated_at"] = now
            _insert_row(dest, "tasks", moved_task)
            for table in TASK_MOVE_TABLES:
                skip = {"id"} if table in AUTOINCREMENT_TASK_TABLES else set()
                for row in snapshot.get(table, []):
                    moved_row = _apply_task_id(row, task_id, new_task_id)
                    if table == "outcomes":
                        moved_row["project"] = project_to
                    _insert_row(dest, table, moved_row, skip_columns=skip)
            for row in snapshot.get("kpis", []):
                if dest.execute("SELECT 1 FROM kpis WHERE id=?", (row["id"],)).fetchone():
                    continue
                moved_kpi = dict(row)
                moved_kpi["project"] = project_to
                _insert_row(dest, "kpis", moved_kpi)
            for row in snapshot.get("outcome_kpi_links", []):
                moved_link = dict(row)
                moved_link["project"] = project_to
                _insert_row(dest, "outcome_kpi_links", moved_link)
            dest.execute(
                "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                "VALUES (?,?,?,?,?)",
                (new_task_id, actor, "task.moved_in", json.dumps({
                    "from_project": project_from,
                    "original_task_id": task_id,
                    "task_id": new_task_id,
                    "reason": reason or None,
                    "cleared_dependencies": cleared_deps,
                }, sort_keys=True), now),
            )
    except sqlite3.IntegrityError as e:
        return {"error": "destination insert failed", "detail": str(e),
                "task_id": task_id, "project_to": project_to}

    with _conn(project_from) as source:
        source_snapshot = _task_snapshot_in(source, task_id)
        if not source_snapshot:
            return {"moved": True, "warning": "source task already absent after destination copy",
                    "task_id": task_id, "new_task_id": new_task_id,
                    "project_from": project_from, "project_to": project_to}
        archive_id = _insert_archive_in(
            source, task_id, "move_out", actor, reason, project_from,
            project_to, source_snapshot, now)
        _delete_task_related_in(source, task_id, source_snapshot)

    return {"moved": True, "archive_id": archive_id, "task_id": task_id,
            "new_task_id": new_task_id, "project_from": project_from,
            "project_to": project_to, "cleared_dependencies": cleared_deps}


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


def _task_looks_like_code_work(task: Dict[str, Any]) -> bool:
    text = _session_profile_text(task).lower()
    if re.search(r"(?:policy_profile|session_profile)\s*[:=]", text):
        return False
    if re.search(r"\b(non[- ]code|offline evidence|docs[- ]only|review[- ]only)\b", text):
        return False
    code_terms = (
        "code", "repo", "branch", "worktree", "clone", "git ", "github", "pr ",
        "pull request", "merge", "rebase", "commit", "ci", "tests", "test suite",
        "deploy", "server", "api", "mcp", "rest", "ui", "runtime", "adapter",
    )
    return any(term in text for term in code_terms)


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


def set_project_repo_topology(project: str = DEFAULT_PROJECT, canonical_repo: str = "",
                              public_ci_repo: str = "", public_repo: str = "",
                              release_repo: str = "", topology_type: str = "",
                              canonical_default_branch: str = "",
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
        "canonical": {"repo": canonical_repo, "default_branch": canonical_default_branch},
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


def list_task_deliverable_links(task_id: str, project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """Return deliverable links for one task, including cross-project mission rollups.

    Link rows live in the deliverable home project database, so this scans every routable
    project for rows matching the explicit task_project + task_id pair.
    """
    tid = (task_id or "").strip().upper()
    task_project = (project or DEFAULT_PROJECT).strip()
    if not tid or not has_project(task_project):
        return []
    links: List[Dict[str, Any]] = []
    seen: set = set()
    query = (
        """SELECT l.*, d.title AS deliverable_title, d.status AS deliverable_status
           FROM deliverable_task_links l
           JOIN deliverables d ON d.id = l.deliverable_id
           WHERE l.task_id=? AND l.project_id=?
           ORDER BY l.updated_at DESC, l.id"""
    )
    for deliverable_project in project_ids():
        if not has_project(deliverable_project):
            continue
        with _conn(deliverable_project) as c:
            try:
                rows = c.execute(query, (tid, task_project)).fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                link_id = row["id"]
                dedupe = (deliverable_project, link_id)
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                link = _deliverable_link_row(row)
                link["deliverable_home_project"] = deliverable_project
                link["deliverable_title"] = row["deliverable_title"]
                link["deliverable_status"] = row["deliverable_status"]
                if link.get("board_id"):
                    board_row = c.execute("SELECT * FROM project_boards WHERE id=?",
                                          (link["board_id"],)).fetchone()
                    link["board"] = (_project_board_row(board_row, project=deliverable_project)
                                     if board_row else {"error": "unknown board",
                                                        "board_id": link["board_id"],
                                                        "project_id": deliverable_project})
                links.append(link)
    links.sort(key=lambda item: (-(item.get("updated_at") or 0), item.get("id") or ""))
    return links


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


def _task_hierarchy_breadcrumb(task: Dict[str, Any], project: str,
                               links: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    breadcrumb = [
        {"level": "project", "id": project},
        {"level": "workstream", "id": task.get("_wsId"), "label": task.get("_wsName")},
        {"level": "task", "id": task.get("task_id"), "title": task.get("title")},
    ]
    if links is None:
        links = list_task_deliverable_links(task.get("task_id") or "", project=project)
    if links:
        first = links[0]
        board = first.get("board") or {}
        breadcrumb.insert(1, {
            "level": board.get("kind") or "mission",
            "id": first.get("board_id"),
            "title": (board.get("title") if isinstance(board, dict) else None) or first.get("board_id"),
            "deliverable_id": first.get("deliverable_id"),
            "deliverable_title": first.get("deliverable_title"),
        })
    return breadcrumb


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
    if pid in BUILTIN_PROJECTS:
        return {"error": f"reserved built-in project id: {pid}", "project_id": pid}
    repo, repo_error = _validate_github_repo(github_repo)
    if repo_error:
        return {"error": repo_error, "repo": repo, "project_id": pid}

    existing = _dynamic_projects().get(pid)
    if existing:
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


def update_canonical_main_sha(sha: str, actor: str = "github-webhook",
                              project: str = DEFAULT_PROJECT) -> None:
    if not sha:
        return
    set_meta("canonical_main_sha", sha, project=project)
    append_activity("git.main_advanced", actor, {"canonical_main_sha": sha},
                    task_id=None, project=project)


def _git_ok(args: List[str]) -> bool:
    try:
        return subprocess.run(["git", *args], cwd=os.path.dirname(__file__),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=5).returncode == 0
    except Exception:
        return False


def _git_checks_available() -> bool:
    return _git_ok(["rev-parse", "--is-inside-work-tree"])


def _github_repo_from_git_url(url: str) -> str:
    clean = (url or "").strip()
    if not clean:
        return ""
    match = re.search(r"github\.com[:/]([^/\s:]+)/([^/\s]+)", clean, re.I)
    if not match:
        return ""
    repo = f"{match.group(1)}/{match.group(2)}"
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo.strip()


def _github_repo_from_pr_url(url: str) -> str:
    match = GITHUB_PR_URL_RE.search((url or "").strip())
    return match.group(1) if match else ""


def _normalize_repo_slug(repo: str) -> str:
    clean = _github_repo_from_git_url(repo) or (repo or "").strip()
    if clean.endswith(".git"):
        clean = clean[:-4]
    return clean.lower()


def _local_github_repo() -> str:
    try:
        remote = subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=os.path.dirname(__file__),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
    except Exception:
        return ""
    return _github_repo_from_git_url(remote)


def _github_pr(repo: str, pr_number: int, token: str = "") -> Optional[Dict[str, Any]]:
    if not repo or not pr_number:
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/pulls/{int(pr_number)}")
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _github_token() -> str:
    return (
        os.environ.get("PM_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("SWITCHBOARD_CI_GITHUB_TOKEN")
        or ""
    ).strip()


def _github_merged_prs(repo: str, token: str = "", limit: int = 30) -> List[Dict[str, Any]]:
    """Most recently updated closed PRs on the repo, merged ones only (newest first)."""
    if not repo or limit <= 0:
        return []
    per_page = max(1, min(int(limit), 100))
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls"
        f"?state=closed&sort=updated&direction=desc&per_page={per_page}")
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            page = json.loads(r.read().decode())
    except Exception:
        return []
    if not isinstance(page, list):
        return []
    return [pr for pr in page if pr.get("merged_at")]


def _activity_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        return " ".join(_activity_text(v) for v in payload.values())
    if isinstance(payload, list):
        return " ".join(_activity_text(v) for v in payload)
    if payload is None:
        return ""
    return str(payload)


def _infer_pr_evidence_from_activity(c: sqlite3.Connection, task_id: str,
                                     repo: str) -> Dict[str, Any]:
    repo_l = repo.lower()
    rows = c.execute(
        "SELECT kind, payload FROM activity WHERE task_id=? ORDER BY id DESC LIMIT 80",
        (task_id,),
    ).fetchall()
    for row in rows:
        text = _activity_text(_json_payload(row["payload"]))
        if not text:
            continue
        for match in GITHUB_PR_URL_RE.finditer(text):
            pr_repo = match.group(1)
            branch_match = BRANCH_EVIDENCE_RE.search(text)
            head_match = HEAD_EVIDENCE_RE.search(text)
            return {
                "pr_number": int(match.group(2)),
                "pr_url": match.group(0),
                "repo": pr_repo,
                "branch": branch_match.group(1) if branch_match else "",
                "head_sha": head_match.group(1) if head_match else "",
                "source": (
                    "activity_pr_evidence"
                    if pr_repo.lower() == repo_l else "activity_cross_repo_pr_evidence"
                ),
            }
        for match in GITHUB_PR_SHORTHAND_RE.finditer(text):
            if not repo:
                continue
            branch_match = BRANCH_EVIDENCE_RE.search(text)
            head_match = HEAD_EVIDENCE_RE.search(text)
            pr_number = int(match.group(1))
            return {
                "pr_number": pr_number,
                "pr_url": f"https://github.com/{repo}/pull/{pr_number}",
                "repo": repo,
                "branch": branch_match.group(1) if branch_match else "",
                "head_sha": head_match.group(1) if head_match else "",
                "source": "activity_pr_number_evidence",
            }
    return {}


def _infer_pr_evidence_from_git_state(git_state: Dict[str, Any],
                                      repo: str) -> Dict[str, Any]:
    pr_url = (git_state.get("pr_url") or "").strip()
    if not pr_url:
        return {}
    match = GITHUB_PR_URL_RE.search(pr_url)
    if not match:
        return {}
    pr_repo = match.group(1)
    return {
        "pr_number": int(match.group(2)),
        "pr_url": match.group(0),
        "repo": pr_repo,
        "branch": git_state.get("branch") or "",
        "head_sha": git_state.get("head_sha") or "",
        "source": (
            "git_state_pr_url"
            if not repo or pr_repo.lower() == repo.lower() else "git_state_cross_repo_pr_url"
        ),
    }


def _merge_pr_evidence(git_state: Dict[str, Any],
                       inferred: Dict[str, Any]) -> Dict[str, Any]:
    if not inferred:
        return {}
    evidence: Dict[str, Any] = {"source": inferred.get("source")}
    for field in ("pr_number", "pr_url", "repo", "branch", "head_sha"):
        value = inferred.get(field)
        if value and not git_state.get(field):
            evidence[field] = value
    return evidence if any(k != "source" for k in evidence) else {}


def _pr_references_task(pr: Dict[str, Any], task_id: str) -> bool:
    """True when a merged PR explicitly names the task in its branch ref or title.

    Guards the In Progress auto-promote path against mis-association: an In Progress
    task's own activity may reference another task's PR (coordination chatter), and we
    must never auto-stamp Done off that. We only promote when the PR itself carries the
    task id, matching the branch/commit naming convention (cursor/<TASK-ID>-slug,
    "<TASK-ID>: subject"). In Review/Done keep their existing behaviour (the agent
    explicitly asserted the PR by advancing lifecycle), so this check does not apply there.
    """
    if not task_id:
        return False
    token = re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(task_id) + r"(?![A-Za-z0-9])", re.I)
    head_ref = (pr.get("head") or {}).get("ref") or ""
    title = pr.get("title") or ""
    return bool(token.search(head_ref) or token.search(title))


def _external_reconcile_findings(tasks: List[Dict[str, Any]],
                                 git_states: Dict[str, Dict[str, Any]],
                                 canonical_main_sha: str,
                                 project: str = DEFAULT_PROJECT) -> Tuple[
                                     List[Dict[str, Any]],
                                     Dict[str, Any],
                                     List[Dict[str, Any]],
                                 ]:
    findings: List[Dict[str, Any]] = []
    backfilled: List[Dict[str, Any]] = []
    checks: Dict[str, Any] = {
        "git_reachability": "not_configured",
        "github_prs": "not_configured",
    }
    repo = get_project_github_repo(project)
    if canonical_main_sha and _git_checks_available():
        local_repo = _local_github_repo()
        project_repo = _normalize_repo_slug(repo)
        local_repo_norm = _normalize_repo_slug(local_repo)
        if project_repo and not local_repo_norm:
            checks["git_reachability"] = "skipped_local_repo_unknown"
            checks["git_reachability_detail"] = (
                "Local git checkout remote could not be mapped to a GitHub repo; "
                "project-scoped GitHub checks still run when configured."
            )
            checks["project_repo"] = repo
        elif project_repo and local_repo_norm and project_repo != local_repo_norm:
            checks["git_reachability"] = "skipped_repo_mismatch"
            checks["git_reachability_detail"] = (
                "Local git checkout repo does not match the selected project's GitHub repo; "
                "skipping cat-file/merge-base to avoid cross-project false positives."
            )
            checks["local_repo"] = local_repo
            checks["project_repo"] = repo
        else:
            checks["git_reachability"] = "checked"
            if local_repo:
                checks["local_repo"] = local_repo
            main_ref = canonical_main_sha
            if not _git_ok(["cat-file", "-e", f"{main_ref}^{{commit}}"]):
                checks["git_reachability"] = "blocked_missing_canonical_main"
                checks["canonical_main_sha"] = main_ref
                findings.append({
                    "severity": "high",
                    "task_id": None,
                    "code": "canonical_main_sha_not_found",
                    "detail": (
                        "Canonical main SHA is not present in the local git object database; "
                        "fetch or refresh the local checkout before per-task ancestry checks."
                    ),
                })
            else:
                for task in tasks:
                    task_id = task["task_id"]
                    state = git_states.get(task_id, {})
                    state_repo = _github_repo_from_pr_url(state.get("pr_url") or "")
                    state_role = get_project_repo_role(state_repo, project) if state_repo else {}
                    if state_repo and not state_role.get("canonical"):
                        continue
                    for field, severity in (("head_sha", "medium"), ("merged_sha", "high")):
                        if (field == "head_sha" and task.get("status") == "Done"
                                and state.get("merged_sha")):
                            continue
                        if (field == "head_sha" and state.get("pr_number")
                                and task.get("status") in ("In Review", "Done")):
                            # Production checkouts do not need to fetch every PR head. GitHub PR
                            # state below is the source of truth for open/review heads; local git
                            # reachability remains authoritative for merged/default-branch SHAs.
                            continue
                        sha = state.get(field)
                        if not sha:
                            continue
                        if not _git_ok(["cat-file", "-e", f"{sha}^{{commit}}"]):
                            findings.append({"severity": severity, "task_id": task_id,
                                             "code": f"{field}_not_found",
                                             "detail": f"Recorded {field} is not present in the local git object database."})
                            continue
                        if field == "merged_sha" and not _git_ok(["merge-base", "--is-ancestor", sha, main_ref]):
                            findings.append({"severity": "high", "task_id": task_id,
                                             "code": "merged_sha_not_on_canonical_main",
                                             "detail": "Recorded merged_sha is not reachable from canonical main."})

    token = _github_token()
    pr_tasks = [t for t in tasks if git_states.get(t["task_id"], {}).get("pr_number")]
    if repo:
        checks["github_repo"] = repo
        checks["github_prs"] = "checked" if token else "checked_unauthenticated"
    if repo and not pr_tasks:
        checks["github_prs"] = "configured_no_prs"
    if repo and pr_tasks:
        pr_repos = sorted({
            _github_repo_from_pr_url(git_states.get(t["task_id"], {}).get("pr_url") or "") or repo
            for t in pr_tasks
        })
        if pr_repos:
            checks["github_pr_repos"] = pr_repos
        for task in pr_tasks:
            state = git_states.get(task["task_id"], {})
            pr_repo = _github_repo_from_pr_url(state.get("pr_url") or "") or repo
            role_info = get_project_repo_role(pr_repo, project)
            pr = _github_pr(pr_repo, int(state.get("pr_number") or 0), token=token)
            if not pr:
                findings.append({"severity": "medium", "task_id": task["task_id"],
                                 "code": "pr_state_unavailable",
                                 "detail": f"Could not fetch recorded PR state from GitHub repo {pr_repo}."})
                continue
            merged = bool(pr.get("merged_at"))
            if not role_info.get("canonical"):
                findings.append({
                    "severity": "high" if merged or task.get("status") == "Done" else "medium",
                    "task_id": task["task_id"],
                    "code": "repo_role_cannot_mark_done",
                    "detail": (
                        f"Recorded PR is in repo role {role_info.get('role') or 'unknown'} "
                        f"({pr_repo}); only the project canonical repo can mark code work Done."
                    ),
                    "repo_role": role_info.get("role") or "unknown",
                    "repo": pr_repo,
                    "failure_class": "failed_gate",
                })
                continue
            if task.get("status") == "Done" and not merged:
                findings.append({"severity": "high", "task_id": task["task_id"],
                                 "code": "done_pr_not_merged",
                                 "detail": "Task is Done but the recorded GitHub PR is not merged."})
            merge_sha = pr.get("merge_commit_sha")
            base_ref = ((pr.get("base") or {}).get("ref") or "").strip()
            default_ref = (pr.get("base") or {}).get("repo", {}).get("default_branch") or ""
            default_branch_merge = bool(base_ref and default_ref and base_ref == default_ref)
            task_status = task.get("status")
            # In Progress tasks only reach here when a PR reference was hydrated from their
            # own activity/git_state (the agent merged without ever running complete_claim to
            # reach In Review). Auto-promote them ONLY when the PR actually merged into the
            # project's canonical default branch AND the PR itself names the task — never off a
            # feature/integration branch and never from a mis-attributed PR reference.
            stamp_eligible = (
                (task_status in ("In Review", "Done")
                 and (task_status != "Done" or not state.get("merged_sha")))
                or (task_status == "In Progress"
                    and default_branch_merge
                    and _pr_references_task(pr, task["task_id"]))
            )
            if merged and merge_sha and stamp_eligible:
                if default_branch_merge:
                    update_canonical_main_sha(merge_sha, "reconcile", project)
                stamped = mark_task_merged(
                    task["task_id"], merge_sha,
                    pr_number=int(state.get("pr_number") or 0) or None,
                    pr_url=state.get("pr_url") or pr.get("html_url") or "",
                    branch=((pr.get("head") or {}).get("ref") or state.get("branch") or ""),
                    head_sha=((pr.get("head") or {}).get("sha") or state.get("head_sha") or ""),
                    actor="reconcile",
                    project=project,
                )
                if not stamped.get("error"):
                    backfilled.append({
                        "task_id": task["task_id"],
                        "pr_number": state.get("pr_number"),
                        "merged_sha": merge_sha,
                    })
                    git_states[task["task_id"]] = stamped.get("git_state") or state
                    task["status"] = "Done"
                    state = git_states[task["task_id"]]
            if merged and state.get("merged_sha") and merge_sha and state["merged_sha"] != merge_sha:
                findings.append({"severity": "medium", "task_id": task["task_id"],
                                 "code": "merged_sha_mismatch",
                                 "detail": "Recorded merged_sha differs from GitHub PR merge_commit_sha."})

    orphan_findings, orphan_backfilled, orphan_checks = _orphan_merge_discovery_findings(
        tasks, git_states, project=project, repo=repo, token=token)
    findings.extend(orphan_findings)
    backfilled.extend(orphan_backfilled)
    checks.update(orphan_checks)
    return findings, checks, backfilled


def _orphan_merge_discovery_findings(
    tasks: List[Dict[str, Any]],
    git_states: Dict[str, Dict[str, Any]],
    *,
    project: str,
    repo: str,
    token: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    import orphan_merge_discovery

    lookback_days = int(os.environ.get("PM_ORPHAN_MERGE_LOOKBACK_DAYS", "30") or "30")
    now = time.time()
    with _conn(project) as c:
        active_claims = {
            row["task_id"]: dict(row)
            for row in c.execute(
                "SELECT id, task_id, agent_id FROM task_claims "
                "WHERE status='active' AND expires_at>?",
                (now,),
            ).fetchall()
        }

    def _mark_merged(task_id: str, merged_sha: str, **kwargs: Any) -> Dict[str, Any]:
        return mark_task_merged(task_id, merged_sha, **kwargs)

    return orphan_merge_discovery.discover_orphan_merges(
        tasks,
        git_states,
        project=project,
        repo=repo or "",
        token=token,
        lookback_days=lookback_days,
        active_claims=active_claims,
        role_checker=lambda repo_slug: get_project_repo_role(repo_slug, project=project),
        mark_merged_fn=_mark_merged,
        append_activity_fn=append_activity,
        now=now,
    )


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


def _reconcile_signature(findings: List[Dict[str, Any]]) -> str:
    material = [{
        "severity": f.get("severity") or "",
        "task_id": f.get("task_id") or "",
        "code": f.get("code") or "",
        "failure_class": f.get("failure_class") or "",
        "detail": f.get("detail") or "",
    } for f in sorted(findings, key=lambda x: (
        x.get("task_id") or "", x.get("code") or "", x.get("severity") or ""))]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def _format_reconcile_alert(project: str, findings: List[Dict[str, Any]],
                            signature: str, limit: int = 12) -> str:
    lines = [
        f"Reconcile alert for project `{project}`: {len(findings)} actionable finding(s).",
        f"signature={signature}",
    ]
    for f in findings[:limit]:
        task = f.get("task_id") or "board"
        failure_class = f.get("failure_class") or "failed_gate"
        lines.append(
            f"- [{f.get('severity')}] {task} {f.get('code')} "
            f"({failure_class}): {f.get('detail')}"
        )
    if len(findings) > limit:
        lines.append(f"- ... {len(findings) - limit} more; run reconcile(project={project!r}) for full detail.")
    lines.append("Treat this as a Switchboard-owned drift interrupt: fix provenance, release stale claims, or document the exception.")
    return "\n".join(lines)


def reconcile(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Local drift report for board provenance.

    Board-internal checks always run. When a canonical main SHA and local git checkout are
    available, reconcile also verifies recorded SHAs against git reachability. If GitHub repo
    config is present, PR records are checked through the GitHub API.
    """
    now = time.time()
    agreement = get_working_agreement(project)
    findings: List[Dict[str, Any]] = []
    tasks: List[Dict[str, Any]] = []
    git_states: Dict[str, Dict[str, Any]] = {}
    repo = get_project_github_repo(project)
    with _conn(project) as c:
        rows = c.execute("SELECT * FROM tasks ORDER BY sort_order, task_id").fetchall()
        for row in rows:
            task = _task_row(row)
            git_state = _load_git_state(c, task["task_id"])
            tasks.append(task)
            status = task.get("status")
            needs_pr_hydration = (
                repo and
                not git_state.get("pr_number") and
                (
                    status in ("In Review", "In Progress") or
                    (status == "Done" and not _has_done_provenance(git_state))
                )
            )
            if needs_pr_hydration:
                inferred = (
                    _infer_pr_evidence_from_git_state(git_state, repo)
                    or _infer_pr_evidence_from_activity(c, task["task_id"], repo)
                )
                evidence = _merge_pr_evidence(git_state, inferred)
                if evidence:
                    git_state = _upsert_git_state(c, task["task_id"], {
                        "pr_number": evidence.get("pr_number"),
                        "pr_url": evidence.get("pr_url"),
                        "branch": evidence.get("branch") or None,
                        "head_sha": evidence.get("head_sha") or None,
                        "pushed_at": now if evidence.get("head_sha") else None,
                        "evidence": evidence,
                    })
                    c.execute(
                        "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                        (task["task_id"], "reconcile", "git.pr_evidence_hydrated",
                         json.dumps(evidence, sort_keys=True), now),
                    )
            git_states[task["task_id"]] = git_state
            if (status == "Done" and not _has_done_provenance(git_state)
                    and not (repo and git_state.get("pr_number"))):
                findings.append({"severity": "high", "task_id": task["task_id"],
                                 "code": "done_without_merged_sha",
                                 "detail": "Task is Done but has no recorded merge/default-branch or offline evidence provenance."})
            if status == "In Review" and not (git_state.get("branch") or git_state.get("pr_url")):
                findings.append({"severity": "medium", "task_id": task["task_id"],
                                 "code": "review_without_provenance",
                                 "detail": "Task is In Review but lacks branch/PR evidence."})
            if (status == "In Progress" and not git_state.get("head_sha")
                    and not git_state.get("pr_number")):
                # A hydrated pr_number means this task has PR evidence pending merge-provenance
                # evaluation below (open PR, or a merge about to be auto-stamped) — the PR checks
                # own its provenance state, so don't also flag it as "no pushed head".
                findings.append({"severity": "low", "task_id": task["task_id"],
                                 "code": "progress_without_pushed_head",
                                 "detail": "Task is In Progress with no reported pushed head SHA."})
            _upsert_git_state(c, task["task_id"], {"last_reconciled_at": now})
        stale_task_claims = c.execute(
            "SELECT id, task_id, agent_id, expires_at FROM task_claims "
            "WHERE status='active' AND expires_at<=? ORDER BY expires_at",
            (now,),
        ).fetchall()
        for claim in stale_task_claims:
            findings.append({"severity": "medium", "task_id": claim["task_id"],
                             "code": "stale_task_claim",
                             "detail": f"Active task claim {claim['id']} by {claim['agent_id']} expired without completion or abandon."})
        stale_file_leases = c.execute(
            "SELECT id, task_id, agent_id, claimed_at, ttl_minutes FROM file_leases "
            "WHERE released_at IS NULL ORDER BY claimed_at"
        ).fetchall()
        for lease in stale_file_leases:
            expires_at = float(lease["claimed_at"] or 0) + int(lease["ttl_minutes"] or 0) * 60
            if expires_at <= now:
                findings.append({"severity": "medium", "task_id": lease["task_id"],
                                 "code": "stale_file_lease",
                                 "detail": f"File lease {lease['id']} by {lease['agent_id']} expired without release."})
        stale_resource_leases = c.execute(
            "SELECT id, task_id, agent_id, resource_type, claimed_at, ttl_seconds FROM resource_leases "
            "WHERE released_at IS NULL ORDER BY claimed_at"
        ).fetchall()
        for lease in stale_resource_leases:
            expires_at = float(lease["claimed_at"] or 0) + int(lease["ttl_seconds"] or 0)
            if expires_at <= now:
                findings.append({"severity": "medium", "task_id": lease["task_id"],
                                 "code": "stale_resource_lease",
                                 "detail": f"{lease['resource_type']} lease {lease['id']} by {lease['agent_id']} expired without release."})
        tasks_by_id = {task["task_id"]: task for task in tasks}
        for report in _evidence_claim_reports(c):
            if report.get("status") == "pass":
                continue
            task_id = report.get("task_id")
            task = tasks_by_id.get(task_id) if task_id else None
            if (task and task.get("status") == "Done"
                    and _has_done_provenance(git_states.get(task_id, {}))):
                continue
            artifacts = ", ".join(report.get("claim", {}).get("artifacts") or [])
            evidence_values = []
            declared = report.get("declared_evidence") or {}
            for key in ("paths", "urls", "refs"):
                evidence_values.extend(declared.get(key) or [])
            detail = report.get("detail") or "Claim evidence could not be verified."
            if artifacts:
                detail += f" Claimed artifact(s): {artifacts}."
            if evidence_values:
                detail += f" Declared evidence: {', '.join(evidence_values)}."
            findings.append({
                "severity": report.get("severity") or "medium",
                "task_id": report.get("task_id"),
                "code": report.get("code") or "claim_without_evidence",
                "failure_class": report.get("failure_class") or "missing_data",
                "detail": detail,
                "evidence_claim": report,
            })
        cursor = c.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0]
    external_findings, external_checks, backfilled = _external_reconcile_findings(
        tasks, git_states, agreement.get("canonical_main_sha") or "", project=project)
    findings.extend(external_findings)
    publication_findings, publication_checks = _publication_reconcile_findings(
        tasks, git_states, project=project)
    findings.extend(publication_findings)
    external_checks.update(publication_checks)
    if not (agreement.get("canonical_main_sha") or get_meta("canonical_main_sha", None, project=project)):
        findings.append({"severity": "medium", "task_id": None,
                         "code": "missing_canonical_main_sha",
                         "detail": "No canonical main SHA recorded yet; wait for a default-branch push webhook or set meta."})
    findings = [_annotate_reconcile_finding(f) for f in findings]
    append_activity("reconcile.completed", "reconcile",
                    {"findings": len(findings), "backfilled": backfilled},
                    task_id=None, project=project)
    return {"project": project, "ok": not findings, "findings": findings,
            "activity_cursor": cursor, "checked_at": now,
            "external_checks": external_checks, "backfilled": backfilled}


def run_reconcile_alerts(project: str = DEFAULT_PROJECT,
                         alert_to: str = "switchboard/operator",
                         actor: str = "switchboard/reconcile",
                         min_severity: str = "medium",
                         dedupe_window_s: int = 3600,
                         now: Optional[float] = None) -> Dict[str, Any]:
    """Run reconcile and send a deduped directed alert for actionable findings.

    The dedupe key is project + severity floor + finding signature + time bucket, so a
    persistent unresolved issue alerts at most once per bucket while a new drift shape alerts
    immediately.
    """
    now = time.time() if now is None else float(now)
    alert_to = (alert_to or "switchboard/operator").strip()
    min_severity = (min_severity or "medium").strip().lower()
    floor = _severity_value(min_severity)
    if floor <= 0:
        min_severity = "medium"
        floor = _severity_value(min_severity)
    dedupe_window_s = max(60, int(dedupe_window_s or 3600))
    report = reconcile(project=project)
    findings = [f for f in report["findings"]
                if _severity_value(str(f.get("severity") or "")) >= floor]
    if not findings:
        return {"project": project, "ok": True, "alert_sent": False, "deduped": False,
                "finding_count": 0, "min_severity": min_severity,
                "checked_at": report["checked_at"], "external_checks": report["external_checks"]}

    signature = _reconcile_signature(findings)
    window = int(now // dedupe_window_s)
    idem_key = f"reconcile-alert:{project}:{min_severity}:{alert_to}:{window}:{signature}"
    payload = {"project": project, "alert_to": alert_to, "min_severity": min_severity,
               "dedupe_window_s": dedupe_window_s, "signature": signature,
               "finding_count": len(findings)}
    with _conn(project) as c:
        hit = _idem_hit(c, "reconcile_alert", idem_key, actor, payload)
    if hit is not None:
        if "error" in hit:
            return hit
        out = dict(hit)
        out["alert_sent"] = False
        out["deduped"] = True
        return out

    message = _format_reconcile_alert(project, findings, signature)
    msg = send_agent_message(
        from_agent=actor,
        to_agent=alert_to,
        task_id=None,
        message=message,
        requires_ack=True,
        signal="reconcile_alert",
        priority=90,
        idem_key=f"{idem_key}:message",
        project=project,
    )
    response = {"project": project, "ok": False, "alert_sent": True,
                "deduped": False, "message_id": msg["id"],
                "finding_count": len(findings), "min_severity": min_severity,
                "signature": signature, "dedupe_window_s": dedupe_window_s,
                "checked_at": report["checked_at"],
                "external_checks": report["external_checks"],
                "findings": findings}
    with _conn(project) as c:
        _idem_store(c, "reconcile_alert", idem_key, actor, payload, response)
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "reconcile.alert",
                   json.dumps({k: v for k, v in response.items() if k != "findings"},
                              sort_keys=True), now))
    return response


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
# Heavy per-task fields the board list/cards never render — they surface only in
# the task-detail modal, which re-fetches the full task via GET /api/tasks/{id}.
# Dropping them from the (large, whole-board) payload roughly halves it on the
# wire with zero UI change. Kept: description (board search matches it) and
# provenance (the card's Done-proof badge reads it).
_BOARD_LITE_DROP = ("session_health", "external_ci", "publication", "exit_criteria", "agent_state")

# Short-TTL in-memory cache for the hot lite board (HARDEN-36). Keyed by the
# project's latest task mutation, so any write invalidates it immediately and a
# burst of loads (tab refocus, several viewers) rebuilds at most once per TTL.
_BOARD_CACHE: Dict[str, Dict[str, Any]] = {}
_BOARD_CACHE_TTL = 3.0


def _build_board_payload(project: str, lite: bool) -> Dict[str, Any]:
    # The lite path uses the batched, enrichment-free loader (HARDEN-34); rollups
    # read only base fields (status/workstream/effort), so slim rows are enough.
    tasks = list_tasks_for_board(project) if lite else list_tasks(project=project)
    payload: Dict[str, Any] = {k: get_meta(k, project=project) for k in META_SECTIONS}
    payload["project"] = next((p for p in projects() if p["id"] == project), {
        "id": project,
        "label": project,
        "pretitle": "",
        "purpose": project_access(project).get("purpose") or "",
        "boundary": project_access(project).get("boundary") or "",
    })
    payload["rollups"] = board_rollups(project=project, tasks=tasks)
    ws_tasks = tasks
    if lite:
        ws_tasks = [{k: v for k, v in t.items() if k not in _BOARD_LITE_DROP} for t in tasks]
    by_ws: Dict[str, Dict[str, Any]] = {}
    for t in ws_tasks:
        ws = by_ws.setdefault(t["_wsId"], {"workstream_id": t["_wsId"], "name": t["_wsName"], "tasks": []})
        ws["tasks"].append(t)
    payload["workstreams"] = list(by_ws.values())
    payload["project_context"] = get_project_context(project)
    return payload


def board_payload(project: str = DEFAULT_PROJECT, lite: bool = False) -> Dict[str, Any]:
    if not lite:
        return _build_board_payload(project, lite=False)
    try:
        with _conn(project) as c:
            latest = c.execute("SELECT MAX(updated_at) FROM tasks").fetchone()[0] or 0
    except Exception:
        return _build_board_payload(project, lite=True)
    now = time.time()
    cached = _BOARD_CACHE.get(project)
    if cached and cached["key"] == latest and (now - cached["at"]) < _BOARD_CACHE_TTL:
        return cached["payload"]
    payload = _build_board_payload(project, lite=True)
    _BOARD_CACHE[project] = {"key": latest, "at": now, "payload": payload}
    return payload
