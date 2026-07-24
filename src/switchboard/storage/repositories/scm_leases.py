"""Short-lived SCM lease broker (ENFORCE-13).

A lease authorizes one execution to run specific repository operations against one
repository, for a bounded time, only after an exact host/wake/runner/claim bind
exists. The lease references its SCM connection by id and pins the installation
version; the raw installation ref and the materialized GitHub-App token are never
stored on the lease, never written to an event, and never returned by a public
read. The token is produced once, inside the trusted runtime, by the injected
minter and returned only across ``materialize_for_runtime``.

The broker authorizes every operation through the ACCESS-28 SCM connection
preflight, so an operation, repository, org, or project the connection does not
allow can never be leased. Rotation, revocation, expiry, host loss, cancellation,
replay, and context drift all fence the lease so a stale worker cannot mint a token.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Callable, Mapping, Optional

from db.core import _registry_conn
from db.schema import init_project_registry
from switchboard.domain.scm_leases import (
    MintedSCMToken,
    SCMLeaseError,
    SCMLeasePrincipal,
    SCMTokenMinter,
    UnconfiguredSCMTokenMinter,
    normalize_operations,
    phase_for_operation,
)

SCM_LEASE_SCHEMA = "switchboard.scm_lease.v1"

LIVE_LEASE_STATES = ("issued", "materializing", "active")

# A lease is short-lived by design: a caller can request less, never more.
MAX_LEASE_TTL_SECONDS = 3600
# Cap caller-supplied free-text so a lifecycle reason can never carry a large blob.
_MAX_REASON_LENGTH = 256

# The exact binding a lease is issued against. All must be present — an empty field
# means no exact host/wake/runner/claim bind exists yet, so no lease may be issued.
_BINDING_FIELDS = (
    "project_id", "task_id", "generation", "context_digest", "host_id",
    "runner_session_id", "work_session_id", "claim_id", "wake_id", "repository",
)

# Event details are allowlisted so a caller can never smuggle a secret into audit.
_SAFE_EVENT_KEYS = frozenset({
    "installation_version", "phase", "operation", "operations", "ttl_seconds",
    "lifecycle_state", "fenced_lease_count", "reason", "token_returned_once",
})

SCMAuthorizer = Callable[..., Mapping[str, Any]]


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _clip_reason(reason: Any) -> str:
    """Bound a caller-supplied lifecycle reason before it is stored or echoed."""
    return str(reason or "")[:_MAX_REASON_LENGTH]


def _safe_event_details(value: Mapping[str, Any] | None) -> dict[str, Any]:
    details: dict[str, Any] = {}
    for key, item in dict(value or {}).items():
        if key not in _SAFE_EVENT_KEYS:
            continue
        if isinstance(item, (str, int, float, bool, type(None))):
            details[key] = item
        elif isinstance(item, (list, tuple)):
            details[key] = [str(entry) for entry in item]
    return details


class SCMLeaseRepository:
    """Registry-backed exact-binding lease store for repository operations."""

    def __init__(self, *, scm_authorizer: Optional[SCMAuthorizer] = None,
                 minter: Optional[SCMTokenMinter] = None) -> None:
        self._authorizer = scm_authorizer
        self._minter = minter or UnconfiguredSCMTokenMinter()

    # -- authorization port -------------------------------------------------
    def _authorize(self, connection_id: str, *, project: str, repository: str,
                   operation: str, actor: str) -> dict[str, Any]:
        authorizer = self._authorizer
        if authorizer is None:
            from switchboard.storage.repositories.scm_connections import (
                default_scm_connection_repository,
            )
            authorizer = default_scm_connection_repository.preflight
        try:
            return dict(authorizer(
                connection_id, project=project, repository=repository,
                operation=operation, actor=actor) or {})
        except Exception as exc:  # noqa: BLE001
            # A missing, deleted, or otherwise unresolvable connection makes the
            # authorizer raise (ACCESS-28 raises scm_connection_not_found). Fail
            # closed: treat any authorizer failure as an explicit denial.
            code = str(getattr(exc, "code", "") or "scm_connection_not_authorized")
            return {"allowed": False, "error_code": code}

    @staticmethod
    def _prepare() -> None:
        init_project_registry()

    # -- projections --------------------------------------------------------
    @staticmethod
    def _public_lease(row: Mapping[str, Any], *, now: Optional[float] = None) -> dict[str, Any]:
        item = dict(row)
        state = str(item.get("state") or "")
        timestamp = time.time() if now is None else now
        if state in LIVE_LEASE_STATES and float(item.get("expires_at") or 0) <= timestamp:
            state = "expired"
        return {
            "schema": SCM_LEASE_SCHEMA,
            "lease_id": item.get("lease_id"),
            "connection_id": item.get("connection_id"),
            "installation_version": int(item.get("installation_version") or 0),
            "org_id": item.get("org_id"),
            "project": item.get("project_id"),
            "task_id": item.get("task_id"),
            "generation": item.get("generation"),
            "context_digest": item.get("context_digest"),
            "host_id": item.get("host_id"),
            "runner_session_id": item.get("runner_session_id"),
            "work_session_id": item.get("work_session_id"),
            "claim_id": item.get("claim_id"),
            "wake_id": item.get("wake_id"),
            "repository": item.get("repository"),
            "phase": item.get("phase"),
            "operations": _json_list(item.get("operations_json")),
            "state": state,
            "acquiring_principal": {
                "principal_id": item.get("acquiring_principal_id"),
                "principal_kind": item.get("acquiring_principal_kind"),
                "scopes": _json_list(item.get("acquiring_principal_scopes_json")),
                "admin": bool(item.get("acquiring_principal_admin")),
            },
            "acquired_at": item.get("acquired_at"),
            "acquired_by": item.get("acquired_by"),
            "expires_at": item.get("expires_at"),
            "materializing_at": item.get("materializing_at"),
            "activated_at": item.get("activated_at"),
            "released_at": item.get("released_at"),
            "released_by": item.get("released_by"),
            "release_reason": item.get("release_reason"),
        }

    @staticmethod
    def _event_in(c: sqlite3.Connection, lease: Mapping[str, Any], event_type: str, *,
                  actor: str, operation: str = "", reason_code: str = "",
                  details: Mapping[str, Any] | None = None,
                  now: Optional[float] = None) -> None:
        source = dict(lease)
        c.execute(
            "INSERT INTO scm_lease_events("
            "event_id, lease_id, connection_id, event_type, actor, org_id, project_id, "
            "task_id, generation, host_id, runner_session_id, work_session_id, claim_id, "
            "wake_id, repository, phase, operation, reason_code, details_json, created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"scm-lease-event-{uuid.uuid4().hex[:16]}",
                source.get("lease_id"), source.get("connection_id"), event_type,
                str(actor or "system"), source.get("org_id"), source.get("project_id"),
                source.get("task_id"), source.get("generation"), source.get("host_id"),
                source.get("runner_session_id"), source.get("work_session_id"),
                source.get("claim_id"), source.get("wake_id"), source.get("repository"),
                source.get("phase"), operation or None, reason_code or None,
                json.dumps(_safe_event_details(details), sort_keys=True),
                time.time() if now is None else now,
            ),
        )

    @classmethod
    def _expire_leases_in(cls, c: sqlite3.Connection, now: float) -> int:
        rows = c.execute(
            "SELECT * FROM scm_leases WHERE state IN ('issued','materializing','active') "
            "AND expires_at<=?",
            (now,),
        ).fetchall()
        for row in rows:
            c.execute(
                "UPDATE scm_leases SET state='expired', released_at=?, "
                "released_by='switchboard/scm-lease-cleanup', release_reason='lease_expired' "
                "WHERE lease_id=? AND state IN ('issued','materializing','active')",
                (now, row["lease_id"]),
            )
            cls._event_in(c, row, "lease_expired", actor="switchboard/scm-lease-cleanup",
                          reason_code="lease_expired", now=now)
        return len(rows)

    # -- acquisition --------------------------------------------------------
    def acquire_lease(self, *, project: str, connection_id: str, repository: str,
                      org_id: str, operations: Any, task_id: str, generation: str,
                      context_digest: str, host_id: str, runner_session_id: str,
                      work_session_id: str, claim_id: str, wake_id: str,
                      ttl_seconds: int, actor: str,
                      principal: SCMLeasePrincipal) -> dict[str, Any]:
        self._prepare()
        phase, ops = normalize_operations(operations)
        binding = {
            "project_id": str(project or "").strip().lower(),
            "task_id": str(task_id or "").strip(),
            "generation": str(generation or "").strip(),
            "context_digest": str(context_digest or "").strip(),
            "host_id": str(host_id or "").strip(),
            "runner_session_id": str(runner_session_id or "").strip(),
            "work_session_id": str(work_session_id or "").strip(),
            "claim_id": str(claim_id or "").strip(),
            "wake_id": str(wake_id or "").strip(),
            "repository": str(repository or "").strip().lower(),
        }
        if not str(connection_id or "").strip() or not all(binding.values()):
            raise SCMLeaseError(
                "scm_lease_binding_incomplete",
                "connection, project, task, generation, context digest, host, runner, "
                "work session, claim, wake, and repository bindings are all required",
            )
        if not principal.can_use_credentials():
            raise SCMLeaseError(
                "scm_lease_principal_denied",
                "principal is not permitted to broker SCM leases", status_code=403)
        expected_org = binding["repository"].split("/", 1)[0]
        if str(org_id or "").strip().lower() != expected_org:
            raise SCMLeaseError(
                "scm_org_binding_mismatch",
                "org binding must match the repository owner", status_code=403)

        # Authorize every requested operation against the ACCESS-28 connection first,
        # outside the lease write transaction, so we never nest registry connections.
        installation_version = 0
        for operation in ops:
            decision = self._authorize(
                connection_id, project=binding["project_id"],
                repository=binding["repository"], operation=operation, actor=actor)
            if not decision.get("allowed"):
                raise SCMLeaseError(
                    str(decision.get("error_code") or "repository_not_authorized"),
                    "repository operation is not authorized for this SCM connection",
                    status_code=403,
                )
            installation_version = int(decision.get("installation_version") or 0)

        now = time.time()
        lease_id = f"scm-lease-{uuid.uuid4().hex[:20]}"
        # Short-lived by design: honor a shorter caller request, never a longer one.
        expires_at = now + min(int(ttl_seconds), MAX_LEASE_TTL_SECONDS)
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._expire_leases_in(c, now)
            existing = c.execute(
                "SELECT * FROM scm_leases WHERE connection_id=? AND project_id=? AND task_id=? "
                "AND generation=? AND host_id=? AND runner_session_id=? AND work_session_id=? "
                "AND claim_id=? AND wake_id=? AND repository=? AND phase=? "
                "AND state IN ('issued','materializing','active')",
                (
                    connection_id, binding["project_id"], binding["task_id"],
                    binding["generation"], binding["host_id"], binding["runner_session_id"],
                    binding["work_session_id"], binding["claim_id"], binding["wake_id"],
                    binding["repository"], phase,
                ),
            ).fetchone()
            if existing:
                if (existing["state"] == "issued"
                        and existing["acquiring_principal_id"] == principal.principal_id
                        and existing["acquiring_principal_kind"] == principal.principal_kind
                        and _json_list(existing["operations_json"]) == ops):
                    return self._public_lease(existing, now=now)
                raise SCMLeaseError(
                    "scm_lease_already_consumed",
                    "an SCM lease for this exact execution binding is already live",
                    status_code=409,
                )
            c.execute(
                "INSERT INTO scm_leases("
                "lease_id, connection_id, installation_version, org_id, project_id, task_id, "
                "generation, context_digest, host_id, runner_session_id, work_session_id, "
                "claim_id, wake_id, repository, phase, operations_json, state, acquired_at, "
                "acquired_by, acquiring_principal_id, acquiring_principal_kind, "
                "acquiring_principal_scopes_json, acquiring_principal_admin, expires_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    lease_id, connection_id, installation_version, expected_org,
                    binding["project_id"], binding["task_id"], binding["generation"],
                    binding["context_digest"], binding["host_id"], binding["runner_session_id"],
                    binding["work_session_id"], binding["claim_id"], binding["wake_id"],
                    binding["repository"], phase, json.dumps(ops, sort_keys=True), "issued",
                    now, actor, principal.principal_id, principal.principal_kind,
                    json.dumps(list(principal.scopes), sort_keys=True), int(principal.admin),
                    expires_at,
                ),
            )
            lease = c.execute("SELECT * FROM scm_leases WHERE lease_id=?", (lease_id,)).fetchone()
            self._event_in(
                c, lease, "lease_acquired", actor=actor,
                details={"installation_version": installation_version, "phase": phase,
                         "operations": ops, "ttl_seconds": int(ttl_seconds)}, now=now)
            return self._public_lease(lease, now=now)

    # -- reads --------------------------------------------------------------
    def get_lease(self, lease_id: str, *, project: str) -> dict[str, Any]:
        self._prepare()
        now = time.time()
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._expire_leases_in(c, now)
            lease = c.execute(
                "SELECT * FROM scm_leases WHERE lease_id=?",
                (str(lease_id or "").strip(),),
            ).fetchone()
            if not lease or lease["project_id"] != str(project or "").strip().lower():
                raise SCMLeaseError(
                    "scm_lease_not_available", "SCM lease is not available", status_code=404)
            return self._public_lease(lease, now=now)

    @staticmethod
    def _binding_matches(lease: Mapping[str, Any], expected: Mapping[str, str]) -> bool:
        return all(str(lease[key] or "") == value for key, value in expected.items())

    @classmethod
    def _fence_lease_in(cls, c: sqlite3.Connection, lease: Mapping[str, Any], *,
                        actor: str, reason: str, now: float) -> None:
        changed = c.execute(
            "UPDATE scm_leases SET state='fenced', released_at=?, released_by=?, "
            "release_reason=? WHERE lease_id=? AND state IN ('issued','materializing','active')",
            (now, actor, reason, lease["lease_id"]),
        ).rowcount
        if changed:
            cls._event_in(c, lease, "lease_fenced", actor=actor, reason_code=reason,
                          details={"reason": reason}, now=now)

    # -- trusted-runtime materialization ------------------------------------
    def materialize_for_runtime(self, lease_id: str, *, project: str, task_id: str,
                                generation: str, context_digest: str, host_id: str,
                                runner_session_id: str, work_session_id: str,
                                claim_id: str, wake_id: str, repository: str,
                                operation: str, actor: str,
                                principal: SCMLeasePrincipal,
                                minter: Optional[SCMTokenMinter] = None) -> MintedSCMToken:
        """Trusted bridge only: validate every binding, then mint one short-lived token.

        Not registered as a REST or MCP tool. The returned token must be written
        straight into the isolated runtime and never serialized, logged, or stored.
        Any failure fences the lease so a stale worker cannot mint again.
        """
        self._prepare()
        op = str(operation or "").strip().lower()
        phase_for_operation(op)  # fail closed on an unknown operation
        expected = {
            "project_id": str(project or "").strip().lower(),
            "task_id": str(task_id or "").strip(),
            "generation": str(generation or "").strip(),
            "context_digest": str(context_digest or "").strip(),
            "host_id": str(host_id or "").strip(),
            "runner_session_id": str(runner_session_id or "").strip(),
            "work_session_id": str(work_session_id or "").strip(),
            "claim_id": str(claim_id or "").strip(),
            "wake_id": str(wake_id or "").strip(),
            "repository": str(repository or "").strip().lower(),
        }

        with _registry_conn() as c:
            preview = c.execute(
                "SELECT * FROM scm_leases WHERE lease_id=?",
                (str(lease_id or "").strip(),),
            ).fetchone()
        if not preview:
            raise SCMLeaseError(
                "scm_lease_not_available", "SCM lease is not available", status_code=404)
        if not self._binding_matches(preview, expected):
            raise SCMLeaseError(
                "scm_lease_binding_mismatch", "SCM lease binding failed", status_code=403)
        if (preview["acquiring_principal_id"] != principal.principal_id
                or preview["acquiring_principal_kind"] != principal.principal_kind):
            raise SCMLeaseError(
                "scm_lease_principal_binding_mismatch",
                "SCM lease principal binding failed", status_code=403)
        if op not in _json_list(preview["operations_json"]):
            raise SCMLeaseError(
                "scm_operation_not_leased",
                "operation is not within this lease's granted phase", status_code=403)

        # Full re-authorization against the ACCESS-28 connection (allowlists, topology,
        # operation scope). The authoritative revocation/rotation check happens again
        # in-transaction below, closing the window between this call and the lock.
        decision = self._authorize(
            preview["connection_id"], project=expected["project_id"],
            repository=expected["repository"], operation=op, actor=actor)

        now = time.time()
        # Phase 1 — claim the lease under the write lock. Re-read the connection row
        # in-transaction (authoritative revocation/drift fence, TOCTOU-safe), then move
        # issued -> materializing exactly once and COMMIT before leaving the lock. Once
        # committed as materializing, a later mint failure can only fence the lease, never
        # resurrect it to issued — so a lost-response mint cannot be retried into a second
        # token.
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._expire_leases_in(c, now)
            lease = c.execute(
                "SELECT * FROM scm_leases WHERE lease_id=?", (str(lease_id).strip(),),
            ).fetchone()
            if not lease:
                raise SCMLeaseError(
                    "scm_lease_not_available", "SCM lease is not available", status_code=404)
            if lease["state"] == "expired" or float(lease["expires_at"] or 0) <= now:
                c.commit()
                raise SCMLeaseError(
                    "scm_lease_expired", "SCM lease has expired", status_code=409)
            if lease["state"] != "issued":
                raise SCMLeaseError(
                    "scm_lease_already_consumed",
                    "SCM lease cannot be materialized again", status_code=409)
            if not decision.get("allowed"):
                self._fence_lease_in(
                    c, lease, actor="switchboard/scm-lease",
                    reason="scm_connection_revoked", now=now)
                c.commit()
                raise SCMLeaseError(
                    "scm_connection_not_authorized",
                    "SCM connection no longer authorizes this repository operation",
                    status_code=409,
                )
            connection = c.execute(
                "SELECT lifecycle_state, installation_version, installation_ref "
                "FROM scm_connections WHERE connection_id=?",
                (lease["connection_id"],),
            ).fetchone()
            if not connection or str(connection["lifecycle_state"] or "") != "active":
                self._fence_lease_in(
                    c, lease, actor="switchboard/scm-lease",
                    reason="scm_connection_revoked", now=now)
                c.commit()
                raise SCMLeaseError(
                    "scm_connection_not_authorized",
                    "SCM connection no longer authorizes this repository operation",
                    status_code=409,
                )
            if int(connection["installation_version"] or 0) != int(lease["installation_version"] or 0):
                self._fence_lease_in(
                    c, lease, actor="switchboard/scm-lease",
                    reason="scm_installation_drift", now=now)
                c.commit()
                raise SCMLeaseError(
                    "scm_installation_drift",
                    "SCM installation rotated since the lease was issued", status_code=409)
            changed = c.execute(
                "UPDATE scm_leases SET state='materializing', materializing_at=? "
                "WHERE lease_id=? AND state='issued' AND expires_at>?",
                (now, lease["lease_id"], now),
            ).rowcount
            if changed != 1:
                raise SCMLeaseError(
                    "scm_lease_already_consumed",
                    "SCM lease cannot be materialized again", status_code=409)
            lease_id_str = str(lease["lease_id"])
            installation_ref = str(connection["installation_ref"] or "")
            phase = str(lease["phase"] or "")
            operations = tuple(_json_list(lease["operations_json"]))
            token_ttl = max(1, min(int(float(lease["expires_at"]) - now), MAX_LEASE_TTL_SECONDS))

        # Phase 2 — mint OUTSIDE the write lock. The mint is a network exchange; holding
        # the single-writer registry lock across it would stall every other control-plane
        # write. Fail closed on ANY exception: fence the still-"materializing" lease in its
        # own short transaction and surface a stable error.
        active_minter = minter or self._minter
        try:
            minted = active_minter.mint(
                installation_ref=installation_ref, repository=expected["repository"],
                phase=phase, operations=operations, ttl_seconds=token_ttl)
        except Exception as exc:  # noqa: BLE001 — a mint failure must never leave the lease reusable
            with _registry_conn() as c:
                c.execute("BEGIN IMMEDIATE")
                failed = c.execute(
                    "SELECT * FROM scm_leases WHERE lease_id=?", (lease_id_str,),
                ).fetchone()
                if failed:
                    self._fence_lease_in(
                        c, failed, actor="switchboard/scm-lease",
                        reason="materialization_failed", now=time.time())
            if isinstance(exc, SCMLeaseError):
                raise SCMLeaseError(exc.code, exc.message, status_code=exc.status_code) from exc
            raise SCMLeaseError(
                "scm_materialization_failed",
                "SCM token materialization failed", status_code=503) from exc

        # Phase 3 — record the single materialization. If the lease was fenced or expired
        # while minting, do NOT return the token (its own short TTL bounds the exposure).
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute(
                "SELECT * FROM scm_leases WHERE lease_id=?", (lease_id_str,),
            ).fetchone()
            if not current or current["state"] != "materializing":
                raise SCMLeaseError(
                    "scm_lease_already_consumed",
                    "SCM lease was fenced during materialization", status_code=409)
            self._event_in(
                c, current, "materialized", actor=actor, operation=op,
                details={"installation_version": int(current["installation_version"] or 0),
                         "phase": current["phase"], "operation": op,
                         "token_returned_once": True}, now=time.time())
        return minted

    def activate_materialized_lease(self, lease_id: str, *, actor: str,
                                    principal: SCMLeasePrincipal) -> dict[str, Any]:
        """Mark a materialized lease active once the repository process has started."""
        self._prepare()
        now = time.time()
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._expire_leases_in(c, now)
            lease = c.execute(
                "SELECT * FROM scm_leases WHERE lease_id=?", (str(lease_id or "").strip(),),
            ).fetchone()
            if not lease or lease["state"] != "materializing":
                if lease and lease["state"] == "expired":
                    c.commit()
                raise SCMLeaseError(
                    "scm_lease_activation_denied",
                    "SCM lease is not awaiting activation", status_code=409)
            if (lease["acquiring_principal_id"] != principal.principal_id
                    or lease["acquiring_principal_kind"] != principal.principal_kind):
                raise SCMLeaseError(
                    "scm_lease_principal_binding_mismatch",
                    "SCM lease principal binding failed", status_code=403)
            changed = c.execute(
                "UPDATE scm_leases SET state='active', activated_at=? "
                "WHERE lease_id=? AND state='materializing' AND expires_at>?",
                (now, lease["lease_id"], now),
            ).rowcount
            if changed != 1:
                raise SCMLeaseError(
                    "scm_lease_activation_denied",
                    "SCM lease activation failed", status_code=409)
            self._event_in(c, lease, "lease_activated", actor=actor, now=now)
            current = c.execute(
                "SELECT * FROM scm_leases WHERE lease_id=?", (lease["lease_id"],),
            ).fetchone()
            return self._public_lease(current, now=now)

    def release_lease(self, lease_id: str, *, project: str, actor: str, reason: str,
                      principal: SCMLeasePrincipal) -> dict[str, Any]:
        self._prepare()
        reason = _clip_reason(reason)
        now = time.time()
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._expire_leases_in(c, now)
            lease = c.execute(
                "SELECT * FROM scm_leases WHERE lease_id=?", (str(lease_id or "").strip(),),
            ).fetchone()
            if not lease or lease["project_id"] != str(project or "").strip().lower():
                raise SCMLeaseError(
                    "scm_lease_not_available", "SCM lease is not available", status_code=404)
            is_acquirer = (
                lease["acquiring_principal_id"] == principal.principal_id
                and lease["acquiring_principal_kind"] == principal.principal_kind)
            if not (principal.admin or (is_acquirer and principal.can_use_credentials())):
                raise SCMLeaseError(
                    "scm_lease_release_denied",
                    "caller cannot release this SCM lease", status_code=403)
            if lease["state"] in LIVE_LEASE_STATES:
                c.execute(
                    "UPDATE scm_leases SET state='released', released_at=?, released_by=?, "
                    "release_reason=? WHERE lease_id=? "
                    "AND state IN ('issued','materializing','active')",
                    (now, actor, reason or "released", lease["lease_id"]),
                )
                self._event_in(c, lease, "lease_released", actor=actor,
                               reason_code=reason or "released", now=now)
            current = c.execute(
                "SELECT * FROM scm_leases WHERE lease_id=?", (lease["lease_id"],),
            ).fetchone()
            return self._public_lease(current, now=now)

    def fence_leases_for_execution(self, *, project: str, task_id: str, generation: str,
                                   host_id: str, runner_session_id: str, claim_id: str,
                                   wake_id: str, actor: str, reason: str) -> int:
        """Fence every live lease bound to one superseded/lost/cancelled execution."""
        self._prepare()
        now = time.time()
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute(
                "SELECT * FROM scm_leases WHERE project_id=? AND task_id=? AND generation=? "
                "AND host_id=? AND runner_session_id=? AND claim_id=? AND wake_id=? "
                "AND state IN ('issued','materializing','active')",
                (
                    str(project or "").strip().lower(), str(task_id or "").strip(),
                    str(generation or "").strip(), str(host_id or "").strip(),
                    str(runner_session_id or "").strip(), str(claim_id or "").strip(),
                    str(wake_id or "").strip(),
                ),
            ).fetchall()
            for row in rows:
                self._fence_lease_in(c, row, actor=actor, reason=reason or "execution_fenced",
                                     now=now)
            return len(rows)

    def fence_leases_for_connection(self, connection_id: str, *, actor: str,
                                    reason: str) -> int:
        """Fence every live lease bound to a connection (rotation/revocation/deletion)."""
        self._prepare()
        now = time.time()
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute(
                "SELECT * FROM scm_leases WHERE connection_id=? "
                "AND state IN ('issued','materializing','active')",
                (str(connection_id or "").strip(),),
            ).fetchall()
            for row in rows:
                self._fence_lease_in(c, row, actor=actor,
                                     reason=reason or "scm_connection_revoked", now=now)
            return len(rows)

    def cleanup_expired_leases(self) -> int:
        self._prepare()
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            return self._expire_leases_in(c, time.time())


default_scm_lease_repository = SCMLeaseRepository()

__all__ = [
    "MAX_LEASE_TTL_SECONDS",
    "SCM_LEASE_SCHEMA",
    "SCMLeaseRepository",
    "default_scm_lease_repository",
]
