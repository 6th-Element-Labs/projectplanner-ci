"""Atomic storage command for a bounded managed-execution finish turn.

All validation is read-only until a savepoint invokes the existing complete-claim
primitive.  Any refusal rolls the savepoint back, so a stale generation, bad receipt,
or dirty session cannot leave partial completion evidence behind.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

from db.connection import _conn
from switchboard.storage.repositories import claims as claims_repo


FINISH_TURN_RESULT_SCHEMA = "switchboard.claim.finish_turn_result.v1"
FINISH_TURN_EVIDENCE_SCHEMA = "switchboard.claim.finish_turn_evidence.v1"


def _reject(code: str, message: str, failure_class: str,
            **details: Any) -> dict[str, Any]:
    return {
        "schema": FINISH_TURN_RESULT_SCHEMA,
        "accepted": False,
        "error": code,
        "error_code": code,
        "reason": code,
        "failure_class": failure_class,
        "message": message,
        **details,
    }


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _pr_ready_proven(result: Mapping[str, Any], pr_number: int) -> bool:
    """Validate the persisted provider receipt without depending on application code."""
    try:
        observed_number = int(result.get("pr_number") or 0)
    except (TypeError, ValueError):
        observed_number = 0
    return (
        result.get("schema") == "switchboard.pr_ready.v1"
        and observed_number == pr_number
        and result.get("status") in {"already_ready", "marked_ready"}
        and result.get("is_draft") is False
    )


def _submission(
    *, task_id: str, claim_id: str, execution_id: str, generation: int,
    work_session_id: str, executed_test_run_id: str, evidence: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    bounded = {
        "schema": FINISH_TURN_EVIDENCE_SCHEMA,
        "task_id": task_id,
        "claim_id": claim_id,
        "execution_id": execution_id,
        "generation": generation,
        "work_session_id": work_session_id,
        "branch": str(evidence.get("branch") or ""),
        "head_sha": str(evidence.get("head_sha") or "").lower(),
        "pr_number": int(evidence.get("pr_number") or 0),
        "pr_url": str(evidence.get("pr_url") or ""),
        "executed_test_run_id": executed_test_run_id,
        "git_diff_check": str(evidence.get("git_diff_check") or ""),
    }
    digest = "sha256:" + hashlib.sha256(
        json.dumps(bounded, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    bounded["submission_digest"] = digest
    return bounded, digest


def _duplicate_receipt(
    handoff: Mapping[str, Any], *, digest: str, claim_id: str,
    task_id: str, execution_id: str, generation: int,
) -> dict[str, Any] | None:
    if not handoff:
        return None
    prior_evidence = _json_object(handoff.get("evidence"))
    prior = _json_object(prior_evidence.get("finish_turn"))
    prior_digest = str(prior.get("submission_digest") or "")
    if prior_digest != digest:
        return _reject(
            "finish_turn_idempotency_conflict",
            "This execution already accepted a different finish_turn submission.",
            "failed_gate",
            claim_id=claim_id,
            task_id=task_id,
            execution_id=execution_id,
            generation=generation,
            accepted_submission_digest=prior_digest or None,
            submitted_digest=digest,
        )
    acknowledged = bool(handoff.get("acknowledged_at"))
    return {
        "schema": FINISH_TURN_RESULT_SCHEMA,
        "accepted": True,
        "idempotent": True,
        "claim_id": claim_id,
        "task_id": task_id,
        "execution_id": execution_id,
        "runner_session_id": str(handoff.get("execution_id") or "") or None,
        "generation": generation,
        "submission_digest": digest,
        "stopping": not acknowledged,
        "pending_host_ack": not acknowledged,
        "lifecycle_phase": "handoff_acknowledged" if acknowledged else "stopping",
        "message": (
            "Identical finish_turn was already accepted; host acknowledgement is recorded."
            if acknowledged else
            "Identical finish_turn was already accepted and remains in the C3 stopping handoff."
        ),
    }


def _validate_and_finish_in(
    c: sqlite3.Connection,
    *,
    claim_id: str,
    task_id: str,
    execution_id: str,
    generation: int,
    work_session_id: str,
    executed_test_run_id: str,
    evidence: Mapping[str, Any],
    actor: str,
    project: str,
    mission_project: str,
) -> dict[str, Any]:
    evidence_obj = dict(evidence)
    bounded, digest = _submission(
        task_id=task_id,
        claim_id=claim_id,
        execution_id=execution_id,
        generation=generation,
        work_session_id=work_session_id,
        executed_test_run_id=executed_test_run_id,
        evidence=evidence_obj,
    )

    claim = c.execute(
        "SELECT * FROM task_claims WHERE id=?", (claim_id,)
    ).fetchone()
    if not claim:
        return _reject(
            "finish_turn_claim_not_found", "The claimed turn does not exist.",
            "missing_data", claim_id=claim_id)

    runner_id = str(claim["runner_session_id"] or "")
    runner = c.execute(
        "SELECT * FROM runner_sessions WHERE runner_session_id=?", (runner_id,)
    ).fetchone() if runner_id else None
    metadata = _json_object(runner["metadata_json"] if runner else None)
    duplicate = _duplicate_receipt(
        _json_object(metadata.get("completion_handoff")),
        digest=digest,
        claim_id=claim_id,
        task_id=task_id,
        execution_id=execution_id,
        generation=generation,
    )
    if duplicate is not None:
        return duplicate

    if str(claim["status"] or "") != "active":
        return _reject(
            "finish_turn_claim_not_active", "finish_turn requires the active bound claim.",
            "unbound_identity", claim_id=claim_id, status=claim["status"])
    if str(claim["task_id"] or "").upper() != task_id.upper():
        return _reject(
            "finish_turn_task_mismatch", "claim_id belongs to a different task.",
            "unbound_identity", claim_id=claim_id, task_id=task_id,
            claim_task_id=claim["task_id"])
    role = str(claim["execution_role"] or "").strip().lower()
    if (not runner or not runner_id or role not in {"implementation", "remediation"}
            or int(claim["execution_generation"] or 0) != generation):
        return _reject(
            "finish_turn_generation_mismatch",
            "The claim is not bound to this managed implementation generation.",
            "unbound_identity", claim_id=claim_id, execution_id=execution_id,
            generation=generation,
            claim_generation=int(claim["execution_generation"] or 0),
            execution_role=role or None)
    if (str(metadata.get("execution_id") or "") != execution_id
            or int(metadata.get("execution_generation") or 0) != generation
            or str(metadata.get("execution_role") or "").strip().lower() != role):
        return _reject(
            "finish_turn_execution_mismatch",
            "The live runner does not match execution_id, generation, and role.",
            "unbound_identity", claim_id=claim_id, execution_id=execution_id,
            generation=generation, runner_session_id=runner_id)

    lease = c.execute(
        "SELECT * FROM resource_leases WHERE id=? AND resource_type='execution' "
        "AND released_at IS NULL", (execution_id,)
    ).fetchone()
    epoch = int(claim["lease_epoch"] or 0)
    if (not lease or lease["task_id"] != task_id
            or int(lease["execution_generation"] or 0) != generation
            or str(lease["execution_role"] or "").strip().lower() != role
            or int(lease["fence_epoch"] or 0) != epoch
            or str(lease["lease_state"] or "") not in {"active", "reserved"}):
        return _reject(
            "finish_turn_execution_lease_mismatch",
            "The canonical execution lease no longer authorizes this generation.",
            "unbound_identity", execution_id=execution_id, generation=generation)

    session_row = c.execute(
        "SELECT * FROM work_sessions WHERE work_session_id=?", (work_session_id,)
    ).fetchone()
    session = claims_repo._store_facade()._work_session_row(session_row) \
        if session_row else None
    if (not session or session.get("claim_id") != claim_id
            or str(session.get("task_id") or "").upper() != task_id.upper()
            or str(session.get("agent_id") or "") != str(claim["agent_id"] or "")
            or str(session.get("status") or "").lower() != "active"
            or str(session.get("runner_session_id") or "") != runner_id
            or int(session.get("execution_generation") or 0) != generation):
        return _reject(
            "finish_turn_work_session_unbound",
            "The Work Session is not bound to the exact claim and generation.",
            "unbound_identity", work_session_id=work_session_id,
            claim_id=claim_id, generation=generation)

    branch = str(evidence_obj.get("branch") or "")
    head_sha = str(evidence_obj.get("head_sha") or "").lower()
    if branch != str(session.get("branch") or ""):
        return _reject(
            "finish_turn_branch_mismatch", "branch does not match the bound Work Session.",
            "stale_branch", branch=branch,
            work_session_branch=session.get("branch"))
    if head_sha != str(session.get("head_sha") or "").lower():
        return _reject(
            "finish_turn_head_mismatch", "head_sha does not match the bound Work Session.",
            "stale_branch", head_sha=head_sha,
            work_session_head_sha=session.get("head_sha"))

    pr_ready = _json_object(evidence_obj.get("pr_ready"))
    pr_number = int(evidence_obj.get("pr_number") or 0)
    if not _pr_ready_proven(pr_ready, pr_number):
        return _reject(
            "finish_turn_push_unproven",
            "Provider proof of a pushed, non-draft PR is required.",
            "missing_data", pr_number=pr_number, pr_ready=pr_ready)
    if (str(pr_ready.get("head_sha") or "").lower() != head_sha
            or str(pr_ready.get("head_ref") or "") != branch
            or str(pr_ready.get("pr_url") or "") != str(evidence_obj.get("pr_url") or "")):
        return _reject(
            "finish_turn_provider_identity_mismatch",
            "Provider PR URL, branch, or head does not match the bounded submission.",
            "stale_branch", pr_number=pr_number, head_sha=head_sha,
            branch=branch, pr_ready=pr_ready)

    hygiene = _json_object(session.get("hygiene"))
    preflight = _json_object(hygiene.get("repo_preflight"))
    blocking_findings = [
        item for item in (preflight.get("findings") or [])
        if isinstance(item, Mapping) and item.get("blocking") is True
    ]
    if (str(session.get("dirty_status") or "").lower() != "clean"
            or int(session.get("conflict_marker_count") or 0) != 0
            or preflight.get("dirty") is True
            or str(preflight.get("verdict") or "").lower() == "deny"
            or blocking_findings):
        return _reject(
            "finish_turn_dirty_diff",
            "The bound Work Session must be clean and conflict-free.",
            "failed_gate", work_session_id=work_session_id,
            dirty_status=session.get("dirty_status"),
            conflict_marker_count=session.get("conflict_marker_count"),
            preflight_verdict=preflight.get("verdict"))
    preflight_head = str(preflight.get("head_sha") or "").lower()
    if preflight_head and preflight_head != head_sha:
        return _reject(
            "finish_turn_preflight_head_mismatch",
            "The Work Session preflight belongs to a different head.",
            "stale_branch", preflight_head_sha=preflight_head,
            head_sha=head_sha)
    if str(evidence_obj.get("git_diff_check") or "").lower() != "passed":
        return _reject(
            "finish_turn_diff_check_missing",
            "git_diff_check must report passed.", "failed_gate")

    test_run = _json_object(hygiene.get("executed_test_run"))
    if str(test_run.get("run_id") or "") != executed_test_run_id:
        return _reject(
            "finish_turn_test_receipt_mismatch",
            "executed_test_run_id does not identify the bound Work Session receipt.",
            "missing_data", executed_test_run_id=executed_test_run_id,
            recorded_test_run_id=test_run.get("run_id"))
    test_gate = claims_repo._executed_test_run_gate(
        {"executed_test_run": test_run}, session)
    if not test_gate.get("ok"):
        return _reject(
            "finish_turn_tests_not_passed",
            "The referenced executed-test receipt is missing, failed, or stale.",
            "failed_gate", executed_test_gate=test_gate)

    evidence_obj["executed_test_run"] = test_gate["run"]
    evidence_obj["executed_test_run_id"] = executed_test_run_id
    evidence_obj["finish_turn"] = bounded

    c.execute("SAVEPOINT finish_turn_submission")
    result = claims_repo._complete_claim_impl(
        claim_id,
        evidence=evidence_obj,
        actor=actor,
        project=project,
        mission_project=mission_project,
        finalize=False,
        _connection=c,
    )
    accepted = bool(result.get("stopping") or result.get("completed"))
    if not accepted:
        c.execute("ROLLBACK TO finish_turn_submission")
        c.execute("RELEASE finish_turn_submission")
        return _reject(
            str(result.get("reason") or result.get("error") or "finish_turn_refused"),
            str(result.get("message") or "Existing completion gates refused finish_turn."),
            str(result.get("failure_class") or "failed_gate"),
            claim_id=claim_id, task_id=task_id,
            completion_gate=result,
        )
    c.execute("RELEASE finish_turn_submission")
    runner_session_id = str(result.get("execution_id") or runner_id)
    return {
        **result,
        "schema": FINISH_TURN_RESULT_SCHEMA,
        "accepted": True,
        "idempotent": bool(result.get("idempotent")),
        "execution_id": execution_id,
        "runner_session_id": runner_session_id,
        "generation": generation,
        "work_session_id": work_session_id,
        "executed_test_run_id": executed_test_run_id,
        "submission_digest": digest,
        "message": (
            "finish_turn accepted; the existing C3 surrender/host-ack handoff has begun."
        ),
    }


def finish_turn(
    *,
    claim_id: str,
    task_id: str,
    execution_id: str,
    generation: int,
    work_session_id: str,
    executed_test_run_id: str,
    evidence: Any,
    actor: str,
    project: str,
    mission_project: str = "",
) -> dict[str, Any]:
    """Validate and commit one exact finish submission in one write transaction."""
    evidence_obj = claims_repo._store_facade()._parse_evidence(evidence)

    def write() -> dict[str, Any]:
        with _conn(project) as c:
            return _validate_and_finish_in(
                c,
                claim_id=str(claim_id or "").strip(),
                task_id=str(task_id or "").strip().upper(),
                execution_id=str(execution_id or "").strip(),
                generation=int(generation or 0),
                work_session_id=str(work_session_id or "").strip(),
                executed_test_run_id=str(executed_test_run_id or "").strip(),
                evidence=evidence_obj,
                actor=actor,
                project=project,
                mission_project=mission_project,
            )

    return claims_repo._store_facade()._write_through(project, write)


__all__ = [
    "FINISH_TURN_EVIDENCE_SCHEMA",
    "FINISH_TURN_RESULT_SCHEMA",
    "finish_turn",
]
