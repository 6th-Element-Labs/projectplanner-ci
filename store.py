"""SQLite store for the taikun-pm satellite — tasks + activity, seeded from a
bundled plan snapshot. One file, zero ops (see ADR 0007). No shared DB touched."""
import json
import hashlib
import os
import re
import sqlite3
import subprocess
import time
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

DB_PATH = os.environ.get("PM_DB_PATH", os.path.join(os.path.dirname(__file__), "taikun_pm.db"))
SEED_PATH = os.environ.get("PM_SEED_PATH", os.path.join(os.path.dirname(__file__), "seed_plan.json"))
HELM_DB_PATH = os.environ.get("PM_HELM_DB_PATH", os.path.join(os.path.dirname(__file__), "helm.db"))
HELM_SEED_PATH = os.environ.get("PM_HELM_SEED_PATH",
                                os.path.join(os.path.dirname(__file__), "seeds", "helm_seed_plan.json"))
SWITCHBOARD_DB_PATH = os.environ.get("PM_SWITCHBOARD_DB_PATH",
                                     os.path.join(os.path.dirname(__file__), "switchboard.db"))
SWITCHBOARD_SEED_PATH = os.environ.get("PM_SWITCHBOARD_SEED_PATH",
                                       os.path.join(os.path.dirname(__file__), "seeds",
                                                    "switchboard_seed_plan.json"))
PROJECT_REGISTRY_DB_PATH = os.environ.get(
    "PM_PROJECT_REGISTRY_DB_PATH",
    os.path.join(os.path.dirname(DB_PATH), "project_registry.db"),
)
PROJECT_ID_SLUG_RE = re.compile(r"[^a-z0-9_-]+")
PROJECT_ID_VALID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GITHUB_PR_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)")
GITHUB_PR_SHORTHAND_RE = re.compile(r"\bPR\s*#?\s*(\d+)\b", re.I)
BRANCH_EVIDENCE_RE = re.compile(r"\bbranch(?:\s+was)?[:\s]+`?([A-Za-z0-9._/-]+)`?", re.I)
HEAD_EVIDENCE_RE = re.compile(r"\bhead(?:_sha|\s+sha)?[:\s]+`?([0-9a-f]{7,40})`?", re.I)

# Multi-project registry. Each project is its OWN sqlite file — physical isolation, so a Helm
# request can never read or write Maxwell's rows (no shared table, no project_id column). The
# default is always 'maxwell', so every existing caller behaves exactly as before.
BUILTIN_PROJECTS = {
    "maxwell": {"db": DB_PATH, "seed": SEED_PATH,
                "label": "Project Maxwell", "pretitle": "TEEP Barnett · TotalEnergies E&P"},
    "helm": {"db": HELM_DB_PATH, "seed": HELM_SEED_PATH,
             "label": "Helm — Marine Nav Companion", "pretitle": "6th Element Labs · web-first chartplotter"},
    "switchboard": {"db": SWITCHBOARD_DB_PATH, "seed": SWITCHBOARD_SEED_PATH,
                    "label": "Switchboard — Agent Coordination Layer",
                    "pretitle": "6th Element Labs · live dogfood control plane"},
}
BUILTIN_GITHUB_REPOS = {
    "helm": "StevenRidder/Helm",
    "switchboard": "6th-Element-Labs/projectplanner",
}
# Back-compat for older call sites that only need the built-in project set. New code should call
# project_ids(), has_project(), projects(), or _resolve() so dynamic projects are included.
PROJECTS = BUILTIN_PROJECTS
DEFAULT_PROJECT = "maxwell"
TASK_ID_RE = re.compile(r"\b([A-Z]+-\d+)\b")
DEFAULT_ORG_ID = "org-6th-element-labs"
ROLE_SCOPES = {
    "viewer": ["read"],
    "commenter": ["read", "write:comments"],
    "contributor": ["read", "write:tasks", "write:ixp", "write:bug_intake"],
    "operator": ["read", "write:tasks", "write:ixp", "write:bug_intake"],
    "admin": ["read", "write:tasks", "write:ixp", "write:system", "write:bug_intake", "admin"],
    "owner": ["read", "write:tasks", "write:ixp", "write:system", "write:bug_intake", "admin"],
}


