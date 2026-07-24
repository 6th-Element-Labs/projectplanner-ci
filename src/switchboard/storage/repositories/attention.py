"""Project-scoped persistence for provider attention requests and decisions."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any, Callable, Mapping, Optional, Sequence

from constants import DEFAULT_PROJECT
from db.connection import _conn, _write_through
from switchboard.domain.attention import assert_attention_transition

ATTENTION_REQUEST_SCHEMA = "switchboard.attention_request.v1"
ATTENTION_DECISION_SCHEMA = "switchboard.attention_decision.v1"
ATTENTION_AUDIT_SCHEMA = "switchboard.attention_audit.v1"
COMPLETION_CLOSEOUT_SCHEMA = "switchboard.completion_human_closeout.v1"
COMPLETION_WAKE_SCHEMA = "switchboard.completion_wake.v1"
COMPLETION_RESUME_RECEIPT_SCHEMA = "switchboard.completion_resume_receipt.v1"
COMPLETION_PROVIDER = "switchboard.completion"


def is_reserved_completion_provider(provider: Any) -> bool:
    """Return whether a provider name belongs to the completion-owner namespace."""
    value = str(provider or "").strip()
    return value == COMPLETION_PROVIDER or value.startswith(
        f"{COMPLETION_PROVIDER}."
    )


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


def _completion_wake_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema": COMPLETION_WAKE_SCHEMA,
        "wake_id": row["wake_id"],
        "project_id": row["project_id"],
        "request_id": row["request_id"],
        "decision_id": row["decision_id"],
        "task_id": row["task_id"],
        "deliverable_id": row["deliverable_id"],
        "completion_run_id": row["completion_run_id"],
        "state_version": int(row["state_version"]),
        "head_sha": row["head_sha"],
        "pr_number": row["pr_number"],
        "choice_id": row["choice_id"],
        "status": row["status"],
        "attempt_count": int(row["attempt_count"]),
        "available_at": row["available_at"],
        "claimed_by": row["claimed_by"],
        "lease_expires_at": row["lease_expires_at"],
        "wake_receipt": _decode(row["wake_receipt_json"], None),
        "completion_receipt": _decode(row["completion_receipt_json"], None),
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _completion_wake_for_request_in(
    c: sqlite3.Connection, request_id: str,
) -> Optional[dict[str, Any]]:
    row = c.execute(
        "SELECT * FROM attention_completion_wakes WHERE request_id=?",
        (request_id,),
    ).fetchone()
    return _completion_wake_from_row(row) if row else None


def _selected_choice(
    choices: Sequence[Any], selected: Any,
) -> tuple[str, str]:
    selected_id = (
        str(selected.get("id") or "").strip()
        if isinstance(selected, Mapping)
        else str(selected or "").strip()
    )
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        if str(choice.get("id") or "").strip() == selected_id:
            return selected_id, str(choice.get("effect") or "").strip()
    return selected_id, ""


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
    context = frozen["context"]
    if payload.get("auto_proceed") is True or context.get("auto_proceed") is True:
        raise AttentionStoreError(
            "attention_auto_proceed_forbidden",
            "attention requests require an explicit operator decision")
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
    choices = _decode(request["choices_json"], [])
    selected = payload.get("choice")
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
        replay = {
            "created": False,
            "idempotent_replay": True,
            "decision": _decision_from_row(existing),
            "request": _request_from_row(request),
        }
        wake = _completion_wake_for_request_in(c, request_id)
        if wake:
            replay["completion_wake"] = wake
        return replay
    now_value = float(now if now is not None else time.time())
    if request["expires_at"] is not None and request["expires_at"] <= now_value:
        if request["status"] == "pending":
            transition_attention_request_in(
                c, request_id, expected_version=request["version"],
                target_status="expired", actor=actor,
                reason="decision_rejected_after_expiry", project=project,
                now=now_value,
            )
        raise AttentionStoreError(
            "attention_request_expired",
            "request expired before the operator decision was recorded",
            details={"current_status": "expired",
                     "current_version": request["version"] + 1},
        )
    context = _decode(request["context_json"], {})
    frozen_head = str(context.get("head_sha") or "").strip()
    frozen_pr_number = str(context.get("pr_number") or "").strip()
    if request["task_id"] and frozen_head:
        try:
            current = c.execute(
                "SELECT head_sha, pr_number FROM task_git_state WHERE task_id=?",
                (request["task_id"],),
            ).fetchone()
        except sqlite3.OperationalError:
            current = None
        current_head = str(current["head_sha"] or "").strip() if current else ""
        if not current_head:
            raise AttentionStoreError(
                "attention_head_unverifiable",
                "current task head is unavailable; decision remains pending",
                details={"frozen_head_sha": frozen_head})
        if current_head != frozen_head:
            if request["status"] == "pending":
                transition_attention_request_in(
                    c, request_id, expected_version=request["version"],
                    target_status="cancelled", actor=actor,
                    reason="exact_head_binding_changed", project=project,
                    now=now_value,
                )
            raise AttentionStoreError(
                "stale_attention_head",
                "request is bound to a different task head",
                details={"frozen_head_sha": frozen_head,
                         "current_head_sha": current_head},
            )
        current_pr_number = str(current["pr_number"] or "").strip()
        if frozen_pr_number and current_pr_number != frozen_pr_number:
            if request["status"] == "pending":
                transition_attention_request_in(
                    c, request_id, expected_version=request["version"],
                    target_status="cancelled", actor=actor,
                    reason="exact_pr_binding_changed", project=project,
                    now=now_value,
                )
            raise AttentionStoreError(
                "stale_attention_pr",
                "request is bound to a different pull request",
                details={
                    "frozen_pr_number": frozen_pr_number,
                    "current_pr_number": current_pr_number,
                },
            )
    completion_binding: dict[str, Any] = {}
    if (
        request["status"] == "pending"
        and str(request["provider"] or "") == COMPLETION_PROVIDER
        and str(request["schema_version"] or "") == COMPLETION_CLOSEOUT_SCHEMA
    ):
        task_id = str(request["task_id"] or "").strip().upper()
        context_task = str(context.get("task_id") or "").strip().upper()
        run_id = str(context.get("completion_run_id") or "").strip()
        state_version = int(context.get("state_version") or 0)
        if (
            context.get("schema") != COMPLETION_CLOSEOUT_SCHEMA
            or not task_id
            or context_task != task_id
            or not run_id
            or state_version < 1
            or not frozen_head
        ):
            raise AttentionStoreError(
                "completion_attention_binding_invalid",
                "completion decision lacks an exact task/run/head binding",
            )
        task = c.execute(
            "SELECT status FROM tasks WHERE task_id=?", (task_id,),
        ).fetchone()
        run = c.execute(
            "SELECT run_id, state_version, head_sha, pr_number, route "
            "FROM completion_runs WHERE task_id=?",
            (task_id,),
        ).fetchone()
        completion_binding = {
            "task_id": task_id,
            "run_id": run_id,
            "state_version": state_version,
            "head_sha": frozen_head,
            "pr_number": frozen_pr_number,
        }
        stale_completion = (
            not task
            or str(task["status"] or "") in {"Done", "Cancelled", "Canceled"}
            or not run
            or str(run["run_id"] or "") != run_id
            or int(run["state_version"] or 0) != state_version
            or str(run["head_sha"] or "") != frozen_head
            or str(run["pr_number"] or "") != frozen_pr_number
            or str(run["route"] or "") != "human"
        )
        if stale_completion:
            transition_attention_request_in(
                c, request_id, expected_version=request["version"],
                target_status="cancelled", actor=actor,
                reason="exact_completion_run_binding_changed", project=project,
                now=now_value,
            )
            raise AttentionStoreError(
                "stale_attention_completion_run",
                "request is bound to a completion run that is no longer current",
                details={
                    "completion_run_id": run_id,
                    "frozen_state_version": state_version,
                    "current_state_version": (
                        int(run["state_version"] or 0) if run else None
                    ),
                    "current_route": str(run["route"] or "") if run else None,
                },
            )
    selected_id = (
        str(selected.get("id") or "") if isinstance(selected, Mapping)
        else str(selected)
    )
    allowed_ids = {
        (str(choice.get("id") or "") if isinstance(choice, Mapping) else str(choice))
        for choice in choices
    }
    if allowed_ids and selected_id not in allowed_ids:
        raise AttentionStoreError(
            "attention_choice_not_allowed",
            "decision must select one of the frozen request choices",
            details={"allowed_choice_ids": sorted(allowed_ids)},
        )
    if request["status"] != "pending" or request["version"] != payload["expected_version"]:
        raise AttentionStoreError(
            "stale_attention_decision",
            "request status or version no longer accepts this decision",
            details={"current_status": request["status"],
                     "current_version": request["version"]},
        )
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

    if (
        str(request["provider"] or "") == COMPLETION_PROVIDER
        and str(request["schema_version"] or "") == COMPLETION_CLOSEOUT_SCHEMA
    ):
        selected_id, selected_effect = _selected_choice(choices, selected)
        task_id = str(completion_binding.get("task_id") or "").strip().upper()
        run_id = str(completion_binding.get("run_id") or "").strip()
        state_version = int(completion_binding.get("state_version") or 0)
        if selected_effect == "resume_assessment":
            wake_id = f"completion-wake-{uuid.uuid4().hex}"
            c.execute(
                "INSERT INTO attention_completion_wakes("
                "wake_id, project_id, request_id, decision_id, task_id, deliverable_id, "
                "completion_run_id, state_version, head_sha, pr_number, choice_id, "
                "status, attempt_count, available_at, created_at, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',0,?,?,?)",
                (
                    wake_id, project or request["project_id"], request_id,
                    decision_id, task_id,
                    str(context.get("deliverable_id") or "").strip(),
                    run_id, state_version, frozen_head,
                    context.get("pr_number"), selected_id, now_value, now_value,
                    now_value,
                ),
            )
        elif selected_effect == "remain_blocked":
            hold_receipt = {
                "schema": COMPLETION_RESUME_RECEIPT_SCHEMA,
                "effect": "remain_blocked",
                "request_id": request_id,
                "decision_id": decision_id,
                "task_id": task_id,
                "completion_run_id": run_id,
                "state_version": state_version,
                "head_sha": frozen_head,
                "choice_id": selected_id,
                "verified": True,
            }
            delivering = transition_attention_request_in(
                c, request_id, expected_version=next_version,
                target_status="delivering", actor=actor,
                reason="human_hold_recorded",
                delivery_claimed_by="switchboard/completion-owner",
                project=project, now=now_value,
                allow_completion_owner=True,
            )
            transition_attention_request_in(
                c, request_id, expected_version=delivering["version"],
                target_status="resolved", actor=actor,
                reason="human_hold_applied", delivery_receipt=hold_receipt,
                project=project, now=now_value,
                allow_completion_owner=True,
            )
        else:
            raise AttentionStoreError(
                "completion_attention_choice_invalid",
                "completion decisions must resume assessment or remain blocked",
                details={"choice_id": selected_id, "effect": selected_effect},
            )

    decision = c.execute(
        "SELECT * FROM attention_decisions WHERE decision_id=?", (decision_id,)
    ).fetchone()
    request = c.execute(
        "SELECT * FROM attention_requests WHERE request_id=?", (request_id,)
    ).fetchone()
    result = {
        "created": True,
        "idempotent_replay": False,
        "decision": _decision_from_row(decision),
        "request": _request_from_row(request),
    }
    wake = _completion_wake_for_request_in(c, request_id)
    if wake:
        result["completion_wake"] = wake
    return result


def transition_attention_request_in(
    c: sqlite3.Connection, request_id: str, *, expected_version: int,
    target_status: str, actor: str, reason: str = "", delivery_receipt: Any = None,
    delivery_claimed_by: str = "", project: str = "",
    now: Optional[float] = None, allow_completion_owner: bool = False,
) -> dict[str, Any]:
    """Advance a request using optimistic concurrency and append an audit event."""
    row = c.execute(
        "SELECT * FROM attention_requests WHERE request_id=?"
        + (" AND project_id=?" if project else ""),
        (request_id, project) if project else (request_id,),
    ).fetchone()
    if not row:
        raise AttentionStoreError("attention_request_not_found", "request does not exist")
    if (
        str(row["provider"] or "") == COMPLETION_PROVIDER
        and target_status in {"delivering", "resolved"}
        and not allow_completion_owner
    ):
        raise AttentionStoreError(
            "attention_completion_owner_required",
            "completion delivery transitions require the fenced completion owner",
        )
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


def reconcile_attention_lifecycle_in(
    c: sqlite3.Connection, *, project: str, now: Optional[float] = None,
    delivery_timeout_s: float = 300.0,
) -> dict[str, int]:
    """Materialize expiry and abandoned delivery as audited terminal states."""
    now_value = float(now if now is not None else time.time())
    expired = c.execute(
        "SELECT request_id, version FROM attention_requests "
        "WHERE project_id=? AND status IN ('pending','decision_recorded') "
        "AND expires_at IS NOT NULL AND expires_at<=?",
        (project, now_value),
    ).fetchall()
    expired = [
        row for row in expired
        if not c.execute(
            "SELECT 1 FROM attention_completion_wakes "
            "WHERE request_id=? AND status IN "
            "('pending','claimed','accepted','failed')",
            (row["request_id"],),
        ).fetchone()
    ]
    for row in expired:
        transition_attention_request_in(
            c, row["request_id"], expected_version=row["version"],
            target_status="expired", actor="attention-reconcile",
            reason="request_expiry_elapsed", project=project, now=now_value)
    abandoned = c.execute(
        "SELECT request_id, version FROM attention_requests "
        "WHERE project_id=? AND status='delivering' "
        "AND delivery_started_at IS NOT NULL AND delivery_started_at<=?",
        (project, now_value - max(1.0, float(delivery_timeout_s))),
    ).fetchall()
    abandoned = [
        row for row in abandoned
        if not c.execute(
            "SELECT 1 FROM attention_completion_wakes "
            "WHERE request_id=? AND status='accepted'",
            (row["request_id"],),
        ).fetchone()
    ]
    for row in abandoned:
        transition_attention_request_in(
            c, row["request_id"], expected_version=row["version"],
            target_status="orphaned", actor="attention-reconcile",
            reason="delivery_claim_timeout", project=project, now=now_value)
    return {"expired": len(expired), "orphaned": len(abandoned)}


def _operator_queue_clause(now: float) -> tuple[str, Sequence[Any]]:
    """Single predicate shared by the operator queue and bell count."""
    return (
        "project_id=? AND (status='pending' OR (status='decision_recorded' "
        "AND EXISTS (SELECT 1 FROM attention_completion_wakes w "
        "WHERE w.request_id=attention_requests.request_id "
        "AND w.status IN ('pending','failed')))) "
        "AND (expires_at IS NULL OR expires_at>?)",
        (now,),
    )


def list_attention_requests_in(
    c: sqlite3.Connection, *, project: str, now: Optional[float] = None,
    limit: int = 100, offset: int = 0,
) -> list[dict[str, Any]]:
    now_value = float(now if now is not None else time.time())
    reconcile_attention_lifecycle_in(c, project=project, now=now_value)
    clause, tail = _operator_queue_clause(now_value)
    rows = c.execute(
        f"SELECT * FROM attention_requests WHERE {clause} "
        "ORDER BY created_at, request_id LIMIT ? OFFSET ?",
        (project, *tail, max(1, min(int(limit), 500)), max(0, int(offset))),
    ).fetchall()
    items = []
    for row in rows:
        item = _request_from_row(row)
        wake = _completion_wake_for_request_in(c, row["request_id"])
        if wake:
            item["completion_wake"] = wake
        items.append(item)
    return items


def count_attention_requests_in(
    c: sqlite3.Connection, *, project: str, now: Optional[float] = None,
) -> int:
    now_value = float(now if now is not None else time.time())
    reconcile_attention_lifecycle_in(c, project=project, now=now_value)
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
    result = _request_from_row(row)
    wake = _completion_wake_for_request_in(c, request_id)
    if wake:
        result["completion_wake"] = wake
    return result


def claim_attention_decision_in(
    c: sqlite3.Connection, *, project: str, host_id: str, actor: str,
    provider: str = "", request_id: str = "", runner_session_id: str = "",
    work_session_id: str = "", now: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Atomically claim the oldest matching recorded decision for one Agent Host."""
    now_value = float(now if now is not None else time.time())
    reconcile_attention_lifecycle_in(c, project=project, now=now_value)
    filters = ["project_id=?", "status='decision_recorded'", "host_id=?"]
    values: list[Any] = [project, host_id]
    filters.append("provider=?")
    values.append(provider)
    if request_id:
        filters.append("request_id=?")
        values.append(request_id)
    filters.append("COALESCE(runner_session_id, '')=?")
    values.append(runner_session_id)
    filters.append("COALESCE(work_session_id, '')=?")
    values.append(work_session_id)
    filters.append("(expires_at IS NULL OR expires_at>?)")
    values.append(now_value)
    row = c.execute(
        "SELECT * FROM attention_requests WHERE "
        + " AND ".join(filters)
        + " ORDER BY decided_at, request_id LIMIT 1",
        values,
    ).fetchone()
    if not row:
        return None
    if str(row["provider"] or "") == COMPLETION_PROVIDER:
        raise AttentionStoreError(
            "attention_completion_owner_required",
            "completion decisions are delivered only by the fenced completion owner",
        )
    request = transition_attention_request_in(
        c, row["request_id"], expected_version=row["version"],
        target_status="delivering", actor=actor, delivery_claimed_by=host_id,
        project=project, now=now_value,
    )
    decision = c.execute(
        "SELECT * FROM attention_decisions WHERE request_id=? "
        "ORDER BY created_at DESC LIMIT 1",
        (row["request_id"],),
    ).fetchone()
    return {"request": request, "decision": _decision_from_row(decision)}


