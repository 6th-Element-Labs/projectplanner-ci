"""Server-authoritative project grants for account/org-owned Agent Hosts.

The grant is placement authorization only.  Enrollment remains the Host identity
authority and ``agent_hosts`` remains the Capacity/liveness authority.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from constants import (
    PROJECT_EXECUTION_ISOLATION_MODES,
    PROJECT_EXECUTION_RUNTIMES,
    PROJECT_EXECUTION_TRUST_ZONES,
)
from db.connection import _conn
from db.core import _registry_conn
from db.schema import init_project_registry
from switchboard.storage.repositories.access import has_project, project_access
from switchboard.storage.repositories.projects import get_project_repo_topology

SCHEMA = "switchboard.agent_host_project_grant.v1"


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"error": code, "error_code": code, "message": message, **details}


def _public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["schema"] = SCHEMA
    result["active"] = result.get("status") == "active"
    return result


def _canonical_repo(project: str) -> str:
    topology = get_project_repo_topology(project)
    return str((((topology.get("roles") or {}).get("canonical") or {}).get("repo")) or "")


def create_agent_host_project_grant(
    *, source_project: str, host_id: str, target_project: str,
    canonical_repository: str, runtime: str, provider: str, trust_zone: str,
    isolation_mode: str, max_concurrency: int, actor: str,
) -> dict[str, Any]:
    """Grant an existing enrollment to one project/repository without cloning it."""
    init_project_registry()
    if not has_project(target_project):
        return _error("target_project_not_found", "target project is not registered")
    canonical = _canonical_repo(target_project)
    if not canonical or canonical.lower() != canonical_repository.lower():
        return _error(
            "canonical_repository_mismatch",
            "repository must exactly match the target project's canonical repository",
            expected_repository=canonical,
        )
    if runtime not in PROJECT_EXECUTION_RUNTIMES:
        return _error("runtime_not_supported", "runtime is not supported", runtime=runtime)
    if trust_zone not in PROJECT_EXECUTION_TRUST_ZONES:
        return _error("trust_zone_not_supported", "trust zone is not supported")
    if isolation_mode not in PROJECT_EXECUTION_ISOLATION_MODES:
        return _error("isolation_mode_not_supported", "isolation mode is not supported")
    if not provider:
        return _error("provider_required", "provider is required")
    if max_concurrency < 1 or max_concurrency > 32:
        return _error("invalid_max_concurrency", "max_concurrency must be between 1 and 32")

    source_access = project_access(source_project)
    target_access = project_access(target_project)
    with _conn(source_project) as connection:
        enrollment = connection.execute(
            "SELECT * FROM agent_host_enrollments WHERE project_id=? AND host_id=?",
            (source_project, host_id),
        ).fetchone()
        host = connection.execute(
            "SELECT * FROM agent_hosts WHERE host_id=?",
            (host_id,),
        ).fetchone()
    if not enrollment:
        return _error("host_enrollment_not_found", "host is not enrolled in the source project")
    enrollment = dict(enrollment)
    if enrollment.get("status") != "active":
        return _error("host_enrollment_revoked", "host enrollment is not active")
    owner_user_id = str(enrollment.get("owner_user_id") or "")
    source_owner = str(source_access.get("owner_user_id") or "")
    source_org = str(source_access.get("org_id") or "")
    target_org = str(target_access.get("org_id") or "")
    target_owner = str(target_access.get("owner_user_id") or "")
    if owner_user_id != source_owner and not (source_org and source_org == target_org):
        return _error(
            "host_ownership_mismatch",
            "host owner is not the source account or a member of the shared project organization",
        )
    if not source_org and not owner_user_id:
        return _error("host_ownership_missing", "host has no durable account or organization owner")
    if owner_user_id != target_owner and not (source_org and source_org == target_org):
        return _error(
            "target_project_access_denied",
            "host owner is not authorized for the target project account or organization",
        )
    fingerprint = str(enrollment.get("public_key_fingerprint") or "")
    if not fingerprint:
        return _error("host_attestation_missing", "host enrollment has no key attestation")
    if not host:
        return _error("host_inventory_missing", "host has no Capacity inventory record")
    host = dict(host)
    expires_at = float(host.get("heartbeat_at") or 0) + int(host.get("heartbeat_ttl_s") or 60)
    if host.get("status") != "online" or time.time() >= expires_at:
        return _error("host_attestation_stale", "host inventory attestation is stale")
    try:
        provider_allowlist = json.loads(enrollment.get("provider_allowlist_json") or "[]")
    except json.JSONDecodeError:
        provider_allowlist = []
    if provider not in provider_allowlist:
        return _error(
            "host_provider_not_authorized",
            "provider is not authorized by the Host enrollment",
            provider=provider,
        )
    try:
        advertised_runtimes = json.loads(host.get("runtimes_json") or "[]")
    except json.JSONDecodeError:
        advertised_runtimes = []
    runtime_names = {
        str(item.get("runtime") or item.get("name") or "")
        if isinstance(item, dict) else str(item)
        for item in advertised_runtimes if item
    }
    if runtime not in runtime_names:
        return _error(
            "host_runtime_not_advertised",
            "runtime is not advertised by the live Host inventory",
            runtime=runtime,
        )

    now = time.time()
    grant_id = "hostgrant-" + uuid.uuid4().hex[:20]
    values = (
        grant_id, source_project, host_id, owner_user_id, source_org,
        target_project, canonical, runtime, provider, trust_zone, isolation_mode,
        max_concurrency, int(enrollment.get("identity_generation") or 0), fingerprint,
        "active", now, actor, now, actor,
    )
    with _registry_conn() as connection:
        connection.execute(
            "INSERT INTO agent_host_project_grants("
            "grant_id,source_project_id,host_id,owner_user_id,owner_org_id,"
            "target_project_id,canonical_repository,runtime,provider,trust_zone,"
            "isolation_mode,max_concurrency,enrollment_identity_generation,"
            "attestation_fingerprint,status,created_at,created_by,updated_at,updated_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_project_id,host_id,target_project_id,canonical_repository) "
            "DO UPDATE SET runtime=excluded.runtime,provider=excluded.provider,"
            "trust_zone=excluded.trust_zone,isolation_mode=excluded.isolation_mode,"
            "max_concurrency=excluded.max_concurrency,"
            "enrollment_identity_generation=excluded.enrollment_identity_generation,"
            "attestation_fingerprint=excluded.attestation_fingerprint,status='active',"
            "revoked_at=NULL,revoke_reason=NULL,updated_at=excluded.updated_at,"
            "updated_by=excluded.updated_by",
            values,
        )
        row = connection.execute(
            "SELECT * FROM agent_host_project_grants WHERE source_project_id=? "
            "AND host_id=? AND target_project_id=? AND canonical_repository=?",
            (source_project, host_id, target_project, canonical),
        ).fetchone()
    return {"granted": True, "grant": _public(row)}


def revoke_agent_host_project_grant(
    *, source_project: str, grant_id: str, reason: str, actor: str,
) -> dict[str, Any]:
    init_project_registry()
    now = time.time()
    with _registry_conn() as connection:
        row = connection.execute(
            "SELECT * FROM agent_host_project_grants WHERE grant_id=? AND source_project_id=?",
            (grant_id, source_project),
        ).fetchone()
        if not row:
            return _error("host_grant_not_found", "host project grant was not found")
        connection.execute(
            "UPDATE agent_host_project_grants SET status='revoked',revoked_at=?,"
            "revoke_reason=?,updated_at=?,updated_by=? WHERE grant_id=?",
            (now, reason or "operator_revoke", now, actor, grant_id),
        )
        updated = connection.execute(
            "SELECT * FROM agent_host_project_grants WHERE grant_id=?", (grant_id,),
        ).fetchone()
    return {"revoked": True, "grant": _public(updated)}


def list_agent_host_project_grants(
    *, source_project: str = "", target_project: str = "", host_id: str = "",
    include_revoked: bool = False,
) -> list[dict[str, Any]]:
    init_project_registry()
    clauses: list[str] = []
    values: list[Any] = []
    for field, value in (("source_project_id", source_project),
                         ("target_project_id", target_project), ("host_id", host_id)):
        if value:
            clauses.append(f"{field}=?")
            values.append(value)
    if not include_revoked:
        clauses.append("status='active'")
    query = "SELECT * FROM agent_host_project_grants"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at, grant_id"
    with _registry_conn() as connection:
        rows = connection.execute(query, values).fetchall()
    return [_public(row) for row in rows]


__all__ = [
    "create_agent_host_project_grant", "revoke_agent_host_project_grant",
    "list_agent_host_project_grants",
]
