"""Access, project registry, and principal persistence repository (ARCH-MS-30).

Org/role/project-access helpers were extracted in ARCH-MS-24. This module now
also owns principal/session/password SQL plus ``resolve_write_actor`` and
identity-risk helpers previously planned for ``auth_store.py``. ``store.py``
re-exports the public facade; root ``auth_store.py`` remains a compatibility shim.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from functools import wraps
from typing import Any, Dict, List, Mapping, Optional, Tuple

from constants import *  # noqa: F401,F403
from db.connection import _conn, _project_map, bust_project_cache
from db.core import _json_payload, _registry_conn, coerce_csv_list, hash_token
from db.schema import init_project_registry
from switchboard.contracts.projects.v2 import ProjectRecord, ProjectUpdateCommand
from switchboard.domain.access.identity import (
    binding_for_principal,
    binding_for_registered_agent,
    binding_for_system_actor,
    is_unbound_system_actor,
    shared_token_binding_error,
    validate_system_actor_fields,
)
from switchboard.domain.projects.lifecycle import (
    assert_lifecycle_mutation_allowed,
    default_lifecycle_status,
    lifecycle_write_block,
    normalize_lifecycle_status,
)

__all__ = [
    "normalize_project_id", "project_ids", "has_project", "is_global_project_binding",
    "principal_registry_project", "projects", "role_scopes",
    "principal_scope_definitions", "validate_principal_kind",
    "validate_principal_scopes", "resolve_principal_scopes", "ensure_org",
    "ensure_user", "add_org_member", "set_project_access", "project_access",
    "grant_project_role", "revoke_project_role", "list_project_role_grants",
    "principal_project_grants",
    "principal_project_roles", "effective_principal_scopes", "project_access_model",
    "ensure_bootstrap_project_owner",
    "get_project_record", "list_registry_projects", "update_project_metadata",
    "project_write_block",
    "create_principal", "public_principal_record", "list_principals",
    "get_principal_by_id", "get_principal_by_token", "get_principal_by_token_any_project",
    "password_login_count", "set_principal_password", "get_password_login",
    "create_password_principal", "create_auth_session", "get_principal_by_session",
    "get_principal_by_session_any_project", "revoke_auth_session",
    "revoke_principal_sessions", "revoke_principal", "revoke_principal_token",
    "resolve_write_actor", "IDENTITY_RISK_WINDOW_S",
    "_principal_from_row", "_identity_risk_window_s", "_task_identity_state_in",
    "_identity_takeover_risk_in",
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


def _row_lifecycle_status(row: Dict[str, Any]) -> str:
    return normalize_lifecycle_status(row.get("lifecycle_status")) or default_lifecycle_status()


def _is_active_record(row: Dict[str, Any]) -> bool:
    return _row_lifecycle_status(row) == "active"


def _dynamic_project_row(project_id: str) -> Optional[Dict[str, Any]]:
    init_project_registry()
    with _registry_conn() as c:
        row = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    return dict(row) if row else None


def get_project_record(project_id: str) -> Dict[str, Any]:
    """Return the unified ``switchboard.project.v2`` projection for one project id."""
    pid = (project_id or "").strip()
    if not pid:
        return {"error": "project_id required"}
    row = _dynamic_project_row(pid)
    if not row:
        return {"error": f"unknown project: {pid}"}
    access = project_access(pid)
    merged = {
        "id": row["id"],
        "label": row["label"],
        "pretitle": row.get("pretitle") or "",
        "db_path": row.get("db_path"),
        "seed_path": row.get("seed_path"),
        "created_at": row.get("created_at"),
        "created_by": row.get("created_by"),
        "updated_at": row.get("updated_at") or access.get("updated_at"),
        "updated_by": row.get("updated_by") or access.get("updated_by"),
        "org_id": access.get("org_id") or "",
        "owner_user_id": access.get("owner_user_id") or "",
        "purpose": access.get("purpose") or "",
        "boundary": access.get("boundary") or "",
        "visibility": access.get("visibility"),
        "lifecycle_status": _row_lifecycle_status(row),
        "archived_at": row.get("archived_at"),
        "archived_by": row.get("archived_by"),
        "archive_reason": row.get("archive_reason"),
        "purged_at": row.get("purged_at"),
        "purge_intent_id": row.get("purge_intent_id"),
        "is_protected": bool(row.get("is_protected")),
        "is_system": bool(row.get("is_system")),
        "replacement_project_id": row.get("replacement_project_id"),
        "replacement_board_id": row.get("replacement_board_id"),
        "replacement_mission_id": row.get("replacement_mission_id"),
        "replacement_deliverable_id": row.get("replacement_deliverable_id"),
        "replacement_consolidation_id": row.get("replacement_consolidation_id"),
        # Compatibility field for older consumers.  Lifecycle behavior is driven
        # exclusively by the registry flags, never by this label or an id check.
        "is_builtin": bool(row.get("is_system")),
    }
    return ProjectRecord.from_mapping(merged).model_dump(by_alias=True)


def list_registry_projects(*, include_archived: bool = True) -> List[Dict[str, Any]]:
    """Return full registry projections for every known project id."""
    records = []
    for pid in sorted(_project_map()):
        record = get_project_record(pid)
        if record.get("error"):
            continue
        if include_archived or record.get("lifecycle_status") == "active":
            records.append(record)
    return records


def project_write_block(project_id: str, operation: str = "write") -> Optional[Dict[str, Any]]:
    """Shared registry-side guard for writes that do not touch the board database."""
    record = get_project_record(project_id)
    if record.get("error"):
        return record
    return lifecycle_write_block(
        project_id, str(record.get("lifecycle_status") or "active"), operation)


def _guard_project_write(operation: str):
    """Wrap legacy registry entry points without rewriting their extracted bodies."""
    def decorate(function):
        @wraps(function)
        def guarded(project_id, *args, **kwargs):
            blocked = project_write_block(project_id, operation)
            if blocked:
                return blocked
            return function(project_id, *args, **kwargs)
        return guarded
    return decorate


def transition_project_lifecycle(project_id: str, requested: str, *, actor: str,
                                 reason: str, impact_report_hash: str = "",
                                 validation: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Atomically persist one lifecycle transition and its registry audit event."""
    pid = str(project_id or "").strip()
    actor_name = str(actor or "").strip()
    reason_text = str(reason or "").strip()
    if not pid or not actor_name or not reason_text:
        return {"error": "project_id, actor, and reason are required"}
    current = get_project_record(pid)
    if current.get("error"):
        return current
    requested_status = normalize_lifecycle_status(requested)
    err = assert_lifecycle_mutation_allowed(current, requested_status)
    if err:
        return err
    prior = str(current.get("lifecycle_status") or "active")
    if prior == requested_status:
        return {
            "project": current,
            "transitioned": False,
            "idempotent": True,
            "from_status": prior,
            "to_status": requested_status,
        }
    now = time.time()
    event_id = f"project-lifecycle-{uuid.uuid4().hex[:16]}"
    validation_json = json.dumps(dict(validation or {}), sort_keys=True)
    init_project_registry()
    with _registry_conn() as c:
        if requested_status == "archived":
            c.execute(
                "UPDATE projects SET lifecycle_status=?, archived_at=?, archived_by=?, "
                "archive_reason=?, updated_at=?, updated_by=? WHERE id=?",
                ("archived", now, actor_name, reason_text, now, actor_name, pid),
            )
        elif requested_status == "active":
            c.execute(
                "UPDATE projects SET lifecycle_status=?, archived_at=NULL, archived_by=NULL, "
                "archive_reason=NULL, updated_at=?, updated_by=? WHERE id=?",
                ("active", now, actor_name, pid),
            )
        else:
            c.execute(
                "UPDATE projects SET lifecycle_status='purged', purged_at=?, "
                "purge_intent_id=?, updated_at=?, updated_by=? WHERE id=?",
                (now, str((validation or {}).get("purge_intent_id") or "") or None,
                 now, actor_name, pid),
            )
        c.execute(
            "INSERT INTO project_lifecycle_events(event_id, project_id, from_status, to_status, "
            "actor, reason, impact_report_hash, validation_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (event_id, pid, prior, requested_status, actor_name, reason_text,
             impact_report_hash or None, validation_json, now),
        )
    bust_project_cache()
    return {
        "project": get_project_record(pid),
        "transitioned": True,
        "idempotent": False,
        "event_id": event_id,
        "from_status": prior,
        "to_status": requested_status,
        "created_at": now,
    }