def _scope_covering_completion_wake_in(
    c: sqlite3.Connection, scope_id: str, wake: sqlite3.Row,
    *, now: Optional[float] = None,
) -> Optional[sqlite3.Row]:
    scope = c.execute(
        "SELECT * FROM autopilot_scopes WHERE scope_id=?",
        (scope_id,),
    ).fetchone()
    now_value = float(now if now is not None else time.time())
    if (
        not scope
        or str(scope["status"] or "") != "active"
        or not str(scope["lease_id"] or "").strip()
        or not str(scope["holder_agent_id"] or "").strip()
        or int(scope["generation"] or 0) < 1
        or int(scope["fence_epoch"] or 0) < 1
        or scope["expires_at"] is None
        or float(scope["expires_at"] or 0) <= now_value
    ):
        return None
    if str(scope["scope_type"] or "") == "task":
        if (
            str(scope["task_project"] or "") != str(wake["project_id"] or "")
            or str(scope["task_id"] or "").strip().upper()
            != str(wake["task_id"] or "").strip().upper()
            or str(scope["deliverable_id"] or "")
            != str(wake["deliverable_id"] or "")
        ):
            return None
        return scope
    if (
        str(scope["scope_type"] or "") != "deliverable"
        or str(scope["deliverable_id"] or "")
        != str(wake["deliverable_id"] or "")
    ):
        return None
    linked = c.execute(
        "SELECT 1 FROM deliverable_task_links "
        "WHERE deliverable_id=? AND project_id=? AND task_id=? LIMIT 1",
        (
            scope["deliverable_id"],
            wake["project_id"],
            str(wake["task_id"] or "").strip().upper(),
        ),
    ).fetchone()
    return scope if linked else None


