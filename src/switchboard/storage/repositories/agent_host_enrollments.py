"""Personal Agent Host enrollment and rotatable host-identity persistence.

The bootstrap code and host bearer are returned once and stored only as hashes.
Provider credentials never enter this surface: enrollment grants only the narrow
``read`` and ``write:ixp`` scopes needed by an Agent Host.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
import uuid
from typing import Any, Iterable

from constants import DEFAULT_PROJECT
from db.connection import _conn
from db.core import hash_token


ENROLLMENT_SCHEMA = "switchboard.agent_host_enrollment.v1"
_ACTIVE = "active"
_FINGERPRINT_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")


def _json_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _normalized_list(values: Iterable[Any] | None) -> list[str]:
    return sorted({str(value).strip() for value in (values or []) if str(value).strip()})


def _public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value.pop("bootstrap_hash", None)
    for key in ("tenant_allowlist", "project_allowlist", "provider_allowlist"):
        value[key] = _json_list(value.pop(f"{key}_json", "[]"))
    value["schema"] = ENROLLMENT_SCHEMA
    return value


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"error": code, "error_code": code, "message": message, **details}


def _fingerprint(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _FINGERPRINT_RE.fullmatch(normalized):
        return ""
    return normalized if normalized.startswith("sha256:") else f"sha256:{normalized}"


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return result[:48] or "personal-agent-host"


def begin_agent_host_enrollment(
    *,
    owner_user_id: str,
    requested_host_id: str = "",
    tenant_allowlist: Iterable[Any] | None = None,
    project_allowlist: Iterable[Any] | None = None,
    provider_allowlist: Iterable[Any] | None = None,
    package_version: str = "",
    ttl_seconds: int = 600,
    created_by_principal_id: str = "",
    actor: str = "system",
    project: str = DEFAULT_PROJECT,
) -> dict[str, Any]:
    """Issue one short-lived, single-use bootstrap code for a user-owned host."""
    owner_user_id = str(owner_user_id or "").strip()
    requested_host_id = str(requested_host_id or "").strip()
    if not owner_user_id:
        return _error("owner_user_id_required", "owner_user_id is required")
    if requested_host_id and not requested_host_id.startswith("host/"):
        return _error("invalid_host_id", "requested_host_id must start with host/")
    try:
        ttl_seconds = min(900, max(60, int(ttl_seconds)))
    except (TypeError, ValueError):
        return _error("invalid_ttl", "ttl_seconds must be an integer")

    now = time.time()
    bootstrap_code = "ahb-" + secrets.token_urlsafe(24)
    enrollment_id = "hostenroll-" + uuid.uuid4().hex[:16]
    projects = _normalized_list(project_allowlist) or [project]
    with _conn(project) as connection:
        connection.execute(
            "INSERT INTO agent_host_enrollments("
            "enrollment_id, project_id, requested_host_id, owner_user_id, "
            "tenant_allowlist_json, project_allowlist_json, provider_allowlist_json, "
            "bootstrap_hash, bootstrap_expires_at, package_version, status, "
            "created_by_principal_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                enrollment_id,
                project,
                requested_host_id or None,
                owner_user_id,
                json.dumps(_normalized_list(tenant_allowlist), sort_keys=True),
                json.dumps(projects, sort_keys=True),
                json.dumps(_normalized_list(provider_allowlist), sort_keys=True),
                hash_token(bootstrap_code),
                now + ttl_seconds,
                str(package_version or "").strip() or None,
                "pending",
                created_by_principal_id or None,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (
                None,
                actor,
                "agent_host.enrollment_started",
                json.dumps({
                    "enrollment_id": enrollment_id,
                    "owner_user_id": owner_user_id,
                    "requested_host_id": requested_host_id or None,
                    "bootstrap_expires_at": now + ttl_seconds,
                }, sort_keys=True),
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM agent_host_enrollments WHERE enrollment_id=?", (enrollment_id,)
        ).fetchone()
    return {
        "created": True,
        "bootstrap_code": bootstrap_code,
        "bootstrap_code_returned_once": True,
        "enrollment": _public(row),
    }


def complete_agent_host_enrollment(
    *,
    bootstrap_code: str,
    hostname: str,
    platform: str,
    public_key_fingerprint: str,
    agent_host_version: str = "",
    actor: str = "agent-host-bootstrap",
    project: str = DEFAULT_PROJECT,
) -> dict[str, Any]:
    """Consume a bootstrap code and return a narrow host bearer exactly once."""
    bootstrap_code = str(bootstrap_code or "").strip()
    fingerprint = _fingerprint(public_key_fingerprint)
    if not bootstrap_code:
        return _error("bootstrap_code_required", "bootstrap_code is required")
    if not fingerprint:
        return _error(
            "invalid_public_key_fingerprint",
            "public_key_fingerprint must be a SHA-256 fingerprint",
        )
    platform = str(platform or "").strip().lower()
    if platform not in {"darwin", "linux"}:
        return _error("unsupported_platform", "platform must be darwin or linux")

    now = time.time()
    token = "aht-" + secrets.token_urlsafe(32)
    bootstrap_digest = hash_token(bootstrap_code)
    with _conn(project) as connection:
        row = connection.execute(
            "SELECT * FROM agent_host_enrollments WHERE bootstrap_hash=?",
            (bootstrap_digest,),
        ).fetchone()
        if not row:
            return _error("invalid_bootstrap_code", "bootstrap code is invalid")
        current = dict(row)
        if current.get("status") != "pending" or current.get("bootstrap_consumed_at"):
            return _error("bootstrap_code_consumed", "bootstrap code has already been consumed")
        if float(current.get("bootstrap_expires_at") or 0) <= now:
            connection.execute(
                "UPDATE agent_host_enrollments SET status='expired', updated_at=? "
                "WHERE enrollment_id=?",
                (now, current["enrollment_id"]),
            )
            return _error("bootstrap_code_expired", "bootstrap code has expired")

        host_id = str(current.get("requested_host_id") or "").strip()
        if not host_id:
            host_id = f"host/{_slug(hostname)}-{current['enrollment_id'][-6:]}"
        principal_id = f"host-{current['enrollment_id'][-16:]}"
        inserted_principal = False
        try:
            connection.execute(
                "INSERT INTO principals(id, kind, display_name, project, scopes, token_hash, "
                "created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    principal_id,
                    "host",
                    host_id,
                    project,
                    json.dumps(["read", "write:ixp"], sort_keys=True),
                    hash_token(token),
                    now,
                ),
            )
            inserted_principal = True
            changed = connection.execute(
                "UPDATE agent_host_enrollments SET host_id=?, principal_id=?, "
                "public_key_fingerprint=?, identity_generation=1, platform=?, hostname=?, "
                "package_version=COALESCE(NULLIF(?, ''), package_version), status='active', "
                "bootstrap_consumed_at=?, updated_at=? "
                "WHERE enrollment_id=? AND status='pending' AND bootstrap_consumed_at IS NULL",
                (
                    host_id,
                    principal_id,
                    fingerprint,
                    platform,
                    str(hostname or "").strip(),
                    str(agent_host_version or "").strip(),
                    now,
                    now,
                    current["enrollment_id"],
                ),
            )
            if changed.rowcount != 1:
                connection.execute(
                    "DELETE FROM principals WHERE id=? AND token_hash=?",
                    (principal_id, hash_token(token)),
                )
                inserted_principal = False
                raise sqlite3.IntegrityError("bootstrap code was consumed concurrently")
        except sqlite3.IntegrityError:
            # If this attempt inserted the principal but could not consume the
            # bootstrap, remove only its exact token row. A competing attempt
            # that won the principal insert is never touched.
            if inserted_principal:
                connection.execute(
                    "DELETE FROM principals WHERE id=? AND token_hash=?",
                    (principal_id, hash_token(token)),
                )
            return _error("enrollment_conflict", "host enrollment could not be completed")
        connection.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (
                None,
                actor,
                "agent_host.enrolled",
                json.dumps({
                    "enrollment_id": current["enrollment_id"],
                    "host_id": host_id,
                    "principal_id": principal_id,
                    "platform": platform,
                    "identity_generation": 1,
                    "credential_values_redacted": True,
                }, sort_keys=True),
                now,
            ),
        )
        completed = connection.execute(
            "SELECT * FROM agent_host_enrollments WHERE enrollment_id=?",
            (current["enrollment_id"],),
        ).fetchone()
    return {
        "completed": True,
        "host_token": token,
        "host_token_returned_once": True,
        "enrollment": _public(completed),
    }


def get_agent_host_enrollment(host_id: str, project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    with _conn(project) as connection:
        row = connection.execute(
            "SELECT * FROM agent_host_enrollments WHERE project_id=? AND host_id=?",
            (project, str(host_id or "").strip()),
        ).fetchone()
    return _public(row) if row else _error("enrollment_not_found", "host enrollment not found")


def list_agent_host_enrollments(
    *, status: str = "", project: str = DEFAULT_PROJECT
) -> list[dict[str, Any]]:
    query = "SELECT * FROM agent_host_enrollments"
    args: list[Any] = []
    if status:
        query += " WHERE status=?"
        args.append(str(status).strip().lower())
    query += " ORDER BY created_at DESC"
    with _conn(project) as connection:
        rows = connection.execute(query, args).fetchall()
    return [_public(row) for row in rows]


def rotate_agent_host_identity(
    *,
    host_id: str,
    principal_id: str,
    public_key_fingerprint: str,
    actor: str = "agent-host",
    project: str = DEFAULT_PROJECT,
) -> dict[str, Any]:
    """Rotate both bearer material and public-key fingerprint for an active host."""
    fingerprint = _fingerprint(public_key_fingerprint)
    if not fingerprint:
        return _error(
            "invalid_public_key_fingerprint",
            "public_key_fingerprint must be a SHA-256 fingerprint",
        )
    now = time.time()
    token = "aht-" + secrets.token_urlsafe(32)
    with _conn(project) as connection:
        row = connection.execute(
            "SELECT * FROM agent_host_enrollments WHERE project_id=? AND host_id=?",
            (project, str(host_id or "").strip()),
        ).fetchone()
        if not row:
            return _error("enrollment_not_found", "host enrollment not found")
        enrollment = dict(row)
        if enrollment.get("status") != _ACTIVE:
            return _error("host_identity_revoked", "host identity is not active")
        if enrollment.get("principal_id") != str(principal_id or "").strip():
            return _error("host_identity_mismatch", "host principal does not own this enrollment")
        principal_update = connection.execute(
            "UPDATE principals SET token_hash=? WHERE id=? AND revoked_at IS NULL",
            (hash_token(token), principal_id),
        )
        if principal_update.rowcount != 1:
            return _error("host_identity_revoked", "host principal is not active")
        connection.execute(
            "UPDATE agent_host_enrollments SET public_key_fingerprint=?, "
            "identity_generation=identity_generation+1, updated_at=? WHERE enrollment_id=?",
            (fingerprint, now, enrollment["enrollment_id"]),
        )
        connection.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (
                None,
                actor,
                "agent_host.identity_rotated",
                json.dumps({
                    "host_id": host_id,
                    "principal_id": principal_id,
                    "identity_generation": int(enrollment.get("identity_generation") or 0) + 1,
                    "credential_values_redacted": True,
                }, sort_keys=True),
                now,
            ),
        )
        updated = connection.execute(
            "SELECT * FROM agent_host_enrollments WHERE enrollment_id=?",
            (enrollment["enrollment_id"],),
        ).fetchone()
    return {
        "rotated": True,
        "host_token": token,
        "host_token_returned_once": True,
        "enrollment": _public(updated),
    }


def revoke_agent_host_identity(
    *,
    host_id: str,
    actor: str = "system",
    reason: str = "operator_revoke",
    final_status: str = "revoked",
    project: str = DEFAULT_PROJECT,
) -> dict[str, Any]:
    """Revoke the bearer and fence future registration/heartbeat for the host id."""
    final_status = str(final_status or "revoked").strip().lower()
    if final_status not in {"revoked", "uninstalled"}:
        return _error("invalid_final_status", "final_status must be revoked or uninstalled")
    now = time.time()
    with _conn(project) as connection:
        row = connection.execute(
            "SELECT * FROM agent_host_enrollments WHERE project_id=? AND host_id=?",
            (project, str(host_id or "").strip()),
        ).fetchone()
        if not row:
            return _error("enrollment_not_found", "host enrollment not found")
        enrollment = dict(row)
        principal_id = str(enrollment.get("principal_id") or "")
        connection.execute(
            "UPDATE principals SET revoked_at=COALESCE(revoked_at, ?) WHERE id=?",
            (now, principal_id),
        )
        connection.execute(
            "UPDATE agent_host_enrollments SET status=?, revoked_at=COALESCE(revoked_at, ?), "
            "updated_at=? WHERE enrollment_id=?",
            (final_status, now, now, enrollment["enrollment_id"]),
        )
        connection.execute(
            "UPDATE agent_hosts SET status='revoked', last_error=? WHERE host_id=?",
            (reason, host_id),
        )
        connection.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (
                None,
                actor,
                "agent_host.identity_revoked" if final_status == "revoked" else "agent_host.uninstalled",
                json.dumps({
                    "host_id": host_id,
                    "principal_id": principal_id,
                    "reason": str(reason or "").strip(),
                    "post_revoke_denial": True,
                }, sort_keys=True),
                now,
            ),
        )
        updated = connection.execute(
            "SELECT * FROM agent_host_enrollments WHERE enrollment_id=?",
            (enrollment["enrollment_id"],),
        ).fetchone()
    return {"revoked": True, "enrollment": _public(updated)}


def check_agent_host_identity(
    host_id: str, principal_id: str, project: str = DEFAULT_PROJECT
) -> dict[str, Any]:
    """Fence enrolled host ids to their active, exact principal identity."""
    with _conn(project) as connection:
        row = connection.execute(
            "SELECT status, principal_id, identity_generation, public_key_fingerprint "
            "FROM agent_host_enrollments WHERE project_id=? AND host_id=?",
            (project, str(host_id or "").strip()),
        ).fetchone()
    if not row:
        return {"required": False, "allowed": True}
    identity = dict(row)
    if identity.get("status") != _ACTIVE:
        return _error(
            "host_identity_revoked",
            "host identity is not active",
            required=True,
            allowed=False,
            status=identity.get("status"),
        )
    if identity.get("principal_id") != str(principal_id or "").strip():
        return _error(
            "host_identity_mismatch",
            "host id is bound to a different principal",
            required=True,
            allowed=False,
        )
    return {
        "required": True,
        "allowed": True,
        "identity_generation": identity.get("identity_generation"),
        "public_key_fingerprint": identity.get("public_key_fingerprint"),
    }


class AgentHostEnrollmentRepository:
    begin = staticmethod(begin_agent_host_enrollment)
    complete = staticmethod(complete_agent_host_enrollment)
    get = staticmethod(get_agent_host_enrollment)
    list = staticmethod(list_agent_host_enrollments)
    rotate = staticmethod(rotate_agent_host_identity)
    revoke = staticmethod(revoke_agent_host_identity)
    check_identity = staticmethod(check_agent_host_identity)


_DEFAULT_REPOSITORY = AgentHostEnrollmentRepository()


def default_agent_host_enrollment_repository() -> AgentHostEnrollmentRepository:
    return _DEFAULT_REPOSITORY


__all__ = [
    "ENROLLMENT_SCHEMA",
    "begin_agent_host_enrollment",
    "complete_agent_host_enrollment",
    "get_agent_host_enrollment",
    "list_agent_host_enrollments",
    "rotate_agent_host_identity",
    "revoke_agent_host_identity",
    "check_agent_host_identity",
    "AgentHostEnrollmentRepository",
    "default_agent_host_enrollment_repository",
]
