"""Tenant-scoped encrypted provider-connection vault (CO-6).

The vault lives in the project registry database because one customer identity may be
allowlisted for several projects in the same tenant. Raw provider credentials are never
returned by public repository methods; the only decryption surface is the explicitly
named trusted-runtime method, which requires an exact active lease binding.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Mapping, Optional

from db.core import _registry_conn
from db.schema import init_project_registry
from switchboard.domain.provider_credentials import (
    CredentialPrincipal,
    CredentialPolicyError,
    VaultKeyUnavailable,
    decrypt_credential,
    encrypt_credential,
    normalize_concurrency_policy,
    normalize_provider,
    validate_auth_type,
)


PROVIDER_CONNECTION_SCHEMA = "switchboard.provider_connection.v1"
PROVIDER_CREDENTIAL_LEASE_SCHEMA = "switchboard.provider_credential_lease.v1"
PROVIDER_CREDENTIAL_EVENT_SCHEMA = "switchboard.provider_credential_event.v1"
LIVE_LEASE_STATES = ("issued", "materializing", "active")


class CredentialVaultError(ValueError):
    """Stable secret-safe failure returned at application/transport boundaries."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "error_code": self.code, "message": self.message}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        parsed = value
    else:
        try:
            parsed = json.loads(value or "[]")
        except (TypeError, json.JSONDecodeError):
            parsed = []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _aad(reference: str, tenant_id: str, user_id: str, provider: str,
         provider_account_id: str, version: int) -> bytes:
    return "\x1f".join((
        reference, tenant_id, user_id, provider, provider_account_id, str(version),
    )).encode("utf-8")