def _cancel_completion_wake_in(
    c: sqlite3.Connection, row: sqlite3.Row, *, reason: str, actor: str,
    now: float,
) -> dict[str, Any]:
    c.execute(
        "UPDATE attention_completion_wakes SET status='cancelled', last_error=?, "
        "claimed_by=NULL, lease_expires_at=NULL, updated_at=? WHERE wake_id=?",
        (reason, now, row["wake_id"]),
    )
    request = c.execute(
        "SELECT * FROM attention_requests WHERE request_id=?",
        (row["request_id"],),
    ).fetchone()
    request_result = None
    if request and request["status"] in {"decision_recorded", "delivering"}:
        request_result = transition_attention_request_in(
            c, row["request_id"], expected_version=request["version"],
            target_status="cancelled", actor=actor, reason=reason,
            project=row["project_id"], now=now,
            allow_completion_owner=request["status"] == "delivering",
        )
    current = c.execute(
        "SELECT * FROM attention_completion_wakes WHERE wake_id=?",
        (row["wake_id"],),
    ).fetchone()
    result = _completion_wake_from_row(current)
    if request_result:
        result["request"] = request_result
    return result


def _claim_completion_wake_in(
    c: sqlite3.Connection, *, project: str, request_id: str,
    actor: str, now: float, lease_s: float,
) -> dict[str, Any]:
    params: list[Any] = [project]
    where = "project_id=?"
    if request_id:
        where += " AND request_id=?"
        params.append(request_id)
    row = c.execute(
        "SELECT * FROM attention_completion_wakes WHERE " + where
        + " ORDER BY created_at LIMIT 1",
        params,
    ).fetchone()
    if not row:
        return {"status": "not_found", "request_id": request_id or None}
    request = c.execute(
        "SELECT * FROM attention_requests WHERE request_id=? AND project_id=?",
        (row["request_id"], project),
    ).fetchone()
    task = c.execute(
        "SELECT task_id, status FROM tasks WHERE task_id=?", (row["task_id"],),
    ).fetchone()
    git = c.execute(
        "SELECT head_sha, pr_number FROM task_git_state WHERE task_id=?",
        (row["task_id"],),
    ).fetchone()
    run = c.execute(
        "SELECT run_id, state_version, head_sha, pr_number, route, evidence_refs_json "
        "FROM completion_runs WHERE task_id=?",
        (row["task_id"],),
    ).fetchone()
    accepted = row["status"] == "accepted"
    stored_wake_receipt = _decode(row["wake_receipt_json"], {})
    resume_state_version = int(
        stored_wake_receipt.get("resume_state_version") or 0
    )
    resuming_accepted_wake = (
        resume_state_version > int(row["state_version"] or 0)
    )
    expected_request_status = (
        "delivering"
        if accepted or resuming_accepted_wake
        else "decision_recorded"
    )
    stale_reason = ""
    if not request or request["status"] != expected_request_status:
        stale_reason = "completion_attention_request_changed"
    elif not task:
        stale_reason = "completion_attention_task_missing"
    elif str(task["status"] or "") in {"Done", "Cancelled", "Canceled"}:
        stale_reason = "completion_attention_task_terminal"
    elif not git or str(git["head_sha"] or "") != str(row["head_sha"] or ""):
        stale_reason = "completion_attention_head_changed"
    elif str((git or {})["pr_number"] or "") != str(row["pr_number"] or ""):
        stale_reason = "completion_attention_pr_changed"
    elif (
        not run
        or str(run["run_id"] or "") != str(row["completion_run_id"] or "")
        or str(run["head_sha"] or "") != str(row["head_sha"] or "")
        or str(run["pr_number"] or "") != str(row["pr_number"] or "")
    ):
        stale_reason = "completion_attention_run_changed"
    elif resuming_accepted_wake and (
        resume_state_version <= int(row["state_version"] or 0)
        or int(run["state_version"] or 0) < resume_state_version
    ):
        stale_reason = "completion_attention_resume_version_invalid"
    elif not resuming_accepted_wake and (
        int(run["state_version"] or 0) != int(row["state_version"] or 0)
        or str(run["route"] or "") != "human"
    ):
        stale_reason = "completion_attention_run_changed"
    if stale_reason:
        return _cancel_completion_wake_in(
            c, row, reason=stale_reason, actor=actor, now=now)
    if row["status"] == "accepted":
        scope_id = str(stored_wake_receipt.get("scope_id") or "").strip()
        scope = (
            _scope_covering_completion_wake_in(c, scope_id, row, now=now)
            if scope_id else None
        )
        if scope:
            c.execute(
                "UPDATE attention_completion_wakes SET updated_at=? "
                "WHERE wake_id=? AND status='accepted'",
                (now, row["wake_id"]),
            )
            result = _completion_wake_from_row(row)
            result["updated_at"] = now
            result["idempotent_replay"] = True
            return result
        c.execute(
            "UPDATE attention_completion_wakes SET status='failed', "
            "available_at=?, last_error=?, updated_at=? WHERE wake_id=? "
            "AND status='accepted'",
            (now, "completion_wake_scope_not_live", now, row["wake_id"]),
        )
        row = c.execute(
            "SELECT * FROM attention_completion_wakes WHERE wake_id=?",
            (row["wake_id"],),
        ).fetchone()
    if row["status"] in {"resolved", "cancelled"}:
        result = _completion_wake_from_row(row)
        result["idempotent_replay"] = True
        return result
    if (
        row["status"] == "claimed"
        and row["lease_expires_at"] is not None
        and float(row["lease_expires_at"]) > now
    ):
        result = _completion_wake_from_row(row)
        result["in_flight"] = True
        return result
    if row["available_at"] is not None and float(row["available_at"]) > now:
        result = _completion_wake_from_row(row)
        result["backoff"] = True
        return result

    lease_expires_at = now + max(1.0, float(lease_s))
    updated = c.execute(
        "UPDATE attention_completion_wakes SET status='claimed', "
        "attempt_count=attempt_count+1, claimed_by=?, lease_expires_at=?, "
        "last_error=NULL, updated_at=? WHERE wake_id=? "
        "AND status IN ('pending','failed','claimed')",
        (actor, lease_expires_at, now, row["wake_id"]),
    )
    if updated.rowcount != 1:
        return {"status": "claim_lost", "wake_id": row["wake_id"]}
    claimed = c.execute(
        "SELECT * FROM attention_completion_wakes WHERE wake_id=?",
        (row["wake_id"],),
    ).fetchone()
    result = _completion_wake_from_row(claimed)
    result["claimed"] = True
    return result