def _registry_conn():
    os.makedirs(os.path.dirname(PROJECT_REGISTRY_DB_PATH), exist_ok=True)
    c = sqlite3.connect(PROJECT_REGISTRY_DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


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
                updated_at    REAL NOT NULL
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
                PRIMARY KEY (project_id, subject_kind, subject_id, role)
            );
            """
        )


def normalize_project_id(value: str) -> str:
    """Turn a human project name like 'Vulkan Renderer' into a stable project id."""
    slug = PROJECT_ID_SLUG_RE.sub("-", (value or "").strip().lower()).strip("-_")
    slug = re.sub(r"[-_]{2,}", "-", slug)
    return slug


def _dynamic_projects() -> Dict[str, Dict[str, str]]:
    init_project_registry()
    with _registry_conn() as c:
        rows = c.execute("SELECT * FROM projects ORDER BY id").fetchall()
    return {
        r["id"]: {
            "db": r["db_path"],
            "seed": r["seed_path"],
            "label": r["label"],
            "pretitle": r["pretitle"] or "",
        }
        for r in rows
    }


def _project_map() -> Dict[str, Dict[str, str]]:
    return {**_dynamic_projects(), **BUILTIN_PROJECTS}


def project_ids() -> List[str]:
    return list(_project_map())


def has_project(project: Optional[str]) -> bool:
    return (project or DEFAULT_PROJECT) in _project_map()


def hash_token(token: str) -> str:
    """Stable one-way token hash for principal lookup."""
    return hashlib.sha256(("switchboard:" + (token or "")).encode("utf-8")).hexdigest()


def projects() -> List[Dict[str, Any]]:
    """The switcher's source of truth — [{id, label, pretitle}]."""
    return [{"id": k, "label": v["label"], "pretitle": v.get("pretitle", "")}
            for k, v in _project_map().items()]


def role_scopes(role: str) -> List[str]:
    return list(ROLE_SCOPES.get((role or "").strip().lower(), []))


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
                       created_by: str = "system") -> Dict[str, Any]:
    init_project_registry()
    if not has_project(project_id):
        return {"error": f"unknown project: {project_id}"}
    if not org_id:
        return {"error": "org_id required"}
    now = time.time()
    with _registry_conn() as c:
        c.execute(
            "INSERT INTO project_access(project_id, org_id, owner_user_id, purpose, boundary, "
            "created_at, created_by, updated_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(project_id) DO UPDATE SET org_id=excluded.org_id, "
            "owner_user_id=excluded.owner_user_id, purpose=excluded.purpose, "
            "boundary=excluded.boundary, updated_at=excluded.updated_at",
            (project_id, org_id, owner_user_id or None, purpose or None, boundary or None,
             now, created_by, now),
        )
        row = c.execute("SELECT * FROM project_access WHERE project_id=?",
                        (project_id,)).fetchone()
    return dict(row)


def project_access(project_id: str) -> Dict[str, Any]:
    init_project_registry()
    with _registry_conn() as c:
        row = c.execute("SELECT * FROM project_access WHERE project_id=?",
                        (project_id,)).fetchone()
    return dict(row) if row else {}


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


def coerce_csv_list(value: Any) -> List[str]:
    """Normalize REST/CLI list fields that may arrive as a list or comma/newline string."""
    if value is None:
        return []
    raw = value if isinstance(value, list) else [value]
    out: List[str] = []
    for item in raw:
        for part in str(item).replace("\n", ",").split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _resolve(project: Optional[str]) -> Dict[str, str]:
    """Map a project id -> its config. Fail CLOSED on an unknown id — never silently fall back
    to Maxwell (which could leak a write across projects)."""
    p = _project_map().get(project or DEFAULT_PROJECT)
    if not p:
        raise ValueError(f"unknown project: {project!r}")
    return p

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
    "done_pr_not_merged": "hidden_fallback",
    "done_without_merged_sha": "hidden_fallback",
    "head_sha_not_found": "stale_branch",
    "merged_sha_mismatch": "invalid_input",
    "merged_sha_not_found": "stale_branch",
    "merged_sha_not_on_canonical_main": "stale_branch",
    "missing_canonical_main_sha": "missing_data",
    "progress_without_pushed_head": "missing_data",
    "pr_state_unavailable": "broken_connection",
    "review_without_provenance": "missing_data",
    "stale_file_lease": "failed_gate",
    "stale_resource_lease": "failed_gate",
    "stale_task_claim": "failed_gate",
}

