"""Append-only decision log, including explainable coordinator decisions."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

from constants import *  # noqa: F401,F403
from db.connection import *  # noqa: F401,F403
from db.core import *  # noqa: F401,F403
from db.schema import *  # noqa: F401,F403

COORDINATOR_DECISION_SCHEMA = "switchboard.coordinator_decision.v1"

__all__ = [
    "COORDINATOR_DECISION_SCHEMA",
    "coordinator_decision_id",
    "record_decision",
    "record_coordinator_decision",
    "list_decisions",
    "list_coordinator_decisions",
    "get_decision",
]


def _structured(value: Any, default: Any, expected: type, field: str) -> Any:
    if value in (None, ""):
        return copy.deepcopy(default)
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} must be valid JSON") from exc
    if not isinstance(parsed, expected):
        raise ValueError(f"{field} must be a {expected.__name__}")
    return copy.deepcopy(parsed)


def _decision_record(row: Any) -> Dict[str, Any]:
    record = dict(row)
    record["decision_id"] = record.get("decision_key") or f"decision-{record['id']}"
    structured = (
        ("inputs_json", "inputs_snapshot", {}, dict),
        ("chosen_action_json", "chosen_action", {}, dict),
        ("skipped_alternatives_json", "skipped_alternatives", [], list),
        ("result_json", "result", {}, dict),
    )
    for stored, public, default, expected in structured:
        raw = record.pop(stored, None)
        try:
            record[public] = _structured(raw, default, expected, public)
        except ValueError:
            # Legacy/corrupt rows stay visible and explicitly fail closed in the projection.
            record[public] = copy.deepcopy(default)
            record.setdefault("projection_warnings", []).append(f"malformed_{stored}")
    if record.get("decision_kind"):
        record["schema"] = COORDINATOR_DECISION_SCHEMA
    return record


def coordinator_decision_id(*, project: str, task_id: str = "",
                            deliverable_id: str = "", coordinator_agent_id: str = "",
                            decision_kind: str, inputs_snapshot: Dict[str, Any],
                            policy_rule: str, chosen_action: Dict[str, Any],
                            stable_key: str = "") -> str:
    """Return a deterministic id for one coordinator recommendation/action.

    When ``stable_key`` is set it is the idempotency key (scoped by project) so a
    retried tick with the same caller key does not manufacture a second explanation.
    Otherwise the canonical input/rule/action snapshot is the identity.
    """
    if (stable_key or "").strip():
        identity: Dict[str, Any] = {
            "project": project,
            "stable_key": stable_key.strip(),
        }
    else:
        identity = {
            "project": project,
            "task_id": task_id or None,
            "deliverable_id": deliverable_id or None,
            "coordinator_agent_id": coordinator_agent_id or None,
            "decision_kind": decision_kind,
            "inputs_snapshot": inputs_snapshot,
            "policy_rule": policy_rule,
            "chosen_action": chosen_action,
        }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    return "coorddec-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def record_decision(task_id: Optional[str], author: str, title: str,
                    context: str, decision: str, rationale: str,
                    supersedes: Optional[int] = None,
                    project: str = DEFAULT_PROJECT, *,
                    decision_key: str = "", decision_kind: str = "",
                    deliverable_id: str = "", coordinator_agent_id: str = "",
                    inputs_snapshot: Any = None, policy_rule: str = "",
                    chosen_action: Any = None, skipped_alternatives: Any = None,
                    result: Any = None,
                    connection: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """Append an immutable ADR-lite record; keyed records are idempotent.

    Legacy callers can keep using the six text fields. Coordinator callers additionally store
    machine-readable inputs, rule, choice, rejected choices, and outcome in the same ledger.
    """
    inputs = _structured(inputs_snapshot, {}, dict, "inputs_snapshot")
    chosen = _structured(chosen_action, {}, dict, "chosen_action")
    skipped = _structured(skipped_alternatives, [], list, "skipped_alternatives")
    outcome = _structured(result, {}, dict, "result")
    now = time.time()

    def write(c: sqlite3.Connection) -> Dict[str, Any]:
        if decision_key:
            existing = c.execute(
                "SELECT * FROM decisions WHERE decision_key=?", (decision_key,)
            ).fetchone()
            if existing:
                record = _decision_record(existing)
                record.update({"created": False, "idempotent": True})
                return record
        cur = c.execute(
            "INSERT INTO decisions(task_id, author, title, context, decision, rationale, "
            "supersedes, created_at, decision_key, decision_kind, deliverable_id, "
            "coordinator_agent_id, inputs_json, policy_rule, chosen_action_json, "
            "skipped_alternatives_json, result_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, author, title, context, decision, rationale, supersedes, now,
             decision_key or None, decision_kind or None, deliverable_id or None,
             coordinator_agent_id or None, json.dumps(inputs, sort_keys=True),
             policy_rule or None, json.dumps(chosen, sort_keys=True),
             json.dumps(skipped, sort_keys=True), json.dumps(outcome, sort_keys=True)),
        )
        decision_pk = cur.lastrowid
        if supersedes:
            c.execute("UPDATE decisions SET status='superseded' WHERE id=?", (supersedes,))
        row = c.execute("SELECT * FROM decisions WHERE id=?", (decision_pk,)).fetchone()
        record = _decision_record(row)
        record.update({"created": True, "idempotent": False})
        return record

    if connection is not None:
        return write(connection)
    with _conn(project) as conn:
        return write(conn)


def record_coordinator_decision(*, author: str, title: str,
                                inputs_snapshot: Any, policy_rule: str,
                                chosen_action: Any, skipped_alternatives: Any,
                                result: Any, project: str = DEFAULT_PROJECT,
                                task_id: str = "", deliverable_id: str = "",
                                coordinator_agent_id: str = "",
                                decision_kind: str = "recommendation",
                                stable_key: str = "", context: str = "",
                                rationale: str = "",
                                connection: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """Persist one explainable coordinator recommendation/action.

    Required structured fields mirror COORD-3 acceptance. The deterministic ``decision_id``
    makes retries idempotent while the integer ``id`` preserves existing decision-log links.
    """
    try:
        inputs = _structured(inputs_snapshot, {}, dict, "inputs_snapshot")
        chosen = _structured(chosen_action, {}, dict, "chosen_action")
        skipped = _structured(skipped_alternatives, [], list, "skipped_alternatives")
        outcome = _structured(result, {}, dict, "result")
    except ValueError as exc:
        return {"error": "invalid_coordinator_decision", "message": str(exc)}
    if not (author or coordinator_agent_id).strip():
        return {"error": "author_required"}
    if not policy_rule.strip():
        return {"error": "policy_rule_required"}
    if not chosen:
        return {"error": "chosen_action_required"}

    coordinator = (coordinator_agent_id or author).strip()
    kind = (decision_kind or "recommendation").strip()
    key = coordinator_decision_id(
        project=project, task_id=task_id, deliverable_id=deliverable_id,
        coordinator_agent_id=coordinator, decision_kind=kind,
        inputs_snapshot=inputs, policy_rule=policy_rule, chosen_action=chosen,
        stable_key=stable_key,
    )
    action_name = str(chosen.get("action") or chosen.get("status") or kind)
    human_context = context or (
        f"Coordinator evaluated {len(inputs.get('candidates') or inputs.get('next_actions') or [])} "
        f"candidate(s) for {deliverable_id or task_id or project}."
    )
    human_rationale = rationale or (
        f"Applied {policy_rule}; {len(skipped)} alternative(s) were skipped."
    )
    return record_decision(
        task_id=task_id or None,
        author=author or coordinator,
        title=title or f"Coordinator: {action_name.replace('_', ' ')}",
        context=human_context,
        decision=action_name,
        rationale=human_rationale,
        project=project,
        decision_key=key,
        decision_kind=kind,
        deliverable_id=deliverable_id,
        coordinator_agent_id=coordinator,
        inputs_snapshot=inputs,
        policy_rule=policy_rule,
        chosen_action=chosen,
        skipped_alternatives=skipped,
        result=outcome,
        connection=connection,
    )


def list_decisions(task_id: Optional[str] = None, status: str = "",
                   project: str = DEFAULT_PROJECT, *, deliverable_id: str = "",
                   decision_kind: str = "", limit: int = 0) -> List[Dict[str, Any]]:
    """List decisions newest-first, with structured coordinator fields decoded."""
    query = "SELECT * FROM decisions WHERE 1=1"
    params: List[Any] = []
    if task_id:
        query += " AND task_id=?"
        params.append(task_id)
    if status:
        query += " AND status=?"
        params.append(status)
    if deliverable_id:
        query += " AND deliverable_id=?"
        params.append(deliverable_id)
    if decision_kind:
        query += " AND decision_kind=?"
        params.append(decision_kind)
    query += " ORDER BY id DESC"
    if limit:
        query += " LIMIT ?"
        params.append(max(1, int(limit)))
    with _conn(project) as c:
        rows = c.execute(query, params).fetchall()
    return [_decision_record(row) for row in rows]


def list_coordinator_decisions(task_id: str = "", deliverable_id: str = "",
                               decision_kind: str = "", limit: int = 100,
                               project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    query = "SELECT * FROM decisions WHERE decision_key IS NOT NULL"
    params: List[Any] = []
    if task_id:
        query += " AND task_id=?"
        params.append(task_id)
    if deliverable_id:
        query += " AND deliverable_id=?"
        params.append(deliverable_id)
    if decision_kind:
        query += " AND decision_kind=?"
        params.append(decision_kind)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit or 100), 1000)))
    with _conn(project) as c:
        rows = c.execute(query, params).fetchall()
    return [_decision_record(row) for row in rows]


def get_decision(decision_id: Any, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        if isinstance(decision_id, str) and not decision_id.isdigit():
            row = c.execute(
                "SELECT * FROM decisions WHERE decision_key=?", (decision_id,)
            ).fetchone()
        else:
            row = c.execute("SELECT * FROM decisions WHERE id=?", (int(decision_id),)).fetchone()
    return _decision_record(row) if row else None