def attempt_completion_wake(
    request_id: str, *, wake_completion_owner: Callable[[Mapping[str, Any]], Any],
    actor: str, project: str = DEFAULT_PROJECT, now: Optional[float] = None,
    lease_s: float = 60.0,
) -> dict[str, Any]:
    """Issue one durable completion wake; crash-safe and idempotent."""
    now_value = float(now if now is not None else time.time())

    def claim() -> dict[str, Any]:
        with _conn(project) as c:
            return _claim_completion_wake_in(
                c, project=project, request_id=request_id, actor=actor,
                now=now_value, lease_s=lease_s)

    claimed = _write_through(project, claim)
    if not claimed.get("claimed"):
        return claimed
    wake_payload = {
        "schema": COMPLETION_WAKE_SCHEMA,
        "wake_id": claimed["wake_id"],
        "request_id": claimed["request_id"],
        "decision_id": claimed["decision_id"],
        "task_id": claimed["task_id"],
        "deliverable_id": claimed["deliverable_id"],
        "completion_run_id": claimed["completion_run_id"],
        "state_version": claimed["state_version"],
        "head_sha": claimed["head_sha"],
        "pr_number": claimed["pr_number"],
        "choice_id": claimed["choice_id"],
        "action": "rehydrate_and_classify",
    }
    try:
        raw_receipt = wake_completion_owner(wake_payload)
        wake_receipt = (
            dict(raw_receipt) if isinstance(raw_receipt, Mapping)
            else {"result": raw_receipt}
        )
        failure = str(
            wake_receipt.get("error")
            or (
                wake_receipt.get("reason")
                if wake_receipt.get("refused")
                else ""
            )
        ).strip()
        if failure or not str(wake_receipt.get("scope_id") or "").strip():
            raise RuntimeError(failure or "completion wake returned no scope_id")
    except Exception as exc:
        def fail() -> dict[str, Any]:
            with _conn(project) as c:
                row = c.execute(
                    "SELECT * FROM attention_completion_wakes WHERE wake_id=?",
                    (claimed["wake_id"],),
                ).fetchone()
                if not row:
                    return {"status": "not_found", "wake_id": claimed["wake_id"]}
                attempts = int(row["attempt_count"] or 1)
                available_at = now_value + min(300.0, 5.0 * (2 ** min(attempts - 1, 6)))
                c.execute(
                    "UPDATE attention_completion_wakes SET status='failed', "
                    "available_at=?, claimed_by=NULL, lease_expires_at=NULL, "
                    "last_error=?, updated_at=? WHERE wake_id=? AND status='claimed'",
                    (available_at, str(exc), now_value, row["wake_id"]),
                )
                failed = c.execute(
                    "SELECT * FROM attention_completion_wakes WHERE wake_id=?",
                    (row["wake_id"],),
                ).fetchone()
                return _completion_wake_from_row(failed)

        return _write_through(project, fail)

    def accept() -> dict[str, Any]:
        with _conn(project) as c:
            row = c.execute(
                "SELECT * FROM attention_completion_wakes WHERE wake_id=?",
                (claimed["wake_id"],),
            ).fetchone()
            if not row:
                return {"status": "not_found", "wake_id": claimed["wake_id"]}
            if row["status"] == "accepted":
                result = _completion_wake_from_row(row)
                result["idempotent_replay"] = True
                return result
            if row["status"] != "claimed":
                return _completion_wake_from_row(row)
            prior_wake_receipt = _decode(row["wake_receipt_json"], {})
            prior_resume_state_version = int(
                prior_wake_receipt.get("resume_state_version") or 0
            )
            previously_advanced = (
                prior_resume_state_version > int(row["state_version"] or 0)
            )
            request = c.execute(
                "SELECT * FROM attention_requests WHERE request_id=?",
                (row["request_id"],),
            ).fetchone()
            expected_request_status = (
                "delivering" if previously_advanced else "decision_recorded"
            )
            if not request or request["status"] != expected_request_status:
                return _cancel_completion_wake_in(
                    c, row, reason="completion_attention_request_changed",
                    actor=actor, now=now_value)
            scope_id = str(wake_receipt.get("scope_id") or "").strip()
            if not _scope_covering_completion_wake_in(
                c, scope_id, row, now=now_value
            ):
                c.execute(
                    "UPDATE attention_completion_wakes SET status='failed', "
                    "available_at=?, claimed_by=NULL, lease_expires_at=NULL, "
                    "last_error=?, updated_at=? WHERE wake_id=? AND status='claimed'",
                    (
                        now_value + 5.0,
                        "completion_wake_scope_binding_invalid",
                        now_value,
                        row["wake_id"],
                    ),
                )
                failed = c.execute(
                    "SELECT * FROM attention_completion_wakes WHERE wake_id=?",
                    (row["wake_id"],),
                ).fetchone()
                return _completion_wake_from_row(failed)
            current_run = c.execute(
                "SELECT state_version, route, evidence_refs_json FROM completion_runs "
                "WHERE run_id=? AND task_id=? AND head_sha=? "
                "AND CAST(pr_number AS TEXT)=?",
                (
                    row["completion_run_id"],
                    row["task_id"],
                    row["head_sha"],
                    str(row["pr_number"] or ""),
                ),
            ).fetchone()
            if not current_run:
                return _cancel_completion_wake_in(
                    c, row, reason="completion_attention_run_changed",
                    actor=actor, now=now_value)
            evidence_refs = _decode(current_run["evidence_refs_json"], {})
            if previously_advanced:
                raw_recorded_wake = evidence_refs.get("human_decision_wake")
                recorded_wake = (
                    dict(raw_recorded_wake)
                    if isinstance(raw_recorded_wake, Mapping)
                    else {}
                )
                if (
                    int(current_run["state_version"] or 0)
                    < prior_resume_state_version
                    or str(recorded_wake.get("wake_id") or "") != row["wake_id"]
                    or str(recorded_wake.get("decision_id") or "")
                    != row["decision_id"]
                ):
                    return _cancel_completion_wake_in(
                        c, row, reason="completion_attention_run_changed",
                        actor=actor, now=now_value)
                advanced_state_version = prior_resume_state_version
            else:
                if (
                    int(current_run["state_version"] or 0)
                    != int(row["state_version"] or 0)
                    or str(current_run["route"] or "") != "human"
                ):
                    return _cancel_completion_wake_in(
                        c, row, reason="completion_attention_run_changed",
                        actor=actor, now=now_value)
                evidence_refs["human_decision_wake"] = {
                    "schema": COMPLETION_WAKE_SCHEMA,
                    "wake_id": row["wake_id"],
                    "request_id": row["request_id"],
                    "decision_id": row["decision_id"],
                    "choice_id": row["choice_id"],
                    "accepted_at": now_value,
                }
                advanced_state_version = int(row["state_version"] or 0) + 1
                advanced = c.execute(
                    "UPDATE completion_runs SET state_version=?, evidence_refs_json=?, "
                    "updated_at=?, actor=? WHERE run_id=? AND task_id=? "
                    "AND state_version=? AND route='human'",
                    (
                        advanced_state_version,
                        _canonical_json(evidence_refs),
                        now_value,
                        actor,
                        row["completion_run_id"],
                        row["task_id"],
                        row["state_version"],
                    ),
                )
                if advanced.rowcount != 1:
                    return _cancel_completion_wake_in(
                        c, row, reason="completion_attention_run_changed",
                        actor=actor, now=now_value)
            accepted_wake_receipt = {**prior_wake_receipt, **wake_receipt}
            accepted_wake_receipt["resume_state_version"] = advanced_state_version
            c.execute(
                "UPDATE attention_completion_wakes SET status='accepted', "
                "wake_receipt_json=?, claimed_by=NULL, lease_expires_at=NULL, "
                "last_error=NULL, updated_at=? WHERE wake_id=? AND status='claimed'",
                (_canonical_json(accepted_wake_receipt), now_value, row["wake_id"]),
            )
            if request["status"] == "decision_recorded":
                request_result = transition_attention_request_in(
                    c, row["request_id"], expected_version=request["version"],
                    target_status="delivering", actor=actor,
                    reason="completion_wake_accepted",
                    delivery_claimed_by="switchboard/completion-owner",
                    project=project, now=now_value,
                    allow_completion_owner=True,
                )
            else:
                request_result = _request_from_row(request)
            accepted = c.execute(
                "SELECT * FROM attention_completion_wakes WHERE wake_id=?",
                (row["wake_id"],),
            ).fetchone()
            result = _completion_wake_from_row(accepted)
            result["request"] = request_result
            return result

    return _write_through(project, accept)