# Plan-level sections that are not per-task (kept verbatim from the seed snapshot).
META_SECTIONS = ["project", "generated", "schedule_start", "schedule_note", "owner_orgs",
                 "rollups", "executive_summary", "timeline_note", "critical_path",
                 "milestones", "consolidated_risks", "consolidated_decisions", "people",
                 "working_agreement"]

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

# A sensible default people list for the assignee picker (the real names in the plan).
DEFAULT_PEOPLE = ["Steve Ridder", "Taikun eng", "Darko", "Sahir", "Sebastian", "Mike",
                  "Michelle", "Sierra", "Clovis", "Devin", "Brent", "IFS owner", "Nubo"]


def _sqlite_timeout_s(env_name: str, default_s: float) -> float:
    try:
        return max(0.0, float(os.environ.get(env_name, str(default_s))))
    except (TypeError, ValueError):
        return default_s


def _conn(project: str = DEFAULT_PROJECT, timeout_s: Optional[float] = None):
    timeout = _sqlite_timeout_s("PM_SQLITE_TIMEOUT_S", 5.0) if timeout_s is None else timeout_s
    c = sqlite3.connect(_resolve(project)["db"], timeout=timeout)
    c.row_factory = sqlite3.Row
    c.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _control_plane_timeout_s() -> float:
    return _sqlite_timeout_s("PM_CONTROL_PLANE_SQLITE_TIMEOUT_S", 2.0)


def _control_plane_conn(project: str = DEFAULT_PROJECT):
    return _conn(project, timeout_s=_control_plane_timeout_s())


def _sqlite_busy(exc: Exception) -> bool:
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and (
        "database is locked" in text or "database is busy" in text or "locked" in text
    )


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


def _json_size_bytes(value: Any) -> int:
    try:
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except TypeError:
        body = str(value).encode("utf-8")
    return len(body)


def _activity_cursor(project: str = DEFAULT_PROJECT) -> int:
    with _control_plane_conn(project) as c:
        return int(c.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0] or 0)


def init_db(project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
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
                created_at  REAL NOT NULL
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
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                idem_key      TEXT NOT NULL,
                operation     TEXT NOT NULL,
                actor         TEXT NOT NULL,
                request_hash  TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at    REAL NOT NULL,
                PRIMARY KEY (idem_key, operation)
            );
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
                task_id           TEXT,
                principal_id      TEXT,
                idem_key          TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_wake_intents_status
                ON wake_intents(status, deadline, requested_at);
            CREATE INDEX IF NOT EXISTS ix_wake_intents_host
                ON wake_intents(claimed_by_host, status);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_wake_intents_idem
                ON wake_intents(idem_key) WHERE idem_key IS NOT NULL;
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
                options_json      TEXT NOT NULL DEFAULT '{}'
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
            CREATE INDEX IF NOT EXISTS ix_tasks_ws ON tasks(workstream_id);
            CREATE INDEX IF NOT EXISTS ix_inbox_status ON inbox(status);
            CREATE INDEX IF NOT EXISTS ix_activity_task ON activity(task_id);
            CREATE INDEX IF NOT EXISTS ix_activity_ts ON activity(created_at);
            CREATE INDEX IF NOT EXISTS ix_chat_session ON chat(session);
            CREATE INDEX IF NOT EXISTS ix_leases_agent ON file_leases(agent_id);
            """
        )
        # Additive column migrations — safe to run on every startup
        for col_sql in [
            "ALTER TABLE tasks ADD COLUMN agent_state TEXT",  # JSON blob per agent
            "ALTER TABLE agent_messages ADD COLUMN signal TEXT",
            "ALTER TABLE agent_messages ADD COLUMN priority INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE agent_messages ADD COLUMN idem_key TEXT",
            "ALTER TABLE agent_messages ADD COLUMN principal_id TEXT",
        ]:
            try:
                c.execute(col_sql)
            except Exception:
                pass  # column already exists
        try:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_idem "
                      "ON agent_messages(idem_key) WHERE idem_key IS NOT NULL")
        except Exception:
            pass


def seed_if_empty(project: str = DEFAULT_PROJECT):
    with _conn(project) as c:
        n = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        if n:
            return n
        seed_path = _resolve(project)["seed"]
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
    return {
        "stale": stale,
        "flags": flags,
        "message": (
            "Generated rationale may be stale; trust status, dependency_state, "
            "git_state, and provenance."
        ) if stale else None,
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


def _parse_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if text[0:1] in ("{", "["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": value}
        return {"text": value}
    return value


def _slug_token(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (value or "").strip().lower()).strip("_")


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


def _json_payload(raw: str) -> Any:
    """Parse payload JSON while preserving legacy scalar payloads."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw}


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
            "label": "Offline evidence",
            "verifier": offline.get("verifier"),
            "reviewed_at": offline.get("reviewed_at"),
            "artifact_url": offline.get("artifact_url"),
            "evidence_hash": offline.get("evidence_hash"),
        }
    if git_state.get("merged_sha"):
        return {
            "type": "github_pr_merged" if git_state.get("pr_number") else "default_branch_commit",
            "label": "Merged code",
            "merged_sha": git_state.get("merged_sha"),
            "pr_number": git_state.get("pr_number"),
            "pr_url": git_state.get("pr_url"),
        }
    if git_state.get("pr_number") or git_state.get("pr_url"):
        return {
            "type": "github_pr_open",
            "label": "PR evidence",
            "pr_number": git_state.get("pr_number"),
            "pr_url": git_state.get("pr_url"),
        }
    if git_state.get("head_sha"):
        return {
            "type": "branch_head",
            "label": "Branch evidence",
            "head_sha": git_state.get("head_sha"),
        }
    return {"type": None, "label": "No provenance"}


