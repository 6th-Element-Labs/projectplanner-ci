"""Durable completion_runs — current-state authority for PR-backed tasks.

``task_execution_completion_phases`` remains append-only history. This module owns
the single active current-state row per task (SIMPLIFY-22).
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Mapping, Optional

from constants import DEFAULT_PROJECT
from db.connection import _conn, _write_through
from switchboard.storage.repositories import task_completion


SCHEMA = "switchboard.completion_run.v1"
STATES = frozenset({
    "implementing", "waiting", "blocked", "ready_to_queue",
    "waiting_merge_queue", "reconciling", "done", "failed",
})
ROUTES = frozenset({
    "wait", "review_merge", "remediation", "coordination_retry",
    "human", "reconcile", "none",
})
TERMINAL_STATES = frozenset({"done", "failed"})
STALE_EVIDENCE_KEYS = ("ci", "review", "merge_gate", "queue", "merge_queue")


class CompletionRunError(ValueError):
    pass


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _row(row: Any) -> Optional[dict[str, Any]]:
    if not row:
        return None
    return {
        "schema": SCHEMA,
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "pr_number": int(row["pr_number"] or 0),
        "head_sha": row["head_sha"] or "",
        "state": row["state"],
        "route": row["route"],
        "reason_code": row["reason_code"] or "",
        "desired_role": row["desired_role"] or "",
        "attempt": int(row["attempt"] or 0),
        "state_version": int(row["state_version"] or 0),
        "next_retry_at": row["next_retry_at"],
        "evidence_refs": _object(row["evidence_refs_json"]),
        "board_status": row["board_status"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "actor": row["actor"] or "",
    }


def _canonical_merge_provenance(evidence_refs: Mapping[str, Any]) -> bool:
    merge = evidence_refs.get("merge") if isinstance(evidence_refs, Mapping) else None
    if not isinstance(merge, Mapping):
        return False
    merged_sha = str(merge.get("merged_sha") or "").strip().lower()
    source = str(merge.get("provenance_source") or "").strip().lower()
    repo_role = str(merge.get("repo_role") or "").strip().lower()
    if len(merged_sha) < 7:
        return False
    if source not in {
        "github_pr_merged", "default_branch_backfill", "orphan_merge_discovery",
        "offline_evidence_verified", "reconcile",
    }:
        return False
    if repo_role and repo_role != "canonical":
        return False
    return True


def _decision_fingerprint(run: Mapping[str, Any]) -> tuple:
    return (
        str(run.get("pr_number") or 0),
        str(run.get("head_sha") or "").lower(),
        str(run.get("state") or "").lower(),
        str(run.get("route") or "").lower(),
        str(run.get("reason_code") or ""),
        str(run.get("desired_role") or ""),
        str(run.get("board_status") or ""),
        json.dumps(_object(run.get("evidence_refs")), sort_keys=True),
    )


def _invalidate_stale_evidence(evidence_refs: Mapping[str, Any],
                               head_sha: str) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in dict(evidence_refs or {}).items():
        if key in STALE_EVIDENCE_KEYS:
            continue
        if isinstance(value, Mapping):
            value_head = str(value.get("head_sha") or "").lower()
            if value_head and value_head != head_sha.lower():
                continue
        cleaned[key] = value
    return cleaned


def _append_history_in(c: Any, data: Mapping[str, Any], *, actor: str) -> None:
    """Append a task_completion history row on the same connection (atomic)."""
    phase = str(data.get("history_phase") or "").strip().lower()
    if not phase:
        return
    outcome = str(data.get("history_outcome") or "succeeded").strip().lower()
    evidence = _object(data.get("history_evidence") or data.get("evidence_refs"))
    failure = _object(data.get("history_failure"))
    payload = {
        "task_id": data.get("task_id"),
        "pr_number": data.get("pr_number"),
        "head_sha": data.get("head_sha"),
        "runner_generation": data.get("runner_generation") or 1,
        "phase": phase,
        "outcome": outcome,
        "evidence": evidence if outcome != "failed" else {},
        "failure": failure,
        "transitioned_at": data.get("transitioned_at") or time.time(),
    }
    task_id = str(payload["task_id"] or "").strip().upper()
    head_sha = str(payload["head_sha"] or "").strip().lower()
    try:
        pr_number = int(payload.get("pr_number") or 0)
        generation = int(payload.get("runner_generation") or 0)
    except (TypeError, ValueError) as exc:
        raise CompletionRunError("pr_number and runner_generation must be integers") from exc
    if phase not in task_completion.PHASES:
        raise CompletionRunError(f"unsupported completion phase: {phase}")
    if outcome not in task_completion.OUTCOMES:
        raise CompletionRunError(f"unsupported completion outcome: {outcome}")
    if outcome == "failed" and not failure:
        raise CompletionRunError("failed transitions require explicit failure evidence")
    if outcome != "failed" and not evidence:
        raise CompletionRunError("completion transitions require durable evidence")
    identity = f"{task_id}\x1f{pr_number}\x1f{head_sha}\x1f{generation}\x1f{phase}"
    transition_id = "completion-" + hashlib.sha256(identity.encode()).hexdigest()[:20]
    now = float(payload["transitioned_at"])
    existing = c.execute(
        "SELECT * FROM task_execution_completion_phases WHERE "
        "task_id=? AND pr_number=? AND head_sha=? AND runner_generation=? AND phase=?",
        (task_id, pr_number, head_sha, generation, phase)).fetchone()
    if existing:
        current = task_completion._row(existing)
        if (current["outcome"] == outcome and current["evidence"] == evidence
                and current["failure"] == failure):
            return
        if current["outcome"] != "pending" or outcome == "pending":
            raise CompletionRunError("completion transition identity conflict")
        c.execute(
            "UPDATE task_execution_completion_phases SET outcome=?,evidence_json=?,"
            "failure_json=?,actor=?,transitioned_at=? WHERE transition_id=?",
            (outcome, json.dumps(evidence, sort_keys=True),
             json.dumps(failure, sort_keys=True), str(actor or "system"), now,
             transition_id))
        return
    c.execute(
        "INSERT INTO task_execution_completion_phases("
        "transition_id,task_id,pr_number,head_sha,runner_generation,phase,outcome,"
        "evidence_json,failure_json,actor,transitioned_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (transition_id, task_id, pr_number, head_sha, generation, phase, outcome,
         json.dumps(evidence, sort_keys=True), json.dumps(failure, sort_keys=True),
         str(actor or "system"), now))


def get_active_completion_run(task_id: str, *,
                              project: str = DEFAULT_PROJECT) -> Optional[dict[str, Any]]:
    task_id = str(task_id or "").strip().upper()
    if not task_id:
        return None
    with _conn(project) as c:
        row = c.execute(
            "SELECT * FROM completion_runs WHERE task_id=?", (task_id,)
        ).fetchone()
    return _row(row)


def transition_completion_run(data: Mapping[str, Any], *, actor: str,
                              project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    task_id = str(data.get("task_id") or "").strip().upper()
    head_sha = str(data.get("head_sha") or "").strip().lower()
    state = str(data.get("state") or "").strip().lower()
    route = str(data.get("route") or "").strip().lower()
    reason_code = str(data.get("reason_code") or "").strip()
    desired_role = str(data.get("desired_role") or "").strip()
    board_status = str(data.get("board_status") or "").strip()
    evidence_refs = _object(data.get("evidence_refs"))
    next_retry_at = data.get("next_retry_at")
    try:
        pr_number = int(data.get("pr_number") or 0)
    except (TypeError, ValueError) as exc:
        raise CompletionRunError("pr_number must be an integer") from exc
    if not task_id or pr_number <= 0 or len(head_sha) < 7:
        raise CompletionRunError("task_id, PR, and exact head SHA are required")
    if state not in STATES:
        raise CompletionRunError(f"unsupported completion state: {state}")
    if route not in ROUTES:
        raise CompletionRunError(f"unsupported completion route: {route}")
    if state == "done" and not _canonical_merge_provenance(evidence_refs):
        raise CompletionRunError(
            "Done requires canonical provenance on the completion run")

    now = float(data.get("updated_at") or time.time())

    def write():
        with _conn(project) as c:
            existing = c.execute(
                "SELECT * FROM completion_runs WHERE task_id=?", (task_id,)
            ).fetchone()
            current = _row(existing)
            decision = {
                "pr_number": pr_number,
                "head_sha": head_sha,
                "state": state,
                "route": route,
                "reason_code": reason_code,
                "desired_role": desired_role,
                "board_status": board_status,
                "evidence_refs": evidence_refs,
            }
            if current and _decision_fingerprint(current) == _decision_fingerprint(decision):
                return current

            if current:
                run_id = current["run_id"]
                created_at = current["created_at"]
                state_version = int(current["state_version"] or 1)
                attempt = int(current["attempt"] or 1)
                head_changed = current["head_sha"] != head_sha
                route_or_state_changed = (
                    current["state"] != state or current["route"] != route
                    or current["reason_code"] != reason_code
                    or current["desired_role"] != desired_role
                )
                if head_changed or route_or_state_changed:
                    state_version += 1
                if head_changed:
                    evidence_refs_final = _invalidate_stale_evidence(
                        evidence_refs, head_sha)
                    # Keep caller-supplied exact-head evidence after invalidation.
                    for key, value in evidence_refs.items():
                        if key in STALE_EVIDENCE_KEYS and isinstance(value, Mapping):
                            if str(value.get("head_sha") or "").lower() == head_sha:
                                evidence_refs_final[key] = value
                    attempt += 1
                else:
                    evidence_refs_final = dict(evidence_refs)
                    if route_or_state_changed:
                        attempt += 1
            else:
                run_id = "completion-run-" + uuid.uuid4().hex[:16]
                created_at = now
                state_version = 1
                attempt = 1
                evidence_refs_final = dict(evidence_refs)

            c.execute(
                "INSERT INTO completion_runs("
                "run_id, task_id, pr_number, head_sha, state, route, reason_code, "
                "desired_role, attempt, state_version, next_retry_at, "
                "evidence_refs_json, board_status, created_at, updated_at, actor) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "pr_number=excluded.pr_number, head_sha=excluded.head_sha, "
                "state=excluded.state, route=excluded.route, "
                "reason_code=excluded.reason_code, desired_role=excluded.desired_role, "
                "attempt=excluded.attempt, state_version=excluded.state_version, "
                "next_retry_at=excluded.next_retry_at, "
                "evidence_refs_json=excluded.evidence_refs_json, "
                "board_status=excluded.board_status, updated_at=excluded.updated_at, "
                "actor=excluded.actor",
                (run_id, task_id, pr_number, head_sha, state, route, reason_code,
                 desired_role, attempt, state_version, next_retry_at,
                 json.dumps(evidence_refs_final, sort_keys=True), board_status,
                 created_at, now, str(actor or "system")))

            _append_history_in(c, {
                **dict(data),
                "task_id": task_id,
                "pr_number": pr_number,
                "head_sha": head_sha,
                "evidence_refs": evidence_refs_final,
                "transitioned_at": now,
            }, actor=actor)

            if board_status:
                c.execute(
                    "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                    (board_status, now, task_id))

            return _row(c.execute(
                "SELECT * FROM completion_runs WHERE task_id=?", (task_id,)
            ).fetchone())

    return _write_through(project, write)


def recover_incomplete_runs(*, project: str = DEFAULT_PROJECT,
                            actor: str = "system/completion_runs") -> dict[str, Any]:
    """Create current runs for orphaned nonterminal PR-backed tasks.

    Recovery does not depend on board status alone: any nonterminal task with a
    recorded PR/head and no completion_run row is admitted. Re-running is
    idempotent and does not duplicate effects.
    """
    now = time.time()

    def write():
        recovered = 0
        with _conn(project) as c:
            rows = c.execute(
                "SELECT t.task_id, t.status, g.pr_number, g.head_sha "
                "FROM tasks t "
                "JOIN task_git_state g ON g.task_id = t.task_id "
                "LEFT JOIN completion_runs r ON r.task_id = t.task_id "
                "WHERE r.task_id IS NULL "
                "AND COALESCE(g.pr_number, 0) > 0 "
                "AND COALESCE(g.head_sha, '') <> '' "
                "AND t.status NOT IN ('Done', 'Cancelled', 'Canceled')"
            ).fetchall()
            for row in rows:
                task_id = row["task_id"]
                pr_number = int(row["pr_number"] or 0)
                head_sha = str(row["head_sha"] or "").strip().lower()
                status = str(row["status"] or "")
                route = "remediation" if status == "Blocked" else "wait"
                state = "blocked" if status == "Blocked" else "waiting"
                run_id = "completion-run-" + uuid.uuid4().hex[:16]
                c.execute(
                    "INSERT INTO completion_runs("
                    "run_id, task_id, pr_number, head_sha, state, route, reason_code, "
                    "desired_role, attempt, state_version, next_retry_at, "
                    "evidence_refs_json, board_status, created_at, updated_at, actor) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, task_id, pr_number, head_sha, state, route,
                     "recovered_incomplete_run",
                     "remediation" if route == "remediation" else "",
                     1, 1, None, "{}", status, now, now, actor))
                recovered += 1
        return {"recovered": recovered, "schema": SCHEMA}

    return _write_through(project, write)


__all__ = [
    "SCHEMA",
    "STATES",
    "ROUTES",
    "TERMINAL_STATES",
    "CompletionRunError",
    "get_active_completion_run",
    "transition_completion_run",
    "recover_incomplete_runs",
]