def drain_completion_wakes(
    *, wake_completion_owner: Callable[[Mapping[str, Any]], Any],
    actor: str, project: str = DEFAULT_PROJECT, limit: int = 20,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Retry pending/failed/expired-lease wake rows across process restarts."""
    now_value = float(now if now is not None else time.time())
    with _conn(project) as c:
        rows = c.execute(
            "SELECT request_id FROM attention_completion_wakes "
            "WHERE project_id=? AND ("
            "(status IN ('pending','failed') AND available_at<=?) OR "
            "(status='claimed' AND COALESCE(lease_expires_at,0)<=?) OR "
            "(status='accepted' AND updated_at<=?)) "
            "ORDER BY CASE WHEN status='accepted' THEN 1 ELSE 0 END, "
            "available_at, created_at LIMIT ?",
            (
                project, now_value, now_value, now_value - 60.0,
                max(1, min(int(limit), 200)),
            ),
        ).fetchall()
    results = [
        attempt_completion_wake(
            row["request_id"], wake_completion_owner=wake_completion_owner,
            actor=actor, project=project, now=now_value)
        for row in rows
    ]
    return {
        "schema": "switchboard.completion_wake_drain.v1",
        "checked": len(results),
        "accepted": sum(row.get("status") == "accepted" for row in results),
        "failed": sum(row.get("status") == "failed" for row in results),
        "cancelled": sum(row.get("status") == "cancelled" for row in results),
        "results": results,
    }


def complete_completion_wake_for_tick(
    task_id: str, *, tick: Mapping[str, Any],
    scope_authority: Mapping[str, Any], actor: str,
    project: str = DEFAULT_PROJECT,
) -> dict[str, Any]:
    """Atomically bind an accepted wake to one exact completion-owner tick."""
    task_id = str(task_id or "").strip().upper()
    tick_row = dict(tick or {})
    snapshot = dict(tick_row.get("snapshot") or {})
    decision = dict(tick_row.get("decision") or {})
    plan = dict(tick_row.get("plan") or {})
    execution = dict(tick_row.get("execution") or {})
    run = dict(execution.get("run") or {})
    effect_receipt = dict(execution.get("receipt") or {})
    authority = dict(scope_authority or {})
    now = time.time()
    with _conn(project) as c:
        row = c.execute(
            "SELECT * FROM attention_completion_wakes "
            "WHERE project_id=? AND task_id=? AND status IN ('accepted','resolved') "
            "ORDER BY CASE WHEN status='accepted' THEN 0 ELSE 1 END, "
            "created_at DESC LIMIT 1",
            (project, task_id),
        ).fetchone()
        if not row:
            return {"status": "not_applicable", "task_id": task_id}
        if row["status"] == "resolved":
            result = _completion_wake_from_row(row)
            result["idempotent_replay"] = True
            return result
        scope_id = str(authority.get("scope_id") or "").strip()
        generation = int(authority.get("generation") or 0)
        fence_epoch = int(authority.get("fence_epoch") or 0)
        wake_receipt = _decode(row["wake_receipt_json"], {})
        scope = (
            _scope_covering_completion_wake_in(c, scope_id, row, now=now)
            if scope_id else None
        )
        authority_valid = bool(
            authority.get("schema")
            == "switchboard.autopilot_scope_authority.v1"
            and scope
            and str(scope["status"] or "") == "active"
            and str(scope["lease_id"] or "")
            == str(authority.get("lease_id") or "")
            and str(scope["holder_agent_id"] or "")
            == str(authority.get("holder_agent_id") or "")
            and int(scope["generation"] or 0) == generation
            and int(scope["fence_epoch"] or 0) == fence_epoch
            and float(scope["expires_at"] or 0) > now
        )
        if (
            tick_row.get("schema") != "switchboard.completion_tick.v1"
            or str(tick_row.get("task_id") or "").strip().upper() != task_id
            or snapshot.get("schema") != "switchboard.completion_snapshot.v1"
            or str(snapshot.get("task_id") or "").strip().upper() != task_id
            or str(snapshot.get("head_sha") or "").strip() != row["head_sha"]
            or str(snapshot.get("pr_number") or "") != str(row["pr_number"] or "")
            or decision.get("schema") != "switchboard.completion_decision.v1"
            or plan.get("schema") != "switchboard.completion_effect.v1"
            or str(plan.get("task_id") or "").strip().upper() != task_id
            or str(plan.get("head_sha") or "").strip() != row["head_sha"]
            or str(plan.get("pr_number") or "") != str(row["pr_number"] or "")
            or str(run.get("run_id") or "") != row["completion_run_id"]
            or int(run.get("state_version") or 0) <= int(row["state_version"])
            or str(run.get("route") or "") != str(plan.get("route") or "")
            or effect_receipt.get("schema")
            != "switchboard.completion_effect_receipt.v1"
            or str(effect_receipt.get("effect") or "")
            != str(plan.get("effect") or "")
            or str(effect_receipt.get("idem_key") or "")
            != str(plan.get("idem_key") or "")
            or effect_receipt.get("verified") is not True
            or effect_receipt.get("pending") is True
            or not scope_id
            or str(wake_receipt.get("scope_id") or "").strip() != scope_id
            or not authority_valid
        ):
            return {
                "status": "blocked",
                "reason": "completion_tick_receipt_invalid",
                "wake_id": row["wake_id"],
            }
        receipt = {
            "schema": COMPLETION_RESUME_RECEIPT_SCHEMA,
            "effect": "resume_assessment",
            "verified": True,
            "wake_id": row["wake_id"],
            "request_id": row["request_id"],
            "decision_id": row["decision_id"],
            "task_id": task_id,
            "completion_run_id": row["completion_run_id"],
            "frozen_state_version": int(row["state_version"]),
            "result_state_version": int(run["state_version"]),
            "head_sha": row["head_sha"],
            "pr_number": row["pr_number"],
            "scope_id": scope_id,
            "generation": generation,
            "fence_epoch": fence_epoch,
            "effect_receipt": effect_receipt,
            "result_route": run.get("route"),
            "result_reason_code": run.get("reason_code"),
            "recorded_at": now,
        }
        followup_attention = None
        if str(run.get("route") or "").strip().lower() == "human":
            # The decision was genuinely consumed and reassessed, but the
            # authoritative classifier still needs a person. Close the old
            # question and atomically create a fresh, independently decidable
            # closeout instead of silently stranding Blocked(route=human).
            execution_attention = (
                dict(execution.get("attention") or {})
                if isinstance(execution.get("attention"), Mapping) else {}
            )
            execution_request = (
                dict(execution_attention.get("request") or {})
                if isinstance(execution_attention.get("request"), Mapping) else {}
            )
            followup_row = (
                c.execute(
                    "SELECT * FROM attention_requests WHERE request_id=? "
                    "AND project_id=?",
                    (
                        str(execution_request.get("request_id") or ""),
                        project,
                    ),
                ).fetchone()
                if execution_request.get("request_id") else None
            )
            followup_context = (
                _decode(followup_row["context_json"], {})
                if followup_row else {}
            )
            execution_followup_valid = bool(
                followup_row
                and followup_row["request_id"] != row["request_id"]
                and str(followup_row["provider"] or "") == COMPLETION_PROVIDER
                and str(followup_row["schema_version"] or "")
                == COMPLETION_CLOSEOUT_SCHEMA
                and str(followup_row["status"] or "") == "pending"
                and str(followup_row["task_id"] or "").strip().upper() == task_id
                and str(followup_context.get("completion_run_id") or "")
                == str(run.get("run_id") or "")
                and int(followup_context.get("state_version") or 0)
                == int(run.get("state_version") or 0)
                and str(followup_context.get("head_sha") or "") == row["head_sha"]
                and str(followup_context.get("pr_number") or "")
                == str(row["pr_number"] or "")
            )
            if execution_followup_valid:
                followup_attention = {
                    "created": bool(execution_attention.get("created")),
                    "idempotent_replay": bool(
                        execution_attention.get("idempotent_replay")),
                    "request": _request_from_row(followup_row),
                }
            else:
                from switchboard.domain.completion.human_closeout import (
                    build_human_closeout_request,
                )

                followup_data = build_human_closeout_request(
                    plan=plan,
                    decision=decision,
                    snapshot=snapshot,
                    run=run,
                )
                suffix = f":after:{row['decision_id']}"
                followup_data["provider_request_id"] = (
                    str(followup_data.get("provider_request_id") or "") + suffix
                )
                followup_data["idempotency_key"] = (
                    str(followup_data.get("idempotency_key") or "") + suffix
                )
                followup_attention = create_attention_request_in(
                    c, followup_data, project=project, actor=actor, now=now,
                )
            receipt["followup_attention_request_id"] = followup_attention[
                "request"
            ]["request_id"]
        request = c.execute(
            "SELECT * FROM attention_requests WHERE request_id=?",
            (row["request_id"],),
        ).fetchone()
        if not request or request["status"] != "delivering":
            return {
                "status": "blocked",
                "reason": "completion_attention_request_not_delivering",
                "wake_id": row["wake_id"],
            }
        c.execute(
            "UPDATE attention_completion_wakes SET status='resolved', "
            "completion_receipt_json=?, updated_at=? "
            "WHERE wake_id=? AND status='accepted'",
            (_canonical_json(receipt), now, row["wake_id"]),
        )
        request_result = transition_attention_request_in(
            c, row["request_id"], expected_version=request["version"],
            target_status="resolved", actor=actor,
            reason="completion_owner_tick_verified",
            delivery_receipt=receipt, project=project, now=now,
            allow_completion_owner=True,
        )
        resolved = c.execute(
            "SELECT * FROM attention_completion_wakes WHERE wake_id=?",
            (row["wake_id"],),
        ).fetchone()
        result = _completion_wake_from_row(resolved)
        result["request"] = request_result
        if followup_attention:
            result["followup_attention"] = followup_attention
        return result


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
            try:
                return record_attention_decision_in(
                    c, request_id, data, actor=actor,
                    actor_principal_id=actor_principal_id, project=project)
            except AttentionStoreError as exc:
                # Late decisions terminalize the request before reporting the
                # conflict. Preserve that audit write across the raised 409.
                if exc.code in {
                    "attention_request_expired", "stale_attention_head",
                    "stale_attention_pr", "stale_attention_completion_run",
                }:
                    c.commit()
                raise

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
                       runner_session_id: str = "", work_session_id: str = "",
                       now: Optional[float] = None) -> Optional[dict[str, Any]]:
        return _write_through(
            project, lambda: self._claim_decision(
                project=project, host_id=host_id, actor=actor, provider=provider,
                request_id=request_id, runner_session_id=runner_session_id,
                work_session_id=work_session_id, now=now))

    @staticmethod
    def _claim_decision(*, project: str, host_id: str, actor: str,
                        provider: str, request_id: str,
                        runner_session_id: str, work_session_id: str,
                        now: Optional[float]) -> Optional[dict[str, Any]]:
        with _conn(project) as c:
            return claim_attention_decision_in(
                c, project=project, host_id=host_id, actor=actor,
                provider=provider, request_id=request_id,
                runner_session_id=runner_session_id,
                work_session_id=work_session_id, now=now)


default_attention_repository = AttentionRepository()

__all__ = [
    "ATTENTION_AUDIT_SCHEMA",
    "ATTENTION_DECISION_SCHEMA",
    "ATTENTION_REQUEST_SCHEMA",
    "COMPLETION_CLOSEOUT_SCHEMA",
    "COMPLETION_PROVIDER",
    "COMPLETION_RESUME_RECEIPT_SCHEMA",
    "COMPLETION_WAKE_SCHEMA",
    "AttentionRepository",
    "AttentionStoreError",
    "claim_attention_decision_in",
    "attempt_completion_wake",
    "complete_completion_wake_for_tick",
    "count_attention_requests_in",
    "create_attention_request_in",
    "default_attention_repository",
    "drain_completion_wakes",
    "get_attention_request_in",
    "is_reserved_completion_provider",
    "list_attention_requests_in",
    "record_attention_decision_in",
    "reconcile_attention_lifecycle_in",
    "reconstruct_attention_audit_in",
    "transition_attention_request_in",
]