def _load_git_state(c: sqlite3.Connection, task_id: str) -> Dict[str, Any]:
    state = _git_state_row(c.execute("SELECT * FROM task_git_state WHERE task_id=?",
                                     (task_id,)).fetchone())
    state["provenance_type"] = _provenance_summary(state)["type"]
    return state


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
            tasks.append(t)
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
            return None
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


def _request_hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _idem_hit(c: sqlite3.Connection, operation: str, idem_key: str,
              actor: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not idem_key:
        return None
    row = c.execute("SELECT request_hash, response_json FROM idempotency_keys "
                    "WHERE idem_key=? AND operation=?", (idem_key, operation)).fetchone()
    if not row:
        return None
    if row["request_hash"] != _request_hash(payload):
        return {"error": "idempotency conflict", "idem_key": idem_key, "operation": operation}
    return json.loads(row["response_json"])


def _idem_store(c: sqlite3.Connection, operation: str, idem_key: str,
                actor: str, payload: Dict[str, Any], response: Dict[str, Any]) -> None:
    if not idem_key:
        return
    c.execute(
        "INSERT OR REPLACE INTO idempotency_keys"
        "(idem_key, operation, actor, request_hash, response_json, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (idem_key, operation, actor, _request_hash(payload), json.dumps(response, sort_keys=True), time.time()),
    )


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
    principal_id = principal_id or f"{kind}-{uuid.uuid4().hex[:12]}"
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


def _json_obj(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


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


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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
    status = str(session.get("status") or "").lower()
    if session.get("stale") or status in {"exited", "killed", "failed", "completed"}:
        return []
    actions: List[str] = []
    if control.get("managed_process") and session.get("host_id"):
        actions.append("snapshot")
    if control.get("runner_kill"):
        actions.append("kill")
    if control.get("runner_restart"):
        actions.append("restart")
    return actions


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
    if include_claim and c is not None and d.get("claim_id"):
        claim = c.execute("SELECT * FROM task_claims WHERE id=?", (d["claim_id"],)).fetchone()
        d["claim"] = dict(claim) if claim else None
    return d


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
                        now: float, idem_key: str = "") -> Dict[str, Any]:
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
        "status, requested_at, deadline, result_json, task_id, principal_id, idem_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (wake_id, source, reason, json.dumps(selector, sort_keys=True),
         json.dumps(policy, sort_keys=True), status, now, deadline,
         json.dumps(result, sort_keys=True), task_id, principal_id or None, idem_key or None),
    )
    payload = {"wake_id": wake_id, "source": source, "reason": reason,
               "selector": selector, "policy": policy, "status": status,
               "eligible_host_count": len(eligible)}
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
            wake = _insert_wake_intent(
                c, selector=selector, reason=reason or "wake requested",
                source=source or actor, policy=policy, task_id=task_id,
                principal_id=principal_id, actor=actor, now=now, idem_key=idem_key)
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
    if action not in {"snapshot", "kill", "restart"}:
        return {"requested": False, "error": "unsupported_action", "action": action}
    with _conn(project) as c:
        row = c.execute("SELECT * FROM runner_sessions WHERE runner_session_id=?",
                        (runner_session_id,)).fetchone()
        if not row:
            return {"requested": False, "error": "runner_session_not_found",
                    "runner_session_id": runner_session_id}
        session = _runner_session_row(row, now=now, include_claim=True, c=c)
        available = set(session.get("available_actions") or [])
        request_id = "runnerreq-" + uuid.uuid4().hex[:16]
        snapshot = _runner_snapshot_from_session(session, reason=f"before_{action}")
        req_status = "pending" if action in available else "refused"
        result = {}
        if req_status == "refused":
            result = {
                "reason": "action_not_supported",
                "available_actions": sorted(available),
                "control": session.get("control") or {},
            }
        c.execute(
            "INSERT INTO runner_control_requests(request_id, runner_session_id, host_id, "
            "action, status, reason, requested_by, principal_id, requested_at, "
            "snapshot_json, result_json, options_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
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
            ),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (session.get("task_id") or None, actor,
                   f"runner.{action}_{'requested' if req_status == 'pending' else 'refused'}",
                   json.dumps({"request_id": request_id,
                               "runner_session_id": runner_session_id,
                               "host_id": session.get("host_id"),
                               "status": req_status,
                               "reason": reason or "",
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
                               "status": final_status,
                               "result": merged_result,
                               "snapshot": snapshot}, sort_keys=True), now))
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
               project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Atomically claim one specific ready, unblocked task.

    Use this when a human/operator has already selected the task. Unlike claim_next,
    this never substitutes a different scheduler-preferred task.
    """
    now = time.time()
    task_id = (task_id or "").strip()
    payload = {"task_id": task_id, "agent_id": agent_id,
               "ttl_seconds": ttl_seconds,
               "override_identity_risk": bool(override_identity_risk)}
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
        }
        _idem_store(c, "claim_task", idem_key, actor, payload, response)
        return response


def claim_next(agent_id: str, lanes: Any = None,
               capabilities: Any = None,
               max_risk: str = "", max_budget_usd: Optional[float] = None,
               principal_id: str = "", actor: str = "system",
               ttl_seconds: int = 1800, idem_key: str = "",
               override_identity_risk: bool = False,
               project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Atomically claim the highest-priority unblocked task for an agent.

    This is the first TXP slice: deterministic, dependency-aware, and intentionally
    conservative. More sophisticated cost/reliability scoring can layer onto the same
    task_claims/activity records.
    """
    now = time.time()
    lanes = coerce_csv_list(lanes)
    capabilities = coerce_csv_list(capabilities)
    lane_set = {x.strip().upper() for x in lanes}
    cap_set = {x.strip().lower() for x in capabilities}
    max_risk_value = _risk_value(max_risk)
    payload = {"agent_id": agent_id, "lanes": sorted(lane_set),
               "capabilities": sorted(capabilities or []), "max_risk": max_risk,
               "max_budget_usd": max_budget_usd, "ttl_seconds": ttl_seconds,
               "override_identity_risk": bool(override_identity_risk)}
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
                   "identity_unknown": 0}
        identity_risks: Dict[str, Dict[str, Any]] = {}
        human_gates: Dict[str, Dict[str, Any]] = {}
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
                                            "identity_risks": identity_risks}}
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
        }
        _idem_store(c, "claim_next", idem_key, actor, payload, response)
        return response