def _safe_event_details(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Allowlist event details so a caller cannot smuggle a credential into audit rows."""
    allowed = {
        "credential_version", "fenced_lease_count", "lifecycle_state",
        "max_parallel", "refresh_state", "revocation_state", "ttl_seconds",
    }
    details: dict[str, Any] = {}
    for key, item in dict(value or {}).items():
        if key in allowed and isinstance(item, (str, int, float, bool, type(None))):
            details[key] = item
    return details


class ProviderCredentialRepository:
    """Registry-backed provider identity store with exact-binding credential leases."""

    def _prepare(self) -> None:
        init_project_registry()

    @staticmethod
    def _tenant_for_project_in(c: sqlite3.Connection, project: str) -> str:
        row = c.execute(
            "SELECT p.lifecycle_status, a.org_id FROM projects p "
            "JOIN project_access a ON a.project_id=p.id WHERE p.id=?",
            (str(project or "").strip(),),
        ).fetchone()
        if not row:
            raise CredentialVaultError("project_not_available", "project is not available", status_code=404)
        if str(row["lifecycle_status"] or "active") != "active":
            raise CredentialVaultError("project_not_active", "project is not active", status_code=423)
        tenant_id = str(row["org_id"] or "").strip()
        if not tenant_id:
            raise CredentialVaultError("tenant_binding_missing", "project tenant binding is missing", status_code=409)
        return tenant_id

    @classmethod
    def _validate_allowlist_in(cls, c: sqlite3.Connection, tenant_id: str,
                               selected_project: str,
                               allowlist: tuple[str, ...] | list[str]) -> list[str]:
        projects = sorted({str(item or "").strip().lower() for item in allowlist if str(item or "").strip()})
        if not projects or selected_project not in projects:
            raise CredentialVaultError(
                "project_allowlist_invalid",
                "project allowlist must include the selected project",
            )
        for project in projects:
            if cls._tenant_for_project_in(c, project) != tenant_id:
                raise CredentialVaultError(
                    "cross_tenant_allowlist_denied",
                    "every allowlisted project must belong to the same tenant",
                    status_code=403,
                )
        return projects

    @staticmethod
    def _validate_user_tenant_in(c: sqlite3.Connection, tenant_id: str,
                                 user_id: str) -> None:
        user = c.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        member = c.execute(
            "SELECT 1 FROM org_memberships WHERE org_id=? AND user_id=?",
            (tenant_id, user_id),
        ).fetchone()
        owner = c.execute(
            "SELECT 1 FROM project_access WHERE org_id=? AND owner_user_id=? LIMIT 1",
            (tenant_id, user_id),
        ).fetchone()
        if not user or (not member and not owner):
            raise CredentialVaultError(
                "user_tenant_binding_invalid",
                "provider identity user is not a member of the selected tenant",
                status_code=403,
            )

    @staticmethod
    def _event_in(c: sqlite3.Connection, row: Mapping[str, Any], event_type: str,
                  *, actor: str, project: str = "", task_id: str = "",
                  host_id: str = "", runner_session_id: str = "",
                  work_session_id: str = "", lease_id: str = "",
                  reason_code: str = "", details: Mapping[str, Any] | None = None,
                  now: Optional[float] = None) -> None:
        c.execute(
            "INSERT INTO provider_credential_events("
            "event_id, credential_reference, tenant_id, user_id, provider, "
            "provider_account_id, event_type, actor, project_id, task_id, host_id, "
            "runner_session_id, work_session_id, lease_id, reason_code, details_json, created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"provider-event-{uuid.uuid4().hex[:16]}",
                row["credential_reference"], row["tenant_id"], row["user_id"],
                row["provider"], row["provider_account_id"], event_type,
                str(actor or "system"), project or None, task_id or None, host_id or None,
                runner_session_id or None, work_session_id or None, lease_id or None,
                reason_code or None, json.dumps(_safe_event_details(details), sort_keys=True),
                time.time() if now is None else now,
            ),
        )

    @staticmethod
    def _public_connection(row: Mapping[str, Any], *, now: Optional[float] = None) -> dict[str, Any]:
        item = dict(row)
        timestamp = time.time() if now is None else now
        state = str(item.get("lifecycle_state") or "")
        expires_at = item.get("expires_at")
        if state == "active" and expires_at is not None and float(expires_at) <= timestamp:
            state = "expired"
        return {
            "schema": PROVIDER_CONNECTION_SCHEMA,
            "credential_reference": item.get("credential_reference"),
            "tenant_id": item.get("tenant_id"),
            "user_id": item.get("user_id"),
            "provider": item.get("provider"),
            "provider_account_id": item.get("provider_account_id"),
            "auth_type": item.get("auth_type"),
            "project_allowlist": _json_list(item.get("project_allowlist_json")),
            "lifecycle_state": state,
            "refresh_state": item.get("refresh_state"),
            "revocation_state": item.get("revocation_state"),
            "concurrency_policy": _json_object(item.get("concurrency_policy_json")),
            "expires_at": expires_at,
            "credential_version": int(item.get("credential_version") or 0),
            "credential_present": bool(item.get("encrypted_credential")),
            "key_id": item.get("key_id"),
            "audit_provenance": _json_object(item.get("audit_provenance_json")),
            "created_at": item.get("created_at"),
            "created_by": item.get("created_by"),
            "rotated_at": item.get("rotated_at"),
            "rotated_by": item.get("rotated_by"),
            "revoked_at": item.get("revoked_at"),
            "revoked_by": item.get("revoked_by"),
            "revocation_reason": item.get("revocation_reason"),
            "deleted_at": item.get("deleted_at"),
            "deleted_by": item.get("deleted_by"),
            "deletion_reason": item.get("deletion_reason"),
            "updated_at": item.get("updated_at"),
            "updated_by": item.get("updated_by"),
        }

    @staticmethod
    def _public_lease(row: Mapping[str, Any], *, now: Optional[float] = None) -> dict[str, Any]:
        item = dict(row)
        state = str(item.get("state") or "")
        if state in LIVE_LEASE_STATES and float(item.get("expires_at") or 0) <= (time.time() if now is None else now):
            state = "expired"
        return {
            "schema": PROVIDER_CREDENTIAL_LEASE_SCHEMA,
            "lease_id": item.get("lease_id"),
            "credential_reference": item.get("credential_reference"),
            "tenant_id": item.get("tenant_id"),
            "user_id": item.get("user_id"),
            "provider": item.get("provider"),
            "provider_account_id": item.get("provider_account_id"),
            "project": item.get("project_id"),
            "task_id": item.get("task_id"),
            "host_id": item.get("host_id"),
            "runner_session_id": item.get("runner_session_id"),
            "work_session_id": item.get("work_session_id"),
            "credential_version": int(item.get("credential_version") or 0),
            "state": state,
            "acquired_at": item.get("acquired_at"),
            "acquired_by": item.get("acquired_by"),
            "acquiring_principal": {
                "principal_id": item.get("acquiring_principal_id"),
                "principal_kind": item.get("acquiring_principal_kind"),
                "scopes": _json_list(item.get("acquiring_principal_scopes_json")),
                "admin": bool(item.get("acquiring_principal_admin")),
            },
            "expires_at": item.get("expires_at"),
            "materializing_at": item.get("materializing_at"),
            "activated_at": item.get("activated_at"),
            "released_at": item.get("released_at"),
            "released_by": item.get("released_by"),
            "release_reason": item.get("release_reason"),
        }

    @classmethod
    def _authorize_connection_in(cls, c: sqlite3.Connection, row: Mapping[str, Any],
                                 *, project: str, principal_user_id: str = "",
                                 admin: bool = False) -> None:
        tenant_id = cls._tenant_for_project_in(c, project)
        connection = dict(row)
        allowed = project in _json_list(connection.get("project_allowlist_json"))
        same_user = not principal_user_id or principal_user_id == connection.get("user_id")
        if connection.get("tenant_id") != tenant_id or not allowed or (not admin and not same_user):
            raise CredentialVaultError(
                "credential_not_available", "provider credential is not available", status_code=404)

    @staticmethod
    def _refresh_expiration_in(c: sqlite3.Connection, row: Mapping[str, Any],
                               now: float) -> dict[str, Any]:
        current = dict(row)
        if (current.get("lifecycle_state") == "active"
                and current.get("expires_at") is not None
                and float(current["expires_at"]) <= now):
            c.execute(
                "UPDATE provider_connections SET lifecycle_state='expired', "
                "refresh_state='expired', updated_at=? WHERE credential_reference=?",
                (now, current["credential_reference"]),
            )
            current["lifecycle_state"] = "expired"
            current["refresh_state"] = "expired"
            current["updated_at"] = now
        return current

    @classmethod
    def _expire_leases_in(cls, c: sqlite3.Connection, now: float) -> int:
        rows = c.execute(
            "SELECT l.*, p.tenant_id, p.user_id, p.provider, p.provider_account_id "
            "FROM provider_credential_leases l JOIN provider_connections p "
            "ON p.credential_reference=l.credential_reference "
            "WHERE l.state IN ('issued','materializing','active') AND l.expires_at<=?",
            (now,),
        ).fetchall()
        for row in rows:
            c.execute(
                "UPDATE provider_credential_leases SET state='expired', released_at=?, "
                "released_by='switchboard/lease-cleanup', release_reason='lease_expired' "
                "WHERE lease_id=? AND state IN ('issued','materializing','active')",
                (now, row["lease_id"]),
            )
            cls._event_in(
                c, row, "lease_expired", actor="switchboard/lease-cleanup",
                project=row["project_id"], task_id=row["task_id"], host_id=row["host_id"],
                runner_session_id=row["runner_session_id"],
                work_session_id=row["work_session_id"], lease_id=row["lease_id"],
                reason_code="lease_expired", now=now,
            )
        return len(rows)

    @classmethod
    def _fence_leases_in(cls, c: sqlite3.Connection, row: Mapping[str, Any], *,
                         actor: str, reason: str, now: float) -> int:
        leases = c.execute(
            "SELECT * FROM provider_credential_leases "
            "WHERE credential_reference=? AND state IN ('issued','materializing','active')",
            (row["credential_reference"],),
        ).fetchall()
        for lease in leases:
            c.execute(
                "UPDATE provider_credential_leases SET state='fenced', released_at=?, "
                "released_by=?, release_reason=? WHERE lease_id=? "
                "AND state IN ('issued','materializing','active')",
                (now, actor, reason, lease["lease_id"]),
            )
            cls._event_in(
                c, row, "lease_fenced", actor=actor, project=lease["project_id"],
                task_id=lease["task_id"], host_id=lease["host_id"],
                runner_session_id=lease["runner_session_id"],
                work_session_id=lease["work_session_id"], lease_id=lease["lease_id"],
                reason_code=reason, now=now,
            )
        return len(leases)

    def enroll(self, *, project: str, user_id: str, provider: str,
               provider_account_id: str, auth_type: str, credential: str,
               project_allowlist: tuple[str, ...] | list[str], actor: str,
               expires_at: float | None = None, refresh_state: str = "not_applicable",
               concurrency_policy: Mapping[str, Any] | None = None,
               audit_provenance: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._prepare()
        project = str(project or "").strip().lower()
        user_id = str(user_id or "").strip()
        account_id = str(provider_account_id or "").strip()
        actor = str(actor or "").strip() or "system"
        if not user_id or not account_id:
            raise CredentialVaultError("provider_identity_required", "user_id and provider_account_id are required")
        try:
            provider_id = normalize_provider(provider)
            auth_type_id = validate_auth_type(auth_type)
            policy = normalize_concurrency_policy(concurrency_policy)
        except CredentialPolicyError as exc:
            raise CredentialVaultError(exc.code, exc.message) from exc
        now = time.time()
        if expires_at is not None and float(expires_at) <= now:
            raise CredentialVaultError("credential_expiry_invalid", "credential expiry must be in the future")
        reference = f"provider-cred-{uuid.uuid4().hex}"
        version = 1
        try:
            with _registry_conn() as c:
                tenant_id = self._tenant_for_project_in(c, project)
                self._validate_user_tenant_in(c, tenant_id, user_id)
                allowlist = self._validate_allowlist_in(
                    c, tenant_id, project, list(project_allowlist))
                sealed = encrypt_credential(
                    credential,
                    associated_data=_aad(
                        reference, tenant_id, user_id, provider_id, account_id, version),
                )
                c.execute("BEGIN IMMEDIATE")
                c.execute(
                    "INSERT INTO provider_connections("
                    "credential_reference, tenant_id, user_id, provider, provider_account_id, "
                    "auth_type, project_allowlist_json, lifecycle_state, refresh_state, "
                    "revocation_state, concurrency_policy_json, expires_at, credential_version, "
                    "encrypted_credential, credential_nonce, key_id, audit_provenance_json, "
                    "created_at, created_by, updated_at, updated_by"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        reference, tenant_id, user_id, provider_id, account_id, auth_type_id,
                        json.dumps(allowlist, sort_keys=True), "active", refresh_state or "not_applicable",
                        "not_revoked", json.dumps(policy, sort_keys=True), expires_at, version,
                        sealed.ciphertext, sealed.nonce, sealed.key_id,
                        json.dumps(_safe_event_details(audit_provenance), sort_keys=True),
                        now, actor, now, actor,
                    ),
                )
                row = c.execute(
                    "SELECT * FROM provider_connections WHERE credential_reference=?",
                    (reference,),
                ).fetchone()
                self._event_in(
                    c, row, "enrolled", actor=actor, project=project,
                    details={"credential_version": version,
                             "max_parallel": policy["max_parallel"],
                             "refresh_state": refresh_state or "not_applicable"}, now=now,
                )
                return self._public_connection(row, now=now)
        except sqlite3.IntegrityError as exc:
            raise CredentialVaultError(
                "provider_account_already_enrolled",
                "this user/provider account is already enrolled",
                status_code=409,
            ) from exc
        except VaultKeyUnavailable as exc:
            raise CredentialVaultError(
                "vault_key_unavailable", "provider vault key is unavailable", status_code=503) from exc

    def get_metadata(self, credential_reference: str, *, project: str,
                     principal_user_id: str = "", admin: bool = False,
                     include_events: bool = False) -> dict[str, Any]:
        self._prepare()
        now = time.time()
        with _registry_conn() as c:
            row = c.execute(
                "SELECT * FROM provider_connections WHERE credential_reference=?",
                (str(credential_reference or "").strip(),),
            ).fetchone()
            if not row:
                raise CredentialVaultError(
                    "credential_not_available", "provider credential is not available", status_code=404)
            self._authorize_connection_in(
                c, row, project=project, principal_user_id=principal_user_id, admin=admin)
            current = self._refresh_expiration_in(c, row, now)
            result = self._public_connection(current, now=now)
            if include_events:
                events = c.execute(
                    "SELECT * FROM provider_credential_events WHERE credential_reference=? "
                    "ORDER BY created_at, event_id",
                    (current["credential_reference"],),
                ).fetchall()
                result["events"] = [self._public_event(event) for event in events]
            return result

    def list_metadata(self, *, project: str, principal_user_id: str = "",
                      admin: bool = False, user_id: str = "") -> list[dict[str, Any]]:
        self._prepare()
        now = time.time()
        with _registry_conn() as c:
            tenant_id = self._tenant_for_project_in(c, project)
            params: list[Any] = [tenant_id]
            sql = "SELECT * FROM provider_connections WHERE tenant_id=?"
            target_user = str(
                user_id or ("" if admin else principal_user_id) or ""
            ).strip()
            if target_user:
                sql += " AND user_id=?"
                params.append(target_user)
            rows = c.execute(sql + " ORDER BY created_at, credential_reference", params).fetchall()
            result = []
            for row in rows:
                try:
                    self._authorize_connection_in(
                        c, row, project=project, principal_user_id=principal_user_id,
                        admin=admin)
                except CredentialVaultError:
                    continue
                result.append(self._public_connection(
                    self._refresh_expiration_in(c, row, now), now=now))
            return result

    @staticmethod
    def _public_event(row: Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        return {
            "schema": PROVIDER_CREDENTIAL_EVENT_SCHEMA,
            "event_id": item.get("event_id"),
            "credential_reference": item.get("credential_reference"),
            "tenant_id": item.get("tenant_id"),
            "user_id": item.get("user_id"),
            "provider": item.get("provider"),
            "provider_account_id": item.get("provider_account_id"),
            "event_type": item.get("event_type"),
            "actor": item.get("actor"),
            "project": item.get("project_id"),
            "task_id": item.get("task_id"),
            "host_id": item.get("host_id"),
            "runner_session_id": item.get("runner_session_id"),
            "work_session_id": item.get("work_session_id"),
            "lease_id": item.get("lease_id"),
            "reason_code": item.get("reason_code"),
            "details": _safe_event_details(_json_object(item.get("details_json"))),
            "created_at": item.get("created_at"),
        }

    def rotate(self, credential_reference: str, *, project: str, credential: str,
               actor: str, expires_at: float | None = None,
               refresh_state: str = "fresh", principal_user_id: str = "",
               admin: bool = False) -> dict[str, Any]:
        self._prepare()
        now = time.time()
        if expires_at is not None and float(expires_at) <= now:
            raise CredentialVaultError("credential_expiry_invalid", "credential expiry must be in the future")
        try:
            with _registry_conn() as c:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute(
                    "SELECT * FROM provider_connections WHERE credential_reference=?",
                    (str(credential_reference or "").strip(),),
                ).fetchone()
                if not row:
                    raise CredentialVaultError(
                        "credential_not_available", "provider credential is not available", status_code=404)
                self._authorize_connection_in(
                    c, row, project=project, principal_user_id=principal_user_id, admin=admin)
                if row["lifecycle_state"] in {"revoked", "deleted"}:
                    raise CredentialVaultError(
                        "credential_not_rotatable", "revoked or deleted credentials cannot be rotated",
                        status_code=409,
                    )
                version = int(row["credential_version"] or 0) + 1
                sealed = encrypt_credential(
                    credential,
                    associated_data=_aad(
                        row["credential_reference"], row["tenant_id"], row["user_id"],
                        row["provider"], row["provider_account_id"], version),
                )
                fenced = self._fence_leases_in(
                    c, row, actor=actor, reason="credential_rotated", now=now)
                c.execute(
                    "UPDATE provider_connections SET lifecycle_state='active', refresh_state=?, "
                    "revocation_state='not_revoked', expires_at=?, credential_version=?, "
                    "encrypted_credential=?, credential_nonce=?, key_id=?, rotated_at=?, "
                    "rotated_by=?, updated_at=?, updated_by=? WHERE credential_reference=?",
                    (
                        refresh_state or "fresh", expires_at, version, sealed.ciphertext,
                        sealed.nonce, sealed.key_id, now, actor, now, actor,
                        row["credential_reference"],
                    ),
                )
                current = c.execute(
                    "SELECT * FROM provider_connections WHERE credential_reference=?",
                    (row["credential_reference"],),
                ).fetchone()
                self._event_in(
                    c, current, "rotated", actor=actor, project=project,
                    details={"credential_version": version, "fenced_lease_count": fenced,
                             "refresh_state": refresh_state or "fresh"}, now=now,
                )
                return self._public_connection(current, now=now)
        except VaultKeyUnavailable as exc:
            raise CredentialVaultError(
                "vault_key_unavailable", "provider vault key is unavailable", status_code=503) from exc

    def revoke(self, credential_reference: str, *, project: str, actor: str,
               reason: str, principal_user_id: str = "", admin: bool = False) -> dict[str, Any]:
        return self._terminal_transition(
            credential_reference, project=project, actor=actor, reason=reason,
            target_state="revoked", principal_user_id=principal_user_id, admin=admin)

    def delete(self, credential_reference: str, *, project: str, actor: str,
               reason: str, principal_user_id: str = "", admin: bool = False) -> dict[str, Any]:
        return self._terminal_transition(
            credential_reference, project=project, actor=actor, reason=reason,
            target_state="deleted", principal_user_id=principal_user_id, admin=admin)

    def _terminal_transition(self, credential_reference: str, *, project: str,
                             actor: str, reason: str, target_state: str,
                             principal_user_id: str, admin: bool) -> dict[str, Any]:
        self._prepare()
        reason = str(reason or "").strip()
        if not reason:
            raise CredentialVaultError("lifecycle_reason_required", "a lifecycle reason is required")
        now = time.time()
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM provider_connections WHERE credential_reference=?",
                (str(credential_reference or "").strip(),),
            ).fetchone()
            if not row:
                raise CredentialVaultError(
                    "credential_not_available", "provider credential is not available", status_code=404)
            self._authorize_connection_in(
                c, row, project=project, principal_user_id=principal_user_id, admin=admin)
            if row["lifecycle_state"] == target_state:
                return self._public_connection(row, now=now)
            if row["lifecycle_state"] == "deleted":
                raise CredentialVaultError(
                    "credential_deleted", "deleted credentials cannot change state", status_code=409)
            fenced = self._fence_leases_in(
                c, row, actor=actor, reason=f"credential_{target_state}", now=now)
            if target_state == "revoked":
                c.execute(
                    "UPDATE provider_connections SET lifecycle_state='revoked', "
                    "revocation_state='revoked', encrypted_credential=NULL, "
                    "credential_nonce=NULL, key_id=NULL, revoked_at=?, revoked_by=?, "
                    "revocation_reason=?, updated_at=?, updated_by=? "
                    "WHERE credential_reference=?",
                    (now, actor, reason, now, actor, row["credential_reference"]),
                )
            else:
                c.execute(
                    "UPDATE provider_connections SET lifecycle_state='deleted', "
                    "revocation_state='deleted', encrypted_credential=NULL, "
                    "credential_nonce=NULL, key_id=NULL, deleted_at=?, deleted_by=?, "
                    "deletion_reason=?, updated_at=?, updated_by=? "
                    "WHERE credential_reference=?",
                    (now, actor, reason, now, actor, row["credential_reference"]),
                )
            current = c.execute(
                "SELECT * FROM provider_connections WHERE credential_reference=?",
                (row["credential_reference"],),
            ).fetchone()
            self._event_in(
                c, current, target_state, actor=actor, project=project,
                reason_code=f"credential_{target_state}",
                details={"fenced_lease_count": fenced, "lifecycle_state": target_state},
                now=now,
            )
            return self._public_connection(current, now=now)

    def acquire_lease(self, *, project: str, credential_reference: str, user_id: str,
                      provider: str, provider_account_id: str, task_id: str,
                      host_id: str, runner_session_id: str, work_session_id: str,
                      ttl_seconds: int, actor: str,
                      principal: CredentialPrincipal) -> dict[str, Any]:
        self._prepare()
        try:
            provider_id = normalize_provider(provider)
        except CredentialPolicyError as exc:
            raise CredentialVaultError(exc.code, exc.message) from exc
        binding = {
            "project_id": str(project or "").strip().lower(),
            "task_id": str(task_id or "").strip(),
            "host_id": str(host_id or "").strip(),
            "runner_session_id": str(runner_session_id or "").strip(),
            "work_session_id": str(work_session_id or "").strip(),
        }
        if not all(binding.values()):
            raise CredentialVaultError(
                "credential_binding_incomplete",
                "project, task, host, runner session, and work session bindings are required",
            )
        now = time.time()
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._expire_leases_in(c, now)
            row = c.execute(
                "SELECT * FROM provider_connections WHERE credential_reference=?",
                (str(credential_reference or "").strip(),),
            ).fetchone()
            if not row:
                raise CredentialVaultError(
                    "credential_not_available", "provider credential is not available", status_code=404)
            self._authorize_connection_in(
                c, row, project=binding["project_id"], principal_user_id=user_id, admin=False)
            row = self._refresh_expiration_in(c, row, now)
            exact_identity = (
                row["user_id"] == str(user_id or "").strip()
                and row["provider"] == provider_id
                and row["provider_account_id"] == str(provider_account_id or "").strip()
            )
            if not exact_identity:
                raise CredentialVaultError(
                    "credential_identity_mismatch", "provider credential identity binding failed",
                    status_code=403,
                )
            if (row["lifecycle_state"] != "active"
                    or row["revocation_state"] != "not_revoked"
                    or not row["encrypted_credential"]):
                raise CredentialVaultError(
                    "credential_not_usable", "provider credential is not active", status_code=409)
            existing = c.execute(
                "SELECT * FROM provider_credential_leases WHERE credential_reference=? "
                "AND project_id=? AND task_id=? AND host_id=? AND runner_session_id=? "
                "AND work_session_id=? AND credential_version=? "
                "AND state IN ('issued','materializing','active')",
                (
                    row["credential_reference"], binding["project_id"], binding["task_id"],
                    binding["host_id"], binding["runner_session_id"],
                    binding["work_session_id"], row["credential_version"],
                ),
            ).fetchone()
            if existing:
                if (existing["state"] == "issued"
                        and existing["acquiring_principal_id"] == principal.principal_id
                        and existing["acquiring_principal_kind"] == principal.principal_kind):
                    return self._public_lease(existing, now=now)
                raise CredentialVaultError(
                    "credential_lease_already_consumed",
                    "provider credential lease is already being used",
                    status_code=409,
                )
            policy = _json_object(row["concurrency_policy_json"])
            maximum = int(policy.get("max_parallel") or 1)
            active_count = c.execute(
                "SELECT COUNT(*) FROM provider_credential_leases "
                "WHERE credential_reference=? "
                "AND state IN ('issued','materializing','active') AND expires_at>?",
                (row["credential_reference"], now),
            ).fetchone()[0]
            if int(active_count) >= maximum:
                raise CredentialVaultError(
                    "credential_concurrency_exhausted",
                    "provider credential concurrency policy denies another lease",
                    status_code=409,
                )
            lease_id = f"provider-lease-{uuid.uuid4().hex[:20]}"
            expires_at = now + int(ttl_seconds)
            c.execute(
                "INSERT INTO provider_credential_leases("
                "lease_id, credential_reference, tenant_id, user_id, provider, "
                "provider_account_id, project_id, task_id, host_id, runner_session_id, "
                "work_session_id, credential_version, state, acquired_at, acquired_by, expires_at, "
                "acquiring_principal_id, acquiring_principal_kind, "
                "acquiring_principal_scopes_json, acquiring_principal_admin"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    lease_id, row["credential_reference"], row["tenant_id"], row["user_id"],
                    row["provider"], row["provider_account_id"], binding["project_id"],
                    binding["task_id"], binding["host_id"], binding["runner_session_id"],
                    binding["work_session_id"], row["credential_version"], "issued", now,
                    actor, expires_at, principal.principal_id, principal.principal_kind,
                    json.dumps(list(principal.scopes), sort_keys=True), int(principal.admin),
                ),
            )
            lease = c.execute(
                "SELECT * FROM provider_credential_leases WHERE lease_id=?", (lease_id,),
            ).fetchone()
            self._event_in(
                c, row, "lease_acquired", actor=actor, project=binding["project_id"],
                task_id=binding["task_id"], host_id=binding["host_id"],
                runner_session_id=binding["runner_session_id"],
                work_session_id=binding["work_session_id"], lease_id=lease_id,
                details={"credential_version": row["credential_version"],
                         "ttl_seconds": int(ttl_seconds)}, now=now,
            )
            return self._public_lease(lease, now=now)

    @staticmethod
    def _principal_is_acquirer(lease: Mapping[str, Any],
                               principal: CredentialPrincipal) -> bool:
        return (
            lease.get("acquiring_principal_id") == principal.principal_id
            and lease.get("acquiring_principal_kind") == principal.principal_kind
        )

    @classmethod
    def _authorize_lease_release(cls, lease: Mapping[str, Any],
                                 principal: CredentialPrincipal) -> None:
        owner = (
            principal.principal_kind == "user"
            and principal.principal_id == lease.get("user_id")
        )
        exact_service = (
            principal.principal_kind in {"agent", "host", "system"}
            and principal.can_use_credentials()
            and cls._principal_is_acquirer(lease, principal)
        )
        dispatcher = (
            principal.principal_kind == "system"
            and principal.can_use_credentials()
        )
        if not (principal.admin or owner or exact_service or dispatcher):
            raise CredentialVaultError(
                "credential_lease_release_denied",
                "caller cannot release this provider credential lease",
                status_code=403,
            )

    def release_lease(self, lease_id: str, *, project: str, actor: str, reason: str,
                      principal: CredentialPrincipal) -> dict[str, Any]:
        self._prepare()
        now = time.time()
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._expire_leases_in(c, now)
            lease = c.execute(
                "SELECT * FROM provider_credential_leases WHERE lease_id=?",
                (str(lease_id or "").strip(),),
            ).fetchone()
            if not lease:
                raise CredentialVaultError(
                    "credential_lease_not_available", "provider credential lease is not available",
                    status_code=404,
                )
            row = c.execute(
                "SELECT * FROM provider_connections WHERE credential_reference=?",
                (lease["credential_reference"],),
            ).fetchone()
            self._authorize_connection_in(c, row, project=project, admin=True)
            if lease["project_id"] != project:
                raise CredentialVaultError(
                    "credential_lease_not_available", "provider credential lease is not available",
                    status_code=404,
                )
            self._authorize_lease_release(dict(lease), principal)
            if lease["state"] in LIVE_LEASE_STATES:
                c.execute(
                    "UPDATE provider_credential_leases SET state='released', released_at=?, "
                    "released_by=?, release_reason=? WHERE lease_id=? "
                    "AND state IN ('issued','materializing','active')",
                    (now, actor, reason or "released", lease["lease_id"]),
                )
                self._event_in(
                    c, row, "lease_released", actor=actor, project=lease["project_id"],
                    task_id=lease["task_id"], host_id=lease["host_id"],
                    runner_session_id=lease["runner_session_id"],
                    work_session_id=lease["work_session_id"], lease_id=lease["lease_id"],
                    reason_code=reason or "released", now=now,
                )
            current = c.execute(
                "SELECT * FROM provider_credential_leases WHERE lease_id=?",
                (lease["lease_id"],),
            ).fetchone()
            return self._public_lease(current, now=now)

    def materialize_for_runtime(self, lease_id: str, *, project: str, user_id: str,
                                provider: str, provider_account_id: str, task_id: str,
                                host_id: str, runner_session_id: str,
                                work_session_id: str, actor: str,
                                principal: CredentialPrincipal) -> str:
        """Trusted bridge only: validate every binding, then decrypt in process memory.

        This method is deliberately not registered as a REST or MCP tool. Callers must write
        the returned value directly into an isolated runtime home and must never serialize it.
        """
        self._prepare()
        try:
            provider_id = normalize_provider(provider)
        except CredentialPolicyError as exc:
            raise CredentialVaultError(exc.code, exc.message) from exc
        expected = {
            "project_id": str(project or "").strip().lower(),
            "user_id": str(user_id or "").strip(),
            "provider": provider_id,
            "provider_account_id": str(provider_account_id or "").strip(),
            "task_id": str(task_id or "").strip(),
            "host_id": str(host_id or "").strip(),
            "runner_session_id": str(runner_session_id or "").strip(),
            "work_session_id": str(work_session_id or "").strip(),
        }
        now = time.time()
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._expire_leases_in(c, now)
            lease = c.execute(
                "SELECT * FROM provider_credential_leases WHERE lease_id=?",
                (str(lease_id or "").strip(),),
            ).fetchone()
            if not lease or any(lease[key] != value for key, value in expected.items()):
                raise CredentialVaultError(
                    "credential_binding_mismatch",
                    "provider credential lease binding failed",
                    status_code=403,
                )
            if lease["state"] == "expired":
                c.commit()
                raise CredentialVaultError(
                    "credential_not_usable", "provider credential lease has expired",
                    status_code=409,
                )
            if lease["state"] != "issued":
                raise CredentialVaultError(
                    "credential_lease_already_consumed",
                    "provider credential lease cannot be materialized again",
                    status_code=409,
                )
            if not self._principal_is_acquirer(dict(lease), principal):
                raise CredentialVaultError(
                    "credential_principal_binding_mismatch",
                    "provider credential principal binding failed",
                    status_code=403,
                )
            row = c.execute(
                "SELECT * FROM provider_connections WHERE credential_reference=?",
                (lease["credential_reference"],),
            ).fetchone()
            self._authorize_connection_in(
                c, row, project=expected["project_id"],
                principal_user_id=expected["user_id"], admin=False)
            row = self._refresh_expiration_in(c, row, now)
            if (row["lifecycle_state"] != "active"
                    or row["revocation_state"] != "not_revoked"
                    or int(row["credential_version"]) != int(lease["credential_version"])
                    or not row["encrypted_credential"] or not row["credential_nonce"]):
                raise CredentialVaultError(
                    "credential_not_usable", "provider credential is not active", status_code=409)
            changed = c.execute(
                "UPDATE provider_credential_leases SET state='materializing', "
                "materializing_at=? WHERE lease_id=? AND state='issued' AND expires_at>?",
                (now, lease["lease_id"], now),
            ).rowcount
            if changed != 1:
                raise CredentialVaultError(
                    "credential_lease_already_consumed",
                    "provider credential lease cannot be materialized again",
                    status_code=409,
                )
            try:
                credential = decrypt_credential(
                    row["encrypted_credential"], row["credential_nonce"], key_id=row["key_id"],
                    associated_data=_aad(
                        row["credential_reference"], row["tenant_id"], row["user_id"],
                        row["provider"], row["provider_account_id"], row["credential_version"]),
                )
            except (VaultKeyUnavailable, RuntimeError) as exc:
                c.execute(
                    "UPDATE provider_credential_leases SET state='fenced', released_at=?, "
                    "released_by='switchboard/vault', release_reason='materialization_failed' "
                    "WHERE lease_id=? AND state='materializing'",
                    (now, lease["lease_id"]),
                )
                self._event_in(
                    c, row, "materialization_failed", actor="switchboard/vault",
                    project=lease["project_id"], task_id=lease["task_id"],
                    host_id=lease["host_id"], runner_session_id=lease["runner_session_id"],
                    work_session_id=lease["work_session_id"], lease_id=lease["lease_id"],
                    reason_code="vault_decryption_failed", now=now,
                )
                # Persist the fail-closed fence before surfacing the error. Letting the
                # context manager roll this transaction back would leave a compromised
                # lease active and repeatedly eligible for materialization attempts.
                c.commit()
                raise CredentialVaultError(
                    "credential_materialization_failed",
                    "provider credential materialization failed",
                    status_code=503,
                ) from exc
            self._event_in(
                c, row, "materialized", actor=actor, project=lease["project_id"],
                task_id=lease["task_id"], host_id=lease["host_id"],
                runner_session_id=lease["runner_session_id"],
                work_session_id=lease["work_session_id"], lease_id=lease["lease_id"],
                details={"credential_version": row["credential_version"]}, now=now,
            )
            return credential

    def activate_materialized_lease(self, lease_id: str, *, actor: str,
                                    principal: CredentialPrincipal) -> dict[str, Any]:
        """Mark a consumed lease active only after the provider process starts."""
        self._prepare()
        now = time.time()
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._expire_leases_in(c, now)
            lease = c.execute(
                "SELECT * FROM provider_credential_leases WHERE lease_id=?",
                (str(lease_id or "").strip(),),
            ).fetchone()
            if not lease or lease["state"] != "materializing":
                if lease and lease["state"] == "expired":
                    # Keep the cleanup transition even though activation is denied.
                    c.commit()
                raise CredentialVaultError(
                    "credential_lease_activation_denied",
                    "provider credential lease is not awaiting activation",
                    status_code=409,
                )
            if not self._principal_is_acquirer(dict(lease), principal):
                raise CredentialVaultError(
                    "credential_principal_binding_mismatch",
                    "provider credential principal binding failed",
                    status_code=403,
                )
            changed = c.execute(
                "UPDATE provider_credential_leases SET state='active', activated_at=? "
                "WHERE lease_id=? AND state='materializing' AND expires_at>?",
                (now, lease["lease_id"], now),
            ).rowcount
            if changed != 1:
                raise CredentialVaultError(
                    "credential_lease_activation_denied",
                    "provider credential lease activation failed",
                    status_code=409,
                )
            row = c.execute(
                "SELECT * FROM provider_connections WHERE credential_reference=?",
                (lease["credential_reference"],),
            ).fetchone()
            self._event_in(
                c, row, "lease_activated", actor=actor, project=lease["project_id"],
                task_id=lease["task_id"], host_id=lease["host_id"],
                runner_session_id=lease["runner_session_id"],
                work_session_id=lease["work_session_id"], lease_id=lease["lease_id"],
                now=now,
            )
            current = c.execute(
                "SELECT * FROM provider_credential_leases WHERE lease_id=?",
                (lease["lease_id"],),
            ).fetchone()
            return self._public_lease(current, now=now)

    def fence_materialized_lease(self, lease_id: str, *, actor: str, reason: str,
                                 principal: CredentialPrincipal) -> dict[str, Any]:
        """Permanently consume a lease after any launch/materialization failure."""
        self._prepare()
        now = time.time()
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            lease = c.execute(
                "SELECT * FROM provider_credential_leases WHERE lease_id=?",
                (str(lease_id or "").strip(),),
            ).fetchone()
            if not lease:
                raise CredentialVaultError(
                    "credential_lease_not_available",
                    "provider credential lease is not available",
                    status_code=404,
                )
            if not self._principal_is_acquirer(dict(lease), principal):
                raise CredentialVaultError(
                    "credential_principal_binding_mismatch",
                    "provider credential principal binding failed",
                    status_code=403,
                )
            if lease["state"] in {"issued", "materializing", "active"}:
                c.execute(
                    "UPDATE provider_credential_leases SET state='fenced', released_at=?, "
                    "released_by=?, release_reason=? WHERE lease_id=? "
                    "AND state IN ('issued','materializing','active')",
                    (now, actor, reason or "process_start_failed", lease["lease_id"]),
                )
                row = c.execute(
                    "SELECT * FROM provider_connections WHERE credential_reference=?",
                    (lease["credential_reference"],),
                ).fetchone()
                self._event_in(
                    c, row, "lease_fenced", actor=actor, project=lease["project_id"],
                    task_id=lease["task_id"], host_id=lease["host_id"],
                    runner_session_id=lease["runner_session_id"],
                    work_session_id=lease["work_session_id"], lease_id=lease["lease_id"],
                    reason_code=reason or "process_start_failed", now=now,
                )
            current = c.execute(
                "SELECT * FROM provider_credential_leases WHERE lease_id=?",
                (lease["lease_id"],),
            ).fetchone()
            return self._public_lease(current, now=now)

    def cleanup_expired_leases(self) -> int:
        self._prepare()
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            return self._expire_leases_in(c, time.time())


default_provider_credential_repository = ProviderCredentialRepository()


__all__ = [
    "CredentialVaultError",
    "PROVIDER_CONNECTION_SCHEMA",
    "PROVIDER_CREDENTIAL_EVENT_SCHEMA",
    "PROVIDER_CREDENTIAL_LEASE_SCHEMA",
    "ProviderCredentialRepository",
    "default_provider_credential_repository",
]
