"""Coordination receipt projection over append-only Switchboard activity.

Receipts are read-only projections — activity remains source of truth.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

import store

SCHEMA = "switchboard.coordination_receipt.v1"
POLICY_VERSION = "coordination_receipt.projection.v1"

TERMINAL_STATUSES = frozenset({"done", "void", "superseded"})


def _json_payload(raw: Any) -> Any:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _receipt_id(project: str, task_id: str, claim_id: str) -> str:
    return f"cr:{project}:{task_id}:{claim_id}"


def _parse_receipt_id(receipt_id: str) -> Optional[Dict[str, str]]:
    parts = (receipt_id or "").split(":", 3)
    if len(parts) != 4 or parts[0] != "cr":
        return None
    return {"project": parts[1], "task_id": parts[2], "claim_id": parts[3]}


def _source_event(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "cursor": int(row["id"]),
        "kind": row["kind"],
        "actor": row["actor"],
        "created_at": row["created_at"],
    }


def _new_receipt(project: str, task_id: str, claim_id: str,
                 *, started_at: float, agent_id: str = "") -> Dict[str, Any]:
    return {
        "schema": SCHEMA,
        "receipt_id": _receipt_id(project, task_id, claim_id),
        "project": project,
        "task_id": task_id,
        "claim_id": claim_id,
        "agent_id": agent_id or None,
        "runtime": None,
        "host_id": None,
        "status": "open",
        "started_at": started_at,
        "terminal_at": None,
        "evidence_refs": [],
        "approval_refs": [],
        "policy_refs": [],
        "cost_refs": [],
        "failure_refs": [],
        "outcome_refs": [],
        "side_effect_refs": [],
        "source_events": [],
        "gaps": [],
        "policy_version": POLICY_VERSION,
    }


def _append_ref(bucket: List[Dict[str, Any]], ref: Dict[str, Any]) -> None:
    if not ref:
        return
    key = json.dumps(ref, sort_keys=True, default=str)
    if any(json.dumps(existing, sort_keys=True, default=str) == key for existing in bucket):
        return
    bucket.append(ref)


def _evidence_from_payload(payload: Dict[str, Any], cursor: int) -> Optional[Dict[str, Any]]:
    refs = []
    for key in ("branch", "head_sha", "pr_number", "pr_url", "merged_sha"):
        if payload.get(key) not in (None, ""):
            refs.append(key)
    if not refs:
        return None
    return {
        "kind": "git",
        "cursor": cursor,
        **{k: payload.get(k) for k in refs},
    }


def _task_status_to_receipt_status(status: str) -> str:
    normalized = (status or "").strip().lower()
    mapping = {
        "not started": "open",
        "ready": "open",
        "todo": "open",
        "backlog": "open",
        "in progress": "running",
        "in review": "in_review",
        "done": "done",
        "cancelled": "void",
        "canceled": "void",
    }
    return mapping.get(normalized, "open")


def _apply_event(receipt: Dict[str, Any], row: sqlite3.Row) -> None:
    kind = (row["kind"] or "").strip()
    payload = _json_payload(row["payload"])
    event = _source_event(row)
    receipt["source_events"].append(event)

    if kind == "task.claimed":
        receipt["status"] = "running"
        receipt["agent_id"] = payload.get("agent_id") or row["actor"] or receipt.get("agent_id")
        if payload.get("claim_id"):
            receipt["claim_id"] = payload["claim_id"]
            receipt["receipt_id"] = _receipt_id(
                receipt["project"], receipt["task_id"], receipt["claim_id"])
        dispatch = payload.get("dispatch_reason") or {}
        if dispatch:
            _append_ref(receipt["policy_refs"], {
                "kind": "dispatch",
                "cursor": event["cursor"],
                "policy": dispatch.get("policy"),
                "score": dispatch.get("score"),
                "factors": dispatch.get("factors"),
            })
        work_session = dispatch.get("work_session") or payload.get("work_session") or {}
        if work_session.get("work_session_id"):
            _append_ref(receipt["policy_refs"], {
                "kind": "work_session",
                "cursor": event["cursor"],
                **{k: work_session.get(k) for k in (
                    "work_session_id", "policy_profile", "status")},
            })
        return

    if kind == "task.claim.completed":
        receipt["status"] = "in_review"
        evidence = payload.get("evidence") or {}
        ref = _evidence_from_payload(evidence, event["cursor"])
        if ref:
            _append_ref(receipt["evidence_refs"], ref)
        for gate in payload.get("review_gates") or []:
            _append_ref(receipt["approval_refs"], {
                "kind": "review_gate",
                "cursor": event["cursor"],
                "gate": gate,
            })
        if payload.get("done_gate"):
            _append_ref(receipt["failure_refs"], {
                "kind": "done_gate_blocked",
                "cursor": event["cursor"],
                "gate": payload.get("done_gate"),
            })
        return

    if kind == "task.claim.abandoned":
        receipt["status"] = "void"
        receipt["terminal_at"] = row["created_at"]
        _append_ref(receipt["failure_refs"], {
            "kind": "claim_abandoned",
            "cursor": event["cursor"],
            "reason": payload.get("reason"),
        })
        return

    if kind in ("git.pr_opened", "git.pr_evidence_hydrated"):
        ref = _evidence_from_payload(payload, event["cursor"])
        if ref:
            _append_ref(receipt["evidence_refs"], ref)
        if kind == "git.pr_opened" and receipt["status"] in ("open", "running"):
            receipt["status"] = "in_review"
        return

    if kind in ("git.pr_merged", "git.default_branch_backfilled"):
        receipt["status"] = "done"
        receipt["terminal_at"] = row["created_at"]
        ref = _evidence_from_payload(payload, event["cursor"])
        if ref:
            _append_ref(receipt["evidence_refs"], ref)
        _append_ref(receipt["outcome_refs"], {
            "kind": "merge_provenance",
            "cursor": event["cursor"],
            "merged_sha": payload.get("merged_sha") or payload.get("commit_sha"),
            "pr_number": payload.get("pr_number"),
        })
        return

    if kind == "task.offline_verified":
        receipt["status"] = "done"
        receipt["terminal_at"] = row["created_at"]
        offline = payload.get("offline_evidence") or payload
        _append_ref(receipt["evidence_refs"], {
            "kind": "offline",
            "cursor": event["cursor"],
            "artifact_url": offline.get("artifact_url"),
            "evidence_hash": offline.get("evidence_hash"),
            "verifier": offline.get("verifier"),
        })
        _append_ref(receipt["outcome_refs"], {
            "kind": "offline_verified",
            "cursor": event["cursor"],
            "verifier": offline.get("verifier"),
        })
        return

    if kind == "task.review_gate":
        _append_ref(receipt["approval_refs"], {
            "kind": "review_gate",
            "cursor": event["cursor"],
            "gate": payload.get("gate"),
            "source": payload.get("source"),
        })
        return

    if kind in ("task.done_blocked", "principal.unbound_write"):
        _append_ref(receipt["failure_refs"], {
            "kind": kind,
            "cursor": event["cursor"],
            **{k: payload.get(k) for k in (
                "failure_class", "reason", "message", "code")},
        })
        return

    if kind in ("bug.submitted", "bug.reported_from_task"):
        _append_ref(receipt["failure_refs"], {
            "kind": kind,
            "cursor": event["cursor"],
            "bug_task_id": payload.get("bug_task_id"),
            "failure_class": payload.get("failure_class"),
            "severity_hint": payload.get("severity_hint"),
        })
        return

    if kind == "tally.usage_reported":
        _append_ref(receipt["cost_refs"], {
            "kind": "usage",
            "cursor": event["cursor"],
            "spend_id": payload.get("spend_id"),
            "cost_usd": payload.get("cost_usd"),
            "source": payload.get("source"),
        })
        return

    if kind.startswith("side_effect."):
        _append_ref(receipt["side_effect_refs"], {
            "kind": kind,
            "cursor": event["cursor"],
            "effect_key": payload.get("effect_key"),
            "status": payload.get("status"),
        })
        return

    if kind == "agent.registered" and payload.get("agent_id"):
        receipt["agent_id"] = payload.get("agent_id")
        if payload.get("runtime"):
            receipt["runtime"] = payload.get("runtime")
        if payload.get("host_id"):
            receipt["host_id"] = payload.get("host_id")


def _segment_events(rows: List[sqlite3.Row]) -> List[List[sqlite3.Row]]:
    segments: List[List[sqlite3.Row]] = []
    prefix: List[sqlite3.Row] = []
    current: List[sqlite3.Row] = []
    for row in rows:
        kind = (row["kind"] or "").strip()
        if kind == "task.claimed":
            if current:
                segments.append(current)
            current = prefix + [row]
            prefix = []
        elif current:
            current.append(row)
        else:
            prefix.append(row)
    if current:
        segments.append(current)
    elif prefix:
        segments.append(prefix)
    return segments


def _build_receipt(project: str, task_id: str,
                   rows: List[sqlite3.Row]) -> Dict[str, Any]:
    claim_id = "lifecycle"
    started_at = rows[0]["created_at"] if rows else 0.0
    agent_id = ""
    for row in rows:
        if (row["kind"] or "").strip() == "task.claimed":
            payload = _json_payload(row["payload"])
            claim_id = payload.get("claim_id") or claim_id
            agent_id = payload.get("agent_id") or row["actor"] or agent_id
            started_at = row["created_at"]
            break
    receipt = _new_receipt(project, task_id, claim_id,
                             started_at=started_at, agent_id=agent_id)
    for row in rows:
        _apply_event(receipt, row)
    if receipt["status"] not in TERMINAL_STATUSES and receipt.get("terminal_at"):
        receipt["terminal_at"] = None
    return receipt


def _mark_superseded(receipts: List[Dict[str, Any]]) -> None:
    for idx, receipt in enumerate(receipts):
        if idx + 1 >= len(receipts):
            break
        nxt = receipts[idx + 1]
        if receipt["status"] not in TERMINAL_STATUSES:
            receipt["status"] = "superseded"
            receipt["terminal_at"] = nxt.get("started_at")


def _enrich_receipt(c: sqlite3.Connection, receipt: Dict[str, Any]) -> None:
    task_id = receipt["task_id"]
    claim_id = receipt.get("claim_id")
    if claim_id and claim_id != "lifecycle":
        effects = c.execute(
            "SELECT effect_key, effect_type, target, resource, status, requested_at, "
            "verified_at FROM external_side_effects WHERE task_id=? AND claim_id=? "
            "ORDER BY requested_at",
            (task_id, claim_id),
        ).fetchall()
    else:
        effects = c.execute(
            "SELECT effect_key, effect_type, target, resource, status, requested_at, "
            "verified_at FROM external_side_effects WHERE task_id=? ORDER BY requested_at",
            (task_id,),
        ).fetchall()
    for row in effects:
        _append_ref(receipt["side_effect_refs"], {
            "kind": "external_side_effect",
            "effect_key": row["effect_key"],
            "effect_type": row["effect_type"],
            "target": row["target"],
            "resource": row["resource"],
            "status": row["status"],
            "requested_at": row["requested_at"],
            "verified_at": row["verified_at"],
        })

    if claim_id and claim_id != "lifecycle":
        spend_rows = c.execute(
            "SELECT id, cost_usd, provider, model, status, created_at FROM llm_spend "
            "WHERE task_id=? AND claim_id=? ORDER BY created_at",
            (task_id, claim_id),
        ).fetchall()
    else:
        spend_rows = c.execute(
            "SELECT id, cost_usd, provider, model, status, created_at FROM llm_spend "
            "WHERE task_id=? ORDER BY created_at",
            (task_id,),
        ).fetchall()
    for row in spend_rows:
        _append_ref(receipt["cost_refs"], {
            "kind": "llm_spend",
            "spend_id": row["id"],
            "cost_usd": row["cost_usd"],
            "provider": row["provider"],
            "model": row["model"],
            "status": row["status"],
            "created_at": row["created_at"],
        })

    if claim_id and claim_id != "lifecycle":
        outcomes = c.execute(
            "SELECT id, status, title, created_at FROM outcomes "
            "WHERE task_id=? AND claim_id=? ORDER BY created_at",
            (task_id, claim_id),
        ).fetchall()
    else:
        outcomes = c.execute(
            "SELECT id, status, title, created_at FROM outcomes "
            "WHERE task_id=? ORDER BY created_at",
            (task_id,),
        ).fetchall()
    for row in outcomes:
        _append_ref(receipt["outcome_refs"], {
            "kind": "outcome",
            "outcome_id": row["id"],
            "status": row["status"],
            "title": row["title"],
            "created_at": row["created_at"],
        })

    if receipt["status"] == "in_review" and not receipt["evidence_refs"]:
        receipt["gaps"].append("in_review_without_evidence_refs")
    if receipt["status"] == "done" and not receipt["outcome_refs"]:
        receipt["gaps"].append("done_without_outcome_refs")


def _fetch_task_activity(c: sqlite3.Connection, task_id: str,
                           from_cursor: int = 0,
                           until_cursor: Optional[int] = None) -> List[sqlite3.Row]:
    clauses = ["task_id=?"]
    vals: List[Any] = [task_id]
    if from_cursor:
        clauses.append("id > ?")
        vals.append(int(from_cursor))
    if until_cursor:
        clauses.append("id <= ?")
        vals.append(int(until_cursor))
    sql = (
        "SELECT id, task_id, actor, kind, payload, created_at "
        f"FROM activity WHERE {' AND '.join(clauses)} ORDER BY id"
    )
    return c.execute(sql, vals).fetchall()


def project_task_receipts(project: str, task_id: str, *,
                          from_cursor: int = 0,
                          until_cursor: Optional[int] = None,
                          claim_id: str = "") -> List[Dict[str, Any]]:
    store.init_db(project)
    with store._conn(project) as c:
        if not c.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
            return []
        rows = _fetch_task_activity(c, task_id, from_cursor=from_cursor,
                                    until_cursor=until_cursor)
        if not rows:
            task = store.get_task(task_id, project=project) or {}
            receipt = _new_receipt(project, task_id, "lifecycle",
                                   started_at=task.get("created_at") or 0.0,
                                   agent_id=task.get("assignee") or "")
            receipt["status"] = _task_status_to_receipt_status(task.get("status") or "")
            git_state = (task.get("git_state") or {})
            ref = _evidence_from_payload(git_state, 0)
            if ref:
                _append_ref(receipt["evidence_refs"], ref)
            _enrich_receipt(c, receipt)
            return [receipt]
        segments = _segment_events(rows)
        receipts = [_build_receipt(project, task_id, segment) for segment in segments]
        _mark_superseded(receipts)
        if claim_id:
            receipts = [r for r in receipts if r.get("claim_id") == claim_id]
        for receipt in receipts:
            _enrich_receipt(c, receipt)
        return receipts


def get_coordination_receipt(project: str, receipt_id: str) -> Dict[str, Any]:
    parsed = _parse_receipt_id(receipt_id)
    if not parsed:
        return {"error": "invalid_receipt_id", "receipt_id": receipt_id}
    if parsed["project"] != project:
        return {"error": "project_mismatch", "receipt_id": receipt_id, "project": project}
    receipts = project_task_receipts(
        project, parsed["task_id"], claim_id=parsed["claim_id"])
    for receipt in receipts:
        if receipt["receipt_id"] == receipt_id:
            return receipt
    return {"error": "receipt_not_found", "receipt_id": receipt_id}


def list_coordination_receipts(project: str, *,
                               task_id: str = "",
                               agent_id: str = "",
                               limit: int = 50) -> Dict[str, Any]:
    store.init_db(project)
    limit = max(1, min(int(limit or 50), 500))
    receipts: List[Dict[str, Any]] = []
    with store._conn(project) as c:
        if task_id:
            task_ids = [task_id]
        else:
            task_ids = [
                row["task_id"] for row in c.execute(
                    "SELECT task_id FROM tasks ORDER BY updated_at DESC LIMIT ?",
                    (limit * 3,),
                ).fetchall()
            ]
        for tid in task_ids:
            receipts.extend(project_task_receipts(project, tid))
            if len(receipts) >= limit:
                break
    if agent_id:
        agent_id = agent_id.strip()
        receipts = [r for r in receipts if (r.get("agent_id") or "") == agent_id]
    receipts.sort(key=lambda r: float(r.get("started_at") or 0), reverse=True)
    receipts = receipts[:limit]
    return {
        "schema": "switchboard.coordination_receipt_list.v1",
        "project": project,
        "count": len(receipts),
        "receipts": receipts,
    }