def list_project_lifecycle_events(project_id: str) -> List[Dict[str, Any]]:
    init_project_registry()
    with _registry_conn() as c:
        rows = c.execute(
            "SELECT * FROM project_lifecycle_events WHERE project_id=? "
            "ORDER BY created_at, event_id", (project_id,),
        ).fetchall()
    events = []
    for row in rows:
        item = dict(row)
        item["validation"] = json.loads(item.pop("validation_json") or "{}")
        events.append(item)
    return events


def update_project_metadata(command: Mapping[str, Any] | ProjectUpdateCommand,
                            actor: str = "system") -> Dict[str, Any]:
    """Apply editable metadata and optional lifecycle transitions."""
    try:
        cmd = (command if isinstance(command, ProjectUpdateCommand)
               else ProjectUpdateCommand.from_mapping(command))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"invalid project update command: {exc}"}

    pid = cmd.project_id
    if not has_project(pid):
        return {"error": f"unknown project: {pid}"}

    current = get_project_record(pid)
    if current.get("error"):
        return current

    fields = cmd.editable_fields()
    if not fields:
        return {"error": "no editable fields supplied"}

    if cmd.lifecycle_status is not None and cmd.lifecycle_status != current.get("lifecycle_status"):
        return {
            "error": "lifecycle_command_required",
            "message": "use archive_project or restore_project for lifecycle transitions",
            "project_id": pid,
            "requested": cmd.lifecycle_status,
        }
    blocked = project_write_block(pid, "update_project_metadata")
    if blocked:
        return blocked

    row = _dynamic_project_row(pid)
    if not row:
        return {"error": f"unknown project: {pid}"}

    now = time.time()
    project_sets: List[str] = []
    project_vals: List[Any] = []
    for key in (
            "label", "pretitle", "replacement_project_id", "replacement_board_id",
            "replacement_mission_id", "replacement_deliverable_id",
            "replacement_consolidation_id"):
        if key in fields:
            project_sets.append(f"{key}=?")
            project_vals.append(fields[key])

    if cmd.lifecycle_status is not None:
        project_sets.append("lifecycle_status=?")
        project_vals.append(cmd.lifecycle_status)
        if cmd.lifecycle_status == "archived":
            project_sets.extend(["archived_at=?", "archived_by=?", "archive_reason=?"])
            project_vals.extend([now, actor, fields.get("archive_reason")])
        elif cmd.lifecycle_status == "active":
            project_sets.extend(["archived_at=?", "archived_by=?", "archive_reason=?"])
            project_vals.extend([None, None, None])

    project_sets.extend(["updated_at=?", "updated_by=?"])
    project_vals.extend([now, cmd.updated_by or actor])

    access_fields = {k: fields[k] for k in ("org_id", "owner_user_id", "purpose",
                                           "boundary", "visibility") if k in fields}
    init_project_registry()
    with _registry_conn() as c:
        if project_sets:
            c.execute(
                f"UPDATE projects SET {', '.join(project_sets)} WHERE id=?",
                (*project_vals, pid),
            )
        if access_fields:
            vis = access_fields.get("visibility")
            if vis is not None and vis not in ("private", "org", ""):
                return {"error": "visibility must be 'private' or 'org'"}
            c.execute(
                "INSERT INTO project_access(project_id, org_id, owner_user_id, purpose, "
                "boundary, created_at, created_by, updated_at, visibility, updated_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(project_id) DO UPDATE SET "
                "org_id=COALESCE(excluded.org_id, project_access.org_id), "
                "owner_user_id=COALESCE(excluded.owner_user_id, project_access.owner_user_id), "
                "purpose=COALESCE(excluded.purpose, project_access.purpose), "
                "boundary=COALESCE(excluded.boundary, project_access.boundary), "
                "updated_at=excluded.updated_at, "
                "visibility=COALESCE(excluded.visibility, project_access.visibility), "
                "updated_by=excluded.updated_by",
                (pid,
                 access_fields.get("org_id") or current.get("org_id") or DEFAULT_ORG_ID,
                 access_fields.get("owner_user_id", current.get("owner_user_id")) or None,
                 access_fields.get("purpose", current.get("purpose")) or None,
                 access_fields.get("boundary", current.get("boundary")) or None,
                 row.get("created_at") or now,
                 row.get("created_by") or actor,
                 now,
                 (vis or None) if vis is not None else current.get("visibility"),
                 cmd.updated_by or actor),
            )

    bust_project_cache()
    return get_project_record(pid)


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
        record = get_project_record(k)
        if record.get("error"):
            continue
        if allowed is not None and record.get("is_system") and k not in allowed:
            continue
        if record.get("lifecycle_status") != "active":
            continue
        out.append({
            "id": k,
            "label": record.get("label") or v["label"],
            "pretitle": record.get("pretitle") or v.get("pretitle", ""),
            "purpose": record.get("purpose") or "",
            "boundary": record.get("boundary") or "",
            "owner_user_id": record.get("owner_user_id") or "",
            "org_id": record.get("org_id") or "",
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
    """Ensure a global ``users`` row exists — Auth is the exclusive writer (ARCH-MS-83).

    ``created_by`` is retained for call-site compatibility; identity upserts are
    owned by ``switchboard.api.routers.auth.store.ensure_identity``.
    """
    del created_by  # Auth identity rows do not persist created_by today.
    from switchboard.api.auth_port_adapters import configure_auth_ports
    from switchboard.api.routers.auth import store as auth_store

    configure_auth_ports()
    return auth_store.ensure_identity(user_id, email=email, display_name=display_name)


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
    blocked = project_write_block(project_id, "set_project_access")
    if blocked:
        return blocked
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
    bust_project_cache()
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


def principal_project_grants(principal_id: str,
                             include_revoked: bool = False) -> List[Dict[str, Any]]:
    """Return one principal's grants across projects in a single registry read.

    MCP project discovery uses this instead of opening every project database or
    issuing one registry query per project.
    """
    principal_id = (principal_id or "").strip()
    if not principal_id:
        return []
    init_project_registry()
    query = (
        "SELECT * FROM project_role_grants WHERE subject_id=? "
        "AND subject_kind IN ('principal','user')")
    params: List[Any] = [principal_id]
    if not include_revoked:
        query += " AND revoked_at IS NULL"
    query += " ORDER BY project_id, subject_kind, role"
    with _registry_conn() as c:
        rows = c.execute(query, params).fetchall()
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


grant_project_role = _guard_project_write("grant_project_role")(grant_project_role)
revoke_project_role = _guard_project_write("revoke_project_role")(revoke_project_role)
ensure_bootstrap_project_owner = _guard_project_write(
    "ensure_bootstrap_project_owner")(ensure_bootstrap_project_owner)


# --- Principal / session / identity (ARCH-MS-30 auth_store move) ---

def _store_facade():
    """Resolve transitional store helpers after store.py is initialized."""
    import store

    return store


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
    binding = (project or DEFAULT_PROJECT).strip()
    registry = principal_registry_project(binding)
    principal_id = principal_id or f"{kind}-{uuid.uuid4().hex[:12]}"
    display_name = (display_name or principal_id).strip()
    now = time.time()
    scopes_json = json.dumps(scopes, sort_keys=True)
    with _conn(registry) as c:
        c.execute(
            "INSERT INTO principals(id, kind, display_name, project, scopes, token_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (principal_id, kind, display_name, binding, scopes_json, hash_token(token), now),
        )
    return {"id": principal_id, "kind": kind, "display_name": display_name,
            "project": binding, "scopes": scopes, "created_at": now}


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


def get_principal_by_token_any_project(token: str) -> Optional[Dict[str, Any]]:
    """Find a bearer principal by token hash across all project DBs."""
    if not token:
        return None
    token_hash = hash_token(token)
    for project_id in project_ids():
        with _conn(project_id) as c:
            row = c.execute("SELECT * FROM principals WHERE token_hash=?",
                            (token_hash,)).fetchone()
        if row:
            return _principal_from_row(row)
    return None


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
    _store_facade().append_activity(
        "access.token_revoked",
        actor,
        {"principal": public, "sessions_revoked": session_count,
         "already_revoked": already_revoked},
        task_id=None,
        project=project,
    )
    return {"revoked": bool(revoked), "already_revoked": already_revoked,
            "sessions_revoked": session_count, "principal": public}

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
    if not is_unbound_system_actor(actor):
        return binding_for_principal(actor, principal_id=principal_id)

    base_error = shared_token_binding_error(
        actor=actor, principal_id=principal_id, task_id=task_id)

    if system_actor:
        validation_error = validate_system_actor_fields(
            system_actor, system_reason,
            principal_actor=actor, principal_id=principal_id, task_id=task_id)
        if validation_error:
            return validation_error
        return binding_for_system_actor(
            principal_actor=actor,
            principal_id=principal_id,
            system_actor=system_actor,
            system_reason=system_reason,
        )

    with _conn(project) as c:
        if agent_id:
            presence = _store_facade()._active_agent_presence_in(c, agent_id, now)
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
            return binding_for_registered_agent(
                agent_id=agent_id,
                principal_actor=actor,
                principal_id=principal_id,
                binding="registered_agent",
            )
        if task_id:
            active_agents = _store_facade()._active_agent_ids_for_task(c, task_id, now)
            if len(active_agents) == 1:
                return binding_for_registered_agent(
                    agent_id=active_agents[0],
                    principal_actor=actor,
                    principal_id=principal_id,
                    binding="inferred_registered_agent",
                )
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
    active_agents = _store_facade()._active_agent_ids_for_task(c, task_id, now)
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

    def get_project_record(self, project: str) -> Dict[str, Any]:
        return get_project_record(project)

    def list_registry_projects(self, *, include_archived: bool = True) -> List[Dict[str, Any]]:
        return list_registry_projects(include_archived=include_archived)

    def update_project_metadata(self, command: Mapping[str, Any] | ProjectUpdateCommand,
                                actor: str = "system") -> Dict[str, Any]:
        return update_project_metadata(command, actor=actor)

    def transition_project_lifecycle(self, project_id: str, requested: str, *, actor: str,
                                     reason: str, impact_report_hash: str = "",
                                     validation: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        return transition_project_lifecycle(
            project_id, requested, actor=actor, reason=reason,
            impact_report_hash=impact_report_hash, validation=validation)

    def list_project_lifecycle_events(self, project_id: str) -> List[Dict[str, Any]]:
        return list_project_lifecycle_events(project_id)


def default_access_repository() -> AccessStoreRepository:
    """Canonical Phase-1A access repository (registry SQL in this module)."""
    return AccessStoreRepository()
