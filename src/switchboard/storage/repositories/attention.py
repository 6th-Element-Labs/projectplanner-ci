"""Project-scoped persistence for provider attention requests and decisions."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any, Mapping, Optional, Sequence

from constants import DEFAULT_PROJECT
from db.connection import _conn, _write_through
from switchboard.domain.attention import assert_attention_transition

ATTENTION_REQUEST_SCHEMA = "switchboard.attention_request.v1"
ATTENTION_DECISION_SCHEMA = "switchboard.attention_decision.v1"
ATTENTION_AUDIT_SCHEMA = "switchboard.attention_audit.v1"


class AttentionStoreError(ValueError):
    """Typed storage-contract failure."""

    def __init__(self, code: str, message: str, *,
                 details: Optional[dict[str, Any]] = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self), **self.details}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hash_payload(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(dict(value)).encode("utf-8")).hexdigest()


def _decode(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value) if value is not None else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def _request_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema": ATTENTION_REQUEST_SCHEMA,
        "request_id": row["request_id"],
        "project_id": row["project_id"],
        "task_id": row["task_id"],
        "provider": row["provider"],
        "host_id": row["host_id"],
        "runner_session_id": row["runner_session_id"],
        "work_session_id": row["work_session_id"],
        "provider_request_id": row["provider_request_id"],
        "schema_version": row["schema_version"],
        "prompt": row["prompt"],
        "context": _decode(row["context_json"], {}),
        "choices": _decode(row["choices_json"], []),
        "recommended_default": _decode(row["recommended_default_json"], None),
        "status": row["status"],
        "version": row["version"],
        "idempotency_key": row["idempotency_key"],
        "expires_at": row["expires_at"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "decided_at": row["decided_at"],
        "delivery_started_at": row["delivery_started_at"],
        "resolved_at": row["resolved_at"],
        "terminal_reason": row["terminal_reason"],
        "delivery_receipt": _decode(row["delivery_receipt_json"], None),
    }


def _decision_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema": ATTENTION_DECISION_SCHEMA,
        "decision_id": row["decision_id"],
        "request_id": row["request_id"],
        "request_version": row["request_version"],
        "idempotency_key": row["idempotency_key"],
        "choice": _decode(row["choice_json"], None),
        "actor": row["actor"],
        "actor_principal_id": row["actor_principal_id"],
        "created_at": row["created_at"],
        "delivery_claimed_by": row["delivery_claimed_by"],
        "delivery_claimed_at": row["delivery_claimed_at"],
        "delivered_at": row["delivered_at"],
        "delivery_receipt": _decode(row["delivery_receipt_json"], None),
    }


def _event_in(c: sqlite3.Connection, *, request_id: str, event_type: str,
              from_status: Optional[str], to_status: str, request_version: int,
              actor: str, payload: Any, created_at: float) -> None:
    sequence = c.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM attention_events WHERE request_id=?",
        (request_id,),
    ).fetchone()[0]
    c.execute(
        "INSERT INTO attention_events(request_id, sequence, event_type, from_status, "
        "to_status, request_version, actor, payload_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (request_id, sequence, event_type, from_status, to_status, request_version,
         actor, _canonical_json(payload or {}), created_at),
    )


def create_attention_request_in(
    c: sqlite3.Connection, data: Mapping[str, Any], *, project: str, actor: str,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Idempotently persist an immutable provider question."""
    payload = dict(data or {})
    required = ("provider", "provider_request_id", "schema_version", "prompt",
                "choices", "idempotency_key")
    missing = [field for field in required if payload.get(field) in (None, "", [])]
    if missing:
        raise AttentionStoreError("attention_request_invalid", "required fields are missing",
                                  details={"missing": missing})
    frozen = {
        "project_id": project,
        "task_id": payload.get("task_id"),
        "provider": payload["provider"],
        "host_id": payload.get("host_id"),
        "runner_session_id": payload.get("runner_session_id"),
        "work_session_id": payload.get("work_session_id"),
        "provider_request_id": payload["provider_request_id"],
        "schema_version": payload["schema_version"],
        "prompt": payload["prompt"],
        "context": payload.get("context") or {},
        "choices": payload["choices"],
        "recommended_default": payload.get("recommended_default"),
        "expires_at": payload.get("expires_at"),
    }
    request_hash = _hash_payload(frozen)
    existing = c.execute(
        "SELECT * FROM attention_requests WHERE project_id=? AND idempotency_key=?",
        (project, payload["idempotency_key"]),
    ).fetchone()
    if existing:
        if existing["request_hash"] != request_hash:
            raise AttentionStoreError(
                "attention_idempotency_conflict",
                "idempotency key was already used with a different request",
            )
        return {"created": False, "idempotent_replay": True,
                "request": _request_from_row(existing)}
    provider_existing = c.execute(
        "SELECT * FROM attention_requests "
        "WHERE project_id=? AND provider=? AND provider_request_id=?",
        (project, payload["provider"], payload["provider_request_id"]),
    ).fetchone()
    if provider_existing:
        raise AttentionStoreError(
            "attention_provider_request_conflict",
            "provider_request_id already identifies a different request",
        )
    created_at = float(now if now is not None else time.time())
    request_id = str(payload.get("request_id") or f"attention-{uuid.uuid4().hex}")
    c.execute(
        "INSERT INTO attention_requests("
        "request_id, project_id, task_id, provider, host_id, runner_session_id, "
        "work_session_id, provider_request_id, schema_version, prompt, context_json, "
        "choices_json, recommended_default_json, status, version, idempotency_key, "
        "request_hash, expires_at, created_by, created_at, updated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',1,?,?,?,?,?,?)",
        (
            request_id, project, payload.get("task_id"), payload["provider"],
            payload.get("host_id"), payload.get("runner_session_id"),
            payload.get("work_session_id"), payload["provider_request_id"],
            payload["schema_version"], payload["prompt"],
            _canonical_json(payload.get("context") or {}),
            _canonical_json(payload["choices"]),
            (_canonical_json(payload["recommended_default"])
             if payload.get("recommended_default") is not None else None),
            payload["idempotency_key"], request_hash, payload.get("expires_at"),
            actor, created_at, created_at,
        ),
    )
    _event_in(c, request_id=request_id, event_type="attention.requested",
              from_status=None, to_status="pending", request_version=1, actor=actor,
              payload={"schema_version": payload["schema_version"]}, created_at=created_at)
    row = c.execute(
        "SELECT * FROM attention_requests WHERE request_id=?", (request_id,)
    ).fetchone()
    return {"created": True, "idempotent_replay": False, "request": _request_from_row(row)}