def complete_claim(claim_id: str, evidence: str = "", final_status: str = "",
                   actor: str = "system",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
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
        c.execute("UPDATE task_claims SET status='completed', completed_at=? WHERE id=?",
                  (now, claim_id))
        c.execute("UPDATE resource_leases SET released_at=? WHERE resource_type='task' "
                  "AND task_id=? AND agent_id=? AND released_at IS NULL",
                  (now, row["task_id"], row["agent_id"]))
        c.execute("UPDATE tasks SET status=?, updated_at=? WHERE task_id=? "
                  "AND status NOT IN ('Done', 'Cancelled', 'Canceled')",
                  (next_status, now, row["task_id"]))
        git_state = _upsert_git_state(c, row["task_id"], {
            "branch": evidence_obj.get("branch"),
            "head_sha": evidence_obj.get("head_sha"),
            "pushed_at": pushed_at,
            "pr_number": evidence_obj.get("pr_number"),
            "pr_url": evidence_obj.get("pr_url"),
            "merged_sha": evidence_obj.get("merged_sha"),
            "merged_at": merged_at,
            "in_main_content": True if evidence_obj.get("merged_sha") else None,
            "evidence": evidence_obj,
        })
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
        if done_gate:
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"], actor, "task.done_blocked",
                       json.dumps({"claim_id": claim_id, "evidence": evidence_obj,
                                   "done_gate": done_gate,
                                   "source": "complete_claim"}, sort_keys=True), now))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (row["task_id"], actor, "task.claim.completed",
                   json.dumps({"claim_id": claim_id, "evidence": evidence_obj,
                               "requested_status": requested_status or None,
                               "next_status": next_status,
                               "done_gate": done_gate,
                               "terminal_status_preserved": terminal_status_preserved},
                              sort_keys=True), now))
    response = {"completed": True, "claim_id": claim_id, "task_id": row["task_id"],
                "status": next_status, "git_state": git_state}
    if done_gate:
        response["done_gate"] = done_gate
        response["warning"] = done_gate["message"]
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
                     project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
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
            "evidence": {"merged_sha": merged_sha, "pr_number": pr_number,
                         "pr_url": pr_url, "branch": branch, "head_sha": head_sha},
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


