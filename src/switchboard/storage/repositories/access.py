"""Project registry, organization, and role-grant persistence repository.

Extracted verbatim from ``store.py`` for the ARCH-MS-24 Phase 0 exit proof.
The monolith continues to re-export this public facade, so callers keep the
same API while project/access persistence has an explicit ownership boundary.
"""
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from constants import *  # noqa: F401,F403
from db.connection import _project_map
from db.core import _registry_conn, coerce_csv_list
from db.schema import init_project_registry

__all__ = [
    "normalize_project_id", "project_ids", "has_project", "is_global_project_binding",
    "principal_registry_project", "projects", "role_scopes",
    "principal_scope_definitions", "validate_principal_kind",
    "validate_principal_scopes", "resolve_principal_scopes", "ensure_org",
    "ensure_user", "add_org_member", "set_project_access", "project_access",
    "grant_project_role", "revoke_project_role", "list_project_role_grants",
    "principal_project_roles", "effective_principal_scopes", "project_access_model",
    "ensure_bootstrap_project_owner",
    "AccessStoreRepository", "default_access_repository",
]


def normalize_project_id(value: str) -> str:
    """Turn a human project name like 'Vulkan Renderer' into a stable project id."""
    slug = PROJECT_ID_SLUG_RE.sub("-", (value or "").strip().lower()).strip("-_")
    slug = re.sub(r"[-_]{2,}", "-", slug)
    return slug


def project_ids() -> List[str]:
    return list(_project_map())


def has_project(project: Optional[str]) -> bool:
    return (project or DEFAULT_PROJECT) in _project_map()


def is_global_project_binding(project: Optional[str]) -> bool:
    return (project or "").strip() == "*"


def principal_registry_project(project: Optional[str]) -> str:
    """SQLite file that stores a principal record for the given binding."""
    return "switchboard" if is_global_project_binding(project) else (project or DEFAULT_PROJECT)


def projects() -> List[Dict[str, Any]]:
    """The switcher's source of truth — [{id, label, pretitle}].

    ``PM_TOP_LEVEL_PROJECTS`` is a legacy deployment selector for the static
    built-in homes.  Dynamic projects are created at runtime, so their ids cannot
    be present in a process environment that was fixed at boot.  Filtering those
    projects here made ``create_project`` report success while hiding the result
    from MCP discovery, authenticated sessions, and the UI project picker.

    Dynamic visibility is enforced by ``project_access`` plus the caller's grants;
    this registry projection must keep every dynamic project available for that
    access-resolution step.
    """
    visible = (os.environ.get("PM_TOP_LEVEL_PROJECTS") or "").strip()
    allowed = {p.strip() for p in visible.split(",") if p.strip()} if visible else None
    out = []
    for k, v in _project_map().items():
        if allowed is not None and k in BUILTIN_PROJECTS and k not in allowed:
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


def revoke_project_role(project_id: str, subject_kind: str, subject_id: str,
                        role: str, created_by: str = "system") -> Dict[str, Any]:
    """Revoke one project role grant (UI-5 members management).

    Idempotent: revoking an absent/already-revoked grant returns revoked=False with a
    note rather than raising. Owner access lives in project_access.owner_user_id, not in
    grants, so this can never strip a project's owner.
    """
    init_project_registry()
    if not has_project(project_id):
        return {"error": f"unknown project: {project_id}"}
    subject_kind = (subject_kind or "").strip().lower()
    subject_id = (subject_id or "").strip()
    role = (role or "").strip().lower()
    if not subject_id or not role:
        return {"error": "subject_id and role are required"}
    now = time.time()
    with _registry_conn() as c:
        cur = c.execute(
            "UPDATE project_role_grants SET revoked_at=? WHERE project_id=? AND "
            "subject_kind=? AND subject_id=? AND role=? AND revoked_at IS NULL",
            (now, project_id, subject_kind, subject_id, role),
        )
        revoked = cur.rowcount > 0
    result = {"project_id": project_id, "subject_kind": subject_kind,
              "subject_id": subject_id, "role": role, "revoked": revoked}
    if revoked:
        result["revoked_at"] = now
    else:
        result["note"] = "no active grant to revoke"
    return result


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


class AccessStoreRepository:
    """Registry-backed :class:`~switchboard.storage.repositories.protocols.AccessRepository`.

    Wraps the module-level SQL helpers already extracted from ``store.py`` so
    application code can depend on the Protocol without importing SQLite details.
    """

    def normalize_project_id(self, value: str) -> str:
        return normalize_project_id(value)

    def has_project(self, project: Optional[str]) -> bool:
        return has_project(project)

    def projects(self) -> List[Dict[str, Any]]:
        return projects()

    def project_access(self, project: str) -> Dict[str, Any]:
        return project_access(project)


def default_access_repository() -> AccessStoreRepository:
    """Canonical Phase-1A access repository (registry SQL in this module)."""
    return AccessStoreRepository()
