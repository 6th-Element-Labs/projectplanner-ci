"""Durable completion transitions owned by the Task Execution aggregate.

Rows are append-only evidence receipts for the existing execution identity.  They
do not schedule work and do not form an independent coordinator state machine.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Mapping, Optional

from constants import DEFAULT_PROJECT
from db.connection import _conn, _write_through


SCHEMA = "switchboard.task_completion.v1"
PHASES = (
    "review_handoff", "ci", "review_verdict", "remediation",
    "merge_queue", "reconciliation", "completed", "failed",
)
OUTCOMES = frozenset({"pending", "succeeded", "failed"})


class TaskCompletionError(ValueError):
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
        "transition_id": row["transition_id"],
        "task_id": row["task_id"],
        "pr_number": row["pr_number"],
        "head_sha": row["head_sha"],
        "runner_generation": row["runner_generation"],
        "phase": row["phase"],
        "outcome": row["outcome"],
        "evidence": _object(row["evidence_json"]),
        "failure": _object(row["failure_json"]),
        "actor": row["actor"],
        "transitioned_at": row["transitioned_at"],
    }


def record_transition(data: Mapping[str, Any], *, actor: str,
                      project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    task_id = str(data.get("task_id") or "").strip().upper()
    head_sha = str(data.get("head_sha") or "").strip().lower()
    phase = str(data.get("phase") or "").strip().lower()
    outcome = str(data.get("outcome") or "").strip().lower()
    evidence = _object(data.get("evidence"))
    failure = _object(data.get("failure"))
    try:
        pr_number = int(data.get("pr_number") or 0)
        generation = int(data.get("runner_generation") or 0)
    except (TypeError, ValueError) as exc:
        raise TaskCompletionError("pr_number and runner_generation must be integers") from exc
    if not task_id or pr_number <= 0 or len(head_sha) < 7 or generation <= 0:
        raise TaskCompletionError(
            "task_id, PR, exact head SHA, and runner generation are required")
    if phase not in PHASES:
        raise TaskCompletionError(f"unsupported completion phase: {phase}")
    if outcome not in OUTCOMES:
        raise TaskCompletionError(f"unsupported completion outcome: {outcome}")
    if outcome == "failed" and not failure:
        raise TaskCompletionError("failed transitions require explicit failure evidence")
    if outcome != "failed" and not evidence:
        raise TaskCompletionError("completion transitions require durable evidence")

    identity = f"{task_id}\x1f{pr_number}\x1f{head_sha}\x1f{generation}\x1f{phase}"
    transition_id = "completion-" + hashlib.sha256(identity.encode()).hexdigest()[:20]
    now = float(data.get("transitioned_at") or time.time())

    def write():
        with _conn(project) as c:
            existing = c.execute(
            "SELECT * FROM task_execution_completion_phases WHERE "
            "task_id=? AND pr_number=? AND head_sha=? AND runner_generation=? AND phase=?",
            (task_id, pr_number, head_sha, generation, phase)).fetchone()
            if existing:
                current = _row(existing)
                if (current["outcome"] == outcome and current["evidence"] == evidence
                        and current["failure"] == failure):
                    return current
                if current["outcome"] != "pending" or outcome == "pending":
                    raise TaskCompletionError("completion transition identity conflict")
                c.execute(
                    "UPDATE task_execution_completion_phases SET outcome=?,evidence_json=?,"
                    "failure_json=?,actor=?,transitioned_at=? WHERE transition_id=?",
                    (outcome, json.dumps(evidence, sort_keys=True),
                     json.dumps(failure, sort_keys=True), str(actor or "system"), now,
                     transition_id))
                return _row(c.execute(
                    "SELECT * FROM task_execution_completion_phases WHERE transition_id=?",
                    (transition_id,)).fetchone())
            c.execute(
                "INSERT INTO task_execution_completion_phases("
                "transition_id,task_id,pr_number,head_sha,runner_generation,phase,outcome,"
                "evidence_json,failure_json,actor,transitioned_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (transition_id, task_id, pr_number, head_sha, generation, phase, outcome,
                 json.dumps(evidence, sort_keys=True), json.dumps(failure, sort_keys=True),
                 str(actor or "system"), now))
            return _row(c.execute(
                "SELECT * FROM task_execution_completion_phases WHERE transition_id=?",
                (transition_id,)).fetchone())

    return _write_through(project, write)


def get_completion(task_id: str, *, pr_number: int = 0, head_sha: str = "",
                   runner_generation: int = 0,
                   project: str = DEFAULT_PROJECT) -> Optional[dict[str, Any]]:
    where = ["task_id=?"]
    values: list[Any] = [str(task_id or "").strip().upper()]
    for column, value in (("pr_number", pr_number), ("head_sha", head_sha),
                          ("runner_generation", runner_generation)):
        if value:
            where.append(f"{column}=?")
            values.append(value)
    with _conn(project) as c:
        rows = c.execute(
            "SELECT * FROM task_execution_completion_phases WHERE "
            + " AND ".join(where)
            + " ORDER BY transitioned_at ASC, rowid ASC", values).fetchall()
    if not rows:
        return None
    transitions = [_row(row) for row in rows]
    latest = transitions[-1]
    return {
        "schema": SCHEMA,
        "task_id": latest["task_id"],
        "pr_number": latest["pr_number"],
        "head_sha": latest["head_sha"],
        "runner_generation": latest["runner_generation"],
        "phase": latest["phase"],
        "outcome": latest["outcome"],
        "evidence": latest["evidence"],
        "failure": latest["failure"],
        "transitions": transitions,
    }