def _jsonish(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except Exception:
            return {"text": value}
    return {"value": value}


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


def record_decision(task_id: Optional[str], author: str, title: str,
                    context: str, decision: str, rationale: str,
                    supersedes: Optional[int] = None,
                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Append an architectural decision record (ADR-lite) to the decisions log.
    Immutable once written — to reverse, record a new decision with status='superseded'
    and reference the old id in supersedes. Returns the full record."""
    now = time.time()
    with _conn(project) as c:
        cur = c.execute(
            "INSERT INTO decisions(task_id, author, title, context, decision, rationale, "
            "supersedes, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (task_id, author, title, context, decision, rationale, supersedes, now),
        )
        dec_id = cur.lastrowid
        if supersedes:
            c.execute("UPDATE decisions SET status='superseded' WHERE id=?", (supersedes,))
    return {"id": dec_id, "task_id": task_id, "author": author, "title": title,
            "context": context, "decision": decision, "rationale": rationale,
            "status": "accepted", "supersedes": supersedes, "created_at": now}


def list_decisions(task_id: Optional[str] = None, status: str = "",
                   project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """List decisions, optionally filtered by task_id and/or status ('accepted',
    'superseded', 'proposed'). Returns newest-first."""
    q = "SELECT * FROM decisions WHERE 1=1"
    p: List[Any] = []
    if task_id:
        q += " AND task_id=?"; p.append(task_id)
    if status:
        q += " AND status=?"; p.append(status)
    q += " ORDER BY id DESC"
    with _conn(project) as c:
        rows = c.execute(q, p).fetchall()
    return [dict(r) for r in rows]


def get_decision(decision_id: int, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        r = c.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    return dict(r) if r else None


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


def set_task_summary(task_id: str, rationale: str, activity_cursor: int,
                     project: str = DEFAULT_PROJECT) -> None:
    """Upsert the Haiku-generated rationale for a task."""
    with _conn(project) as c:
        c.execute(
            "INSERT OR REPLACE INTO task_summaries(task_id, rationale, generated_at, activity_cursor) "
            "VALUES (?,?,?,?)",
            (task_id, rationale, time.time(), activity_cursor),
        )


def get_task_summary(task_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        r = c.execute("SELECT * FROM task_summaries WHERE task_id=?", (task_id,)).fetchone()
        return dict(r) if r else None


def get_tasks_needing_summary(project: str = DEFAULT_PROJECT,
                              min_interval: int = 900) -> List[str]:
    """Task IDs that have activity AND either no summary yet or new activity since the last
    summary (and enough time has passed to re-run — min_interval seconds)."""
    now = time.time()
    cutoff = now - min_interval
    with _conn(project) as c:
        rows = c.execute(
            """SELECT t.task_id,
                      MAX(a.id) AS max_act,
                      s.activity_cursor,
                      s.generated_at
               FROM tasks t
               JOIN activity a ON a.task_id = t.task_id
               LEFT JOIN task_summaries s ON s.task_id = t.task_id
               GROUP BY t.task_id""",
        ).fetchall()
    result = []
    for row in rows:
        task_id, max_act, cursor, gen_at = row[0], row[1], row[2], row[3]
        no_summary = cursor is None
        new_activity = (not no_summary) and (max_act > cursor)
        interval_ok = gen_at is None or gen_at < cutoff
        if (no_summary or new_activity) and interval_ok:
            result.append(task_id)
    return result


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
    "llm_spend",
    "outcomes",
    "task_claims",
    "file_leases",
    "resource_leases",
    "decisions",
)
AUTOINCREMENT_TASK_TABLES = {"activity", "llm_spend", "decisions"}


def _table_columns(c: sqlite3.Connection, table: str) -> List[str]:
    return [r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]


def _insert_row(c: sqlite3.Connection, table: str, row: Dict[str, Any],
                skip_columns: Optional[set] = None) -> None:
    skip_columns = skip_columns or set()
    cols = [col for col in _table_columns(c, table) if col in row and col not in skip_columns]
    if not cols:
        return
    placeholders = ",".join("?" for _ in cols)
    c.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
        [row[col] for col in cols],
    )


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


def get_project_github_repo(project: str = DEFAULT_PROJECT) -> str:
    """Repository used for PR-state reconciliation on one board.

    Multi-project deployments cannot rely on one global PM_GITHUB_REPO: Helm, Switchboard,
    Vulkan, and Maxwell can all be live at once. Project meta/env wins, then built-in
    dogfood defaults, with the legacy global env kept for the default board and Switchboard.
    """
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


def _validate_github_repo(repo: str) -> Tuple[str, str]:
    clean = (repo or "").strip()
    if clean and not GITHUB_REPO_RE.match(clean):
        return clean, "github repo must be 'owner/name'"
    return clean, ""


def set_project_github_repo(repo: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    repo, error = _validate_github_repo(repo)
    if error:
        return {"error": error, "repo": repo, "project": project}
    set_meta("github_repo", repo, project=project)
    return {"project": project, "github_repo": repo}


def create_project(name: str, project_id: str = "", label: str = "", pretitle: str = "",
                   actor: str = "system", seed_path: str = "",
                   github_repo: str = "") -> Dict[str, Any]:
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
        return {"created": False, "project": {"id": pid, "label": existing["label"],
                "pretitle": existing.get("pretitle", ""), "db": existing["db"],
                "seed": existing.get("seed"),
                "github_repo": get_project_github_repo(pid) or None}}

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
    except Exception:
        with _registry_conn() as c:
            c.execute("DELETE FROM projects WHERE id=?", (pid,))
        raise

    return {"created": True, "project": {"id": pid, "label": project_label,
            "pretitle": project_pretitle, "db": db_path, "seed": seed,
            "github_repo": get_project_github_repo(pid) or None}}


def get_working_agreement(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Canonical connect-time rules for agents in this workspace."""
    override = get_meta("working_agreement", {}, project=project) or {}
    default = {
        "project": project,
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
            pr = _github_pr(pr_repo, int(state.get("pr_number") or 0), token=token)
            if not pr:
                findings.append({"severity": "medium", "task_id": task["task_id"],
                                 "code": "pr_state_unavailable",
                                 "detail": f"Could not fetch recorded PR state from GitHub repo {pr_repo}."})
                continue
            merged = bool(pr.get("merged_at"))
            if task.get("status") == "Done" and not merged:
                findings.append({"severity": "high", "task_id": task["task_id"],
                                 "code": "done_pr_not_merged",
                                 "detail": "Task is Done but the recorded GitHub PR is not merged."})
            merge_sha = pr.get("merge_commit_sha")
            if (merged and merge_sha
                    and task.get("status") in ("In Review", "Done")
                    and (task.get("status") != "Done" or not state.get("merged_sha"))):
                base_ref = ((pr.get("base") or {}).get("ref") or "").strip()
                default_ref = (pr.get("base") or {}).get("repo", {}).get("default_branch") or ""
                if base_ref and default_ref and base_ref == default_ref:
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
    return findings, checks, backfilled


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
                    status == "In Review" or
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
            if status == "In Progress" and not git_state.get("head_sha"):
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
        cursor = c.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0]
    external_findings, external_checks, backfilled = _external_reconcile_findings(
        tasks, git_states, agreement.get("canonical_main_sha") or "", project=project)
    findings.extend(external_findings)
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


def _digest_row(r):
    return {"id": r["id"], "created_at": r["created_at"], "since_ts": r["since_ts"],
            "content": r["content"], "meta": json.loads(r["meta"] or "{}")}


def add_digest(since_ts: float, content: str, meta: Optional[Dict[str, Any]] = None) -> int:
    with _conn() as c:
        cur = c.execute("INSERT INTO digests(created_at, since_ts, content, meta) VALUES (?,?,?,?)",
                        (time.time(), since_ts, content, json.dumps(meta or {})))
        return cur.lastrowid


def last_digest() -> Optional[Dict[str, Any]]:
    with _conn() as c:
        r = c.execute("SELECT * FROM digests ORDER BY id DESC LIMIT 1").fetchone()
        return _digest_row(r) if r else None


def list_digests(limit: int = 20) -> List[Dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM digests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_digest_row(r) for r in rows]


# ---- incremental RAG corpus (Phase 5) — ingested artifacts, persisted + shared --------
def add_rag_chunk(source_kind: str, label: str, text: str, embedding: List[float]):
    with _conn() as c:
        c.execute("INSERT INTO rag_docs(source_kind, label, text, embedding, created_at) VALUES (?,?,?,?,?)",
                  (source_kind, label, text, json.dumps(embedding), time.time()))


def all_rag_chunks() -> List[Dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT label, text, embedding FROM rag_docs ORDER BY id").fetchall()
    return [{"label": r["label"], "text": r["text"], "embedding": json.loads(r["embedding"])} for r in rows]


def rag_docs_max_id() -> int:
    with _conn() as c:
        return c.execute("SELECT COALESCE(MAX(id), 0) FROM rag_docs").fetchone()[0]


def all_rag_rows() -> List[Dict[str, Any]]:
    """rag_docs rows WITH ids — for re-embedding in place (rag.reembed_dynamic)."""
    with _conn() as c:
        rows = c.execute("SELECT id, source_kind, label, text FROM rag_docs ORDER BY id").fetchall()
    return [{"id": r["id"], "source_kind": r["source_kind"], "label": r["label"], "text": r["text"]} for r in rows]


def update_rag_embedding(rag_id: int, embedding: List[float]):
    with _conn() as c:
        c.execute("UPDATE rag_docs SET embedding=? WHERE id=?", (json.dumps(embedding), rag_id))


# ---- Live Inbox queue (Phase 5.5) — triaged inbound artifacts awaiting review ----------
def _inbox_row(r):
    return {"id": r["id"], "source": r["source"], "external_id": r["external_id"],
            "sender": r["sender"], "subject": r["subject"], "summary": r["summary"],
            "triage": json.loads(r["triage"] or "{}"), "status": r["status"],
            "received_at": r["received_at"], "created_at": r["created_at"]}


def inbox_exists(source: str, external_id: str) -> bool:
    if not external_id:
        return False
    with _conn() as c:
        return bool(c.execute("SELECT 1 FROM inbox WHERE source=? AND external_id=?",
                              (source, external_id)).fetchone())


def add_inbox_item(source, external_id, sender, subject, summary, triage, received_at=None) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO inbox(source,external_id,sender,subject,summary,triage,status,received_at,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (source, external_id, sender, subject, summary, json.dumps(triage or {}), "pending",
             received_at or time.time(), time.time()))
        return cur.lastrowid


def list_inbox(status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    with _conn() as c:
        if status:
            rows = c.execute("SELECT * FROM inbox WHERE status=? ORDER BY id DESC LIMIT ?",
                             (status, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM inbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_inbox_row(r) for r in rows]


def get_inbox_item(item_id: int) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        r = c.execute("SELECT * FROM inbox WHERE id=?", (item_id,)).fetchone()
        return _inbox_row(r) if r else None


def set_inbox_status(item_id: int, status: str):
    with _conn() as c:
        c.execute("UPDATE inbox SET status=? WHERE id=?", (status, item_id))


def update_inbox_triage(item_id: int, triage: Dict[str, Any]):
    """Rewrite an item's stored triage JSON — used after a PARTIAL confirm so the proposals
    that were held back (e.g. status->Done awaiting evidence) stay in the queue."""
    with _conn() as c:
        c.execute("UPDATE inbox SET triage=? WHERE id=?", (json.dumps(triage or {}), item_id))


def inbox_pending_count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM inbox WHERE status='pending'").fetchone()[0]


def board_payload(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    tasks = list_tasks(project=project)
    by_ws: Dict[str, Dict[str, Any]] = {}
    for t in tasks:
        ws = by_ws.setdefault(t["_wsId"], {"workstream_id": t["_wsId"], "name": t["_wsName"], "tasks": []})
        ws["tasks"].append(t)
    payload: Dict[str, Any] = {k: get_meta(k, project=project) for k in META_SECTIONS}
    payload["rollups"] = board_rollups(project=project, tasks=tasks)
    payload["workstreams"] = list(by_ws.values())
    return payload