def record_attention_decision_in(
    c: sqlite3.Connection, request_id: str, data: Mapping[str, Any], *,
    actor: str, actor_principal_id: str = "", project: str = "",
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Record one version-fenced decision and advance pending -> decision_recorded."""
    payload = dict(data or {})
    if "expected_version" not in payload or "choice" not in payload or not payload.get(
        "idempotency_key"
    ):
        raise AttentionStoreError(
            "attention_decision_invalid",
            "expected_version, choice, and idempotency_key are required",
        )
    request = c.execute(
        "SELECT * FROM attention_requests WHERE request_id=?"
        + (" AND project_id=?" if project else ""),
        (request_id, project) if project else (request_id,),
    ).fetchone()
    if not request:
        raise AttentionStoreError("attention_request_not_found", "request does not exist")
    decision_frozen = {
        "request_id": request_id,
        "expected_version": payload["expected_version"],
        "choice": payload["choice"],
        "actor": actor,
        "actor_principal_id": actor_principal_id or None,
    }
    decision_hash = _hash_payload(decision_frozen)
    existing = c.execute(
        "SELECT * FROM attention_decisions WHERE request_id=? AND idempotency_key=?",
        (request_id, payload["idempotency_key"]),
    ).fetchone()
    if existing:
        if existing["decision_hash"] != decision_hash:
            raise AttentionStoreError(
                "attention_decision_idempotency_conflict",
                "decision idempotency key was reused with different content",
            )
        return {"created": False, "idempotent_replay": True,
                "decision": _decision_from_row(existing),
                "request": _request_from_row(request)}
    if request["status"] != "pending" or request["version"] != payload["expected_version"]:
        raise AttentionStoreError(
            "stale_attention_decision",
            "request status or version no longer accepts this decision",
            details={"current_status": request["status"],
                     "current_version": request["version"]},
        )
    now_value = float(now if now is not None else time.time())
    next_version = request["version"] + 1
    updated = c.execute(
        "UPDATE attention_requests SET status='decision_recorded', version=?, "
        "decided_at=?, updated_at=? WHERE request_id=? AND status='pending' AND version=?",
        (next_version, now_value, now_value, request_id, request["version"]),
    )
    if updated.rowcount != 1:
        raise AttentionStoreError(
            "stale_attention_decision", "another writer changed the request first")
    decision_id = str(payload.get("decision_id") or f"attention-decision-{uuid.uuid4().hex}")
    c.execute(
        "INSERT INTO attention_decisions("
        "decision_id, request_id, request_version, idempotency_key, decision_hash, "
        "choice_json, actor, actor_principal_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (decision_id, request_id, request["version"], payload["idempotency_key"],
         decision_hash, _canonical_json(payload["choice"]), actor,
         actor_principal_id or None, now_value),
    )
    _event_in(c, request_id=request_id, event_type="attention.decision_recorded",
              from_status="pending", to_status="decision_recorded",
              request_version=next_version, actor=actor,
              payload={"decision_id": decision_id}, created_at=now_value)
    decision = c.execute(
        "SELECT * FROM attention_decisions WHERE decision_id=?", (decision_id,)
    ).fetchone()
    request = c.execute(
        "SELECT * FROM attention_requests WHERE request_id=?", (request_id,)
    ).fetchone()
    return {"created": True, "idempotent_replay": False,
            "decision": _decision_from_row(decision), "request": _request_from_row(request)}


def transition_attention_request_in(
    c: sqlite3.Connection, request_id: str, *, expected_version: int,
    target_status: str, actor: str, reason: str = "", delivery_receipt: Any = None,
    delivery_claimed_by: str = "", project: str = "",
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Advance a request using optimistic concurrency and append an audit event."""
    row = c.execute(
        "SELECT * FROM attention_requests WHERE request_id=?"
        + (" AND project_id=?" if project else ""),
        (request_id, project) if project else (request_id,),
    ).fetchone()
    if not row:
        raise AttentionStoreError("attention_request_not_found", "request does not exist")
    if row["version"] != expected_version:
        raise AttentionStoreError(
            "stale_attention_version", "request version changed",
            details={"current_version": row["version"], "current_status": row["status"]},
        )
    assert_attention_transition(row["status"], target_status)
    if target_status == "expired":
        now_check = float(now if now is not None else time.time())
        if row["expires_at"] is None or row["expires_at"] > now_check:
            raise AttentionStoreError(
                "attention_not_expired", "request expiry has not elapsed")
    now_value = float(now if now is not None else time.time())
    next_version = expected_version + 1
    delivery_started_at = now_value if target_status == "delivering" else row[
        "delivery_started_at"]
    resolved_at = now_value if target_status == "resolved" else row["resolved_at"]
    receipt_json = (_canonical_json(delivery_receipt)
                    if delivery_receipt is not None else row["delivery_receipt_json"])
    updated = c.execute(
        "UPDATE attention_requests SET status=?, version=?, updated_at=?, "
        "delivery_started_at=?, resolved_at=?, terminal_reason=?, "
        "delivery_receipt_json=? WHERE request_id=? AND version=? AND status=?",
        (target_status, next_version, now_value, delivery_started_at, resolved_at,
         reason or row["terminal_reason"], receipt_json, request_id, expected_version,
         row["status"]),
    )
    if updated.rowcount != 1:
        raise AttentionStoreError(
            "stale_attention_version", "another writer changed the request first")
    if target_status == "delivering":
        c.execute(
            "UPDATE attention_decisions SET delivery_claimed_by=?, delivery_claimed_at=? "
            "WHERE decision_id=(SELECT decision_id FROM attention_decisions "
            "WHERE request_id=? ORDER BY created_at DESC LIMIT 1)",
            (delivery_claimed_by or actor, now_value, request_id),
        )
    elif target_status == "resolved":
        c.execute(
            "UPDATE attention_decisions SET delivered_at=?, delivery_receipt_json=? "
            "WHERE decision_id=(SELECT decision_id FROM attention_decisions "
            "WHERE request_id=? ORDER BY created_at DESC LIMIT 1)",
            (now_value, receipt_json, request_id),
        )
    _event_in(
        c, request_id=request_id, event_type=f"attention.{target_status}",
        from_status=row["status"], to_status=target_status, request_version=next_version,
        actor=actor, payload={"reason": reason, "delivery_receipt": delivery_receipt},
        created_at=now_value,
    )
    result = c.execute(
        "SELECT * FROM attention_requests WHERE request_id=?", (request_id,)
    ).fetchone()
    return _request_from_row(result)


def reconstruct_attention_audit_in(
    c: sqlite3.Connection, request_id: str, *, project: str = "",
) -> dict[str, Any]:
    request = c.execute(
        "SELECT * FROM attention_requests WHERE request_id=?"
        + (" AND project_id=?" if project else ""),
        (request_id, project) if project else (request_id,),
    ).fetchone()
    if not request:
        raise AttentionStoreError("attention_request_not_found", "request does not exist")
    decisions = [
        _decision_from_row(row) for row in c.execute(
            "SELECT * FROM attention_decisions WHERE request_id=? ORDER BY created_at",
            (request_id,),
        ).fetchall()
    ]
    events = [{
        "sequence": row["sequence"],
        "event_type": row["event_type"],
        "from_status": row["from_status"],
        "to_status": row["to_status"],
        "request_version": row["request_version"],
        "actor": row["actor"],
        "payload": _decode(row["payload_json"], {}),
        "created_at": row["created_at"],
    } for row in c.execute(
        "SELECT * FROM attention_events WHERE request_id=? ORDER BY sequence",
        (request_id,),
    ).fetchall()]
    return {"schema": ATTENTION_AUDIT_SCHEMA, "request": _request_from_row(request),
            "decisions": decisions, "events": events}


def _operator_queue_clause(now: float) -> tuple[str, Sequence[Any]]:
    """Single predicate shared by the operator queue and bell count."""
    return (
        "project_id=? AND status='pending' "
        "AND (expires_at IS NULL OR expires_at>?)",
        (now,),
    )


def list_attention_requests_in(
    c: sqlite3.Connection, *, project: str, now: Optional[float] = None,
    limit: int = 100, offset: int = 0,
) -> list[dict[str, Any]]:
    now_value = float(now if now is not None else time.time())
    clause, tail = _operator_queue_clause(now_value)
    rows = c.execute(
        f"SELECT * FROM attention_requests WHERE {clause} "
        "ORDER BY created_at, request_id LIMIT ? OFFSET ?",
        (project, *tail, max(1, min(int(limit), 500)), max(0, int(offset))),
    ).fetchall()
    return [_request_from_row(row) for row in rows]


def count_attention_requests_in(
    c: sqlite3.Connection, *, project: str, now: Optional[float] = None,
) -> int:
    now_value = float(now if now is not None else time.time())
    clause, tail = _operator_queue_clause(now_value)
    return int(c.execute(
        f"SELECT COUNT(*) FROM attention_requests WHERE {clause}",
        (project, *tail),
    ).fetchone()[0])


def get_attention_request_in(
    c: sqlite3.Connection, request_id: str, *, project: str,
) -> dict[str, Any]:
    row = c.execute(
        "SELECT * FROM attention_requests WHERE request_id=? AND project_id=?",
        (request_id, project),
    ).fetchone()
    if not row:
        raise AttentionStoreError("attention_request_not_found", "request does not exist")
    return _request_from_row(row)


def claim_attention_decision_in(
    c: sqlite3.Connection, *, project: str, host_id: str, actor: str,
    provider: str = "", request_id: str = "", now: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Atomically claim the oldest matching recorded decision for one Agent Host."""
    filters = ["project_id=?", "status='decision_recorded'", "host_id=?"]
    values: list[Any] = [project, host_id]
    if provider:
        filters.append("provider=?")
        values.append(provider)
    if request_id:
        filters.append("request_id=?")
        values.append(request_id)
    row = c.execute(
        "SELECT * FROM attention_requests WHERE "
        + " AND ".join(filters)
        + " ORDER BY decided_at, request_id LIMIT 1",
        values,
    ).fetchone()
    if not row:
        return None
    request = transition_attention_request_in(
        c, row["request_id"], expected_version=row["version"],
        target_status="delivering", actor=actor, delivery_claimed_by=host_id,
        project=project, now=now,
    )
    decision = c.execute(
        "SELECT * FROM attention_decisions WHERE request_id=? "
        "ORDER BY created_at DESC LIMIT 1",
        (row["request_id"],),
    ).fetchone()
    return {"request": request, "decision": _decision_from_row(decision)}


class AttentionRepository:
    """Write-serialized project repository used by future thin adapters."""

    def create_request(self, data: Mapping[str, Any], *, actor: str,
                       project: str = DEFAULT_PROJECT) -> dict[str, Any]:
        return _write_through(
            project, lambda: self._create_request(data, actor=actor, project=project))

    @staticmethod
    def _create_request(data: Mapping[str, Any], *, actor: str,
                        project: str) -> dict[str, Any]:
        with _conn(project) as c:
            return create_attention_request_in(c, data, project=project, actor=actor)

    def record_decision(self, request_id: str, data: Mapping[str, Any], *, actor: str,
                        actor_principal_id: str = "",
                        project: str = DEFAULT_PROJECT) -> dict[str, Any]:
        return _write_through(
            project, lambda: self._record_decision(
                request_id, data, actor=actor, actor_principal_id=actor_principal_id,
                project=project))

    @staticmethod
    def _record_decision(request_id: str, data: Mapping[str, Any], *, actor: str,
                         actor_principal_id: str, project: str) -> dict[str, Any]:
        with _conn(project) as c:
            return record_attention_decision_in(
                c, request_id, data, actor=actor,
                actor_principal_id=actor_principal_id, project=project)

    def transition(self, request_id: str, *, expected_version: int,
                   target_status: str, actor: str, reason: str = "",
                   delivery_receipt: Any = None, delivery_claimed_by: str = "",
                   project: str = DEFAULT_PROJECT) -> dict[str, Any]:
        return _write_through(
            project, lambda: self._transition(
                request_id, expected_version=expected_version,
                target_status=target_status, actor=actor, reason=reason,
                delivery_receipt=delivery_receipt,
                delivery_claimed_by=delivery_claimed_by, project=project))

    @staticmethod
    def _transition(request_id: str, *, expected_version: int,
                    target_status: str, actor: str, reason: str,
                    delivery_receipt: Any, delivery_claimed_by: str,
                    project: str) -> dict[str, Any]:
        with _conn(project) as c:
            return transition_attention_request_in(
                c, request_id, expected_version=expected_version,
                target_status=target_status, actor=actor, reason=reason,
                delivery_receipt=delivery_receipt,
                delivery_claimed_by=delivery_claimed_by, project=project)

    def reconstruct_audit(self, request_id: str, *,
                          project: str = DEFAULT_PROJECT) -> dict[str, Any]:
        with _conn(project) as c:
            return reconstruct_attention_audit_in(c, request_id, project=project)

    def list_requests(self, *, project: str, now: Optional[float] = None,
                      limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        with _conn(project) as c:
            return list_attention_requests_in(
                c, project=project, now=now, limit=limit, offset=offset)

    def count_requests(self, *, project: str,
                       now: Optional[float] = None) -> int:
        with _conn(project) as c:
            return count_attention_requests_in(c, project=project, now=now)

    def get_request(self, request_id: str, *,
                    project: str) -> dict[str, Any]:
        with _conn(project) as c:
            return get_attention_request_in(c, request_id, project=project)

    def claim_decision(self, *, project: str, host_id: str, actor: str,
                       provider: str = "", request_id: str = "",
                       now: Optional[float] = None) -> Optional[dict[str, Any]]:
        return _write_through(
            project, lambda: self._claim_decision(
                project=project, host_id=host_id, actor=actor, provider=provider,
                request_id=request_id, now=now))

    @staticmethod
    def _claim_decision(*, project: str, host_id: str, actor: str,
                        provider: str, request_id: str,
                        now: Optional[float]) -> Optional[dict[str, Any]]:
        with _conn(project) as c:
            return claim_attention_decision_in(
                c, project=project, host_id=host_id, actor=actor,
                provider=provider, request_id=request_id, now=now)


default_attention_repository = AttentionRepository()

__all__ = [
    "ATTENTION_AUDIT_SCHEMA",
    "ATTENTION_DECISION_SCHEMA",
    "ATTENTION_REQUEST_SCHEMA",
    "AttentionRepository",
    "AttentionStoreError",
    "claim_attention_decision_in",
    "count_attention_requests_in",
    "create_attention_request_in",
    "default_attention_repository",
    "get_attention_request_in",
    "list_attention_requests_in",
    "record_attention_decision_in",
    "reconstruct_attention_audit_in",
    "transition_attention_request_in",
]
