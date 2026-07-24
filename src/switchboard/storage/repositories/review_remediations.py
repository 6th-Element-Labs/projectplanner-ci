"""Automatic, bounded remediation of durable review verdicts (COORD-20).

A ``changes_requested`` verdict is not merely a comment. It becomes a
task-scoped acceptance contract and reopens the work for the lifecycle
coordinator. The coordinator then ensures the remediation session through the
same ``start_task(role=...)`` command as every other transition. A verdict on a
new head closes the prior round; repeated or judgment-class findings fail
closed through COORD-6.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import Any, Mapping, Optional

from constants import DEFAULT_PROJECT
from db.connection import _conn, _write_through
from switchboard.storage.repositories.coordination import (
    deliver_coordination_escalation,
)


REMEDIATION_SCHEMA = "switchboard.review_remediation.v1"
REMEDIATION_SUMMARY_SCHEMA = "switchboard.review_remediation_summary.v1"
REMEDIATION_METRICS_SCHEMA = "switchboard.hands_off_review_metrics.v1"
ACCEPTANCE_SCHEMA = "switchboard.review_remediation_acceptance.v1"
CROSS_TASK_REPAIR_SCHEMA = "switchboard.cross_task_review_repair.v1"
DEFAULT_MAX_ROUNDS = 3
PENDING_STATUSES = frozenset({
    "queued", "wake_requested", "remediating", "review_pending", "blocked",
})
HUMAN_RESOLVABLE_STATUSES = PENDING_STATUSES | frozenset({"escalated", "wake_failed"})
RESOLVED_STATUSES = frozenset({"resolved", "resolved_with_followup"})


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [dict(item) for item in parsed if isinstance(item, Mapping)] if isinstance(parsed, list) else []


def _remediation_from_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["schema"] = REMEDIATION_SCHEMA
    item["acceptance_criteria"] = _json_list(item.pop("acceptance_criteria_json", "[]"))
    item["escalation_findings"] = _json_list(item.pop("escalation_findings_json", "[]"))
    for key in (
        "requires_adversarial_review", "human_intervention_required",
        "resolved_without_human", "save_counted",
    ):
        item[key] = bool(item.get(key))
    return item


def _acceptance_finding(finding: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(finding.get("id") or "").strip(),
        "location": str(finding.get("location") or "").strip(),
        "category": str(finding.get("category") or "").strip(),
        "severity": str(finding.get("severity") or "").strip(),
        "invariant_violated": str(finding.get("invariant_violated") or "").strip(),
        "repair_requirement": str(finding.get("repair_requirement") or "").strip(),
        "class": str(finding.get("class") or "").strip().lower(),
    }


def _needs_adversarial_review(findings: list[dict[str, Any]]) -> bool:
    for finding in findings:
        text = " ".join(str(finding.get(key) or "") for key in (
            "category", "invariant_violated", "repair_requirement",
        )).lower()
        if any(token in text for token in ("concurr", "lease", "race", "locking", "atomic")):
            return True
    return False


def _runtime(value: str) -> str:
    raw = str(value or "").strip().lower()
    if "claude" in raw:
        return "claude-code"
    if "cursor" in raw:
        return "cursor"
    if "codex" in raw:
        return "codex"
    return raw or "codex"


def _worker_runtime_in(c: sqlite3.Connection, task_id: str, assignee: str) -> str:
    row = c.execute(
        "SELECT runtime FROM work_sessions WHERE task_id=? ORDER BY updated_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row and str(row["runtime"] or "").strip():
        return _runtime(row["runtime"])
    claim = c.execute(
        "SELECT agent_id FROM task_claims WHERE task_id=? ORDER BY claimed_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return _runtime((claim or {}).get("agent_id") if isinstance(claim, dict) else (
        claim["agent_id"] if claim else assignee))


def _gate_is_review_only(gate: Mapping[str, Any]) -> bool:
    blocking = [
        dict(finding) for finding in gate.get("findings") or []
        if isinstance(finding, Mapping) and finding.get("blocking", True)
    ]
    if not blocking:
        return False
    codes = [str(finding.get("code") or "").strip().lower() for finding in blocking]
    return all(code.startswith("review_") or "review" in code for code in codes)


def _latest_review_only_gate_in(c: sqlite3.Connection, task_id: str,
                                source_head_sha: str) -> bool:
    rows = c.execute(
        "SELECT payload FROM activity WHERE task_id=? AND kind='merge.gate' "
        "ORDER BY id DESC LIMIT 10", (task_id,),
    ).fetchall()
    for row in rows:
        try:
            gate = json.loads(row["payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        gate_head = str(gate.get("head_sha") or "").strip()
        if gate_head and gate_head != source_head_sha:
            continue
        if _gate_is_review_only(gate):
            return True
    return False


def _criteria_document(verdict: Mapping[str, Any], round_no: int,
                       findings: list[dict[str, Any]], adversarial: bool) -> str:
    return json.dumps({
        "schema": ACCEPTANCE_SCHEMA,
        "task_id": verdict.get("task_id"),
        "verdict_id": verdict.get("verdict_id"),
        "source_head_sha": verdict.get("head_sha"),
        "round": round_no,
        "requires_adversarial_review": adversarial,
        "findings": findings,
    }, sort_keys=True)


def _resolve_prior_in(c: sqlite3.Connection, task_id: str, new_head_sha: str,
                      actor: str, now: float, *, final_pass: bool = False) -> list[str]:
    placeholders = ",".join("?" for _ in PENDING_STATUSES)
    rows = c.execute(
        f"SELECT * FROM review_remediations WHERE task_id=? "
        f"AND status IN ({placeholders}) AND source_head_sha<>? ORDER BY round_no",
        (task_id, *sorted(PENDING_STATUSES), new_head_sha),
    ).fetchall()
    resolved: list[str] = []
    for row in rows:
        hands_off = not bool(row["human_intervention_required"])
        resolved_status = "resolved" if final_pass else "resolved_with_followup"
        c.execute(
            "UPDATE review_remediations SET status=?, "
            "resolved_without_human=?, resolved_head_sha=?, resolved_at=?, updated_at=? "
            "WHERE remediation_id=?",
            (resolved_status, int(hands_off), new_head_sha, now, now,
             row["remediation_id"]),
        )
        c.execute(
            "UPDATE review_findings SET state='fixed', resolved_by=?, resolved_reason=?, "
            "resolved_sha=?, updated_at=? WHERE verdict_id=? AND finding_class='auto' "
            "AND state='open'",
            (actor, "A new head reached independent re-review.", new_head_sha, now,
             row["verdict_id"]),
        )
        resolved.append(row["remediation_id"])
        c.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (task_id, actor,
             "review.remediation_resolved" if final_pass
             else "review.remediation_resolved_with_followup", json.dumps({
                "remediation_id": row["remediation_id"],
                "source_head_sha": row["source_head_sha"],
                "resolved_head_sha": new_head_sha,
                "resolved_without_human": hands_off,
            }, sort_keys=True), now),
        )
    return resolved


def _cross_task_repair_link(task_row: sqlite3.Row) -> dict[str, Any]:
    state = _json_object(task_row["agent_state"])
    link = _json_object(state.get("review_repair"))
    if not link:
        return {}
    link["repair_task_id"] = str(
        link.get("repair_task_id") or task_row["task_id"] or ""
    ).strip().upper()
    link["source_task_id"] = str(link.get("source_task_id") or "").strip().upper()
    link["source_verdict_id"] = str(link.get("source_verdict_id") or "").strip()
    link["remediation_id"] = str(link.get("remediation_id") or "").strip()
    link["finding_ids"] = sorted({
        str(finding_id or "").strip()
        for finding_id in (link.get("finding_ids") or [])
        if str(finding_id or "").strip()
    })
    return link


def _exact_head_merge_gate_in(
        c: sqlite3.Connection, task_id: str, head_sha: str,
) -> Optional[dict[str, Any]]:
    rows = c.execute(
        "SELECT payload FROM activity WHERE task_id=? AND kind='merge.gate' "
        "ORDER BY id DESC LIMIT 25",
        (task_id,),
    ).fetchall()
    for row in rows:
        gate = _json_object(row["payload"])
        if str(gate.get("head_sha") or "").strip() != head_sha:
            continue
        if gate.get("ok") is True or str(gate.get("status") or "").lower() in {
            "pass", "passed", "green",
        }:
            return gate
        return None
    return None


def _repair_blocked(
        link: Mapping[str, Any], reason: str, **details: Any,
) -> dict[str, Any]:
    return {
        "schema": CROSS_TASK_REPAIR_SCHEMA,
        "status": "blocked",
        "reason": reason,
        "repair_task_id": link.get("repair_task_id") or None,
        "source_task_id": link.get("source_task_id") or None,
        "remediation_id": link.get("remediation_id") or None,
        **details,
    }


def _repair_waiting(
        link: Mapping[str, Any], reason: str, **details: Any,
) -> dict[str, Any]:
    return {
        "schema": CROSS_TASK_REPAIR_SCHEMA,
        "status": "waiting",
        "reason": reason,
        "repair_task_id": link.get("repair_task_id") or None,
        "source_task_id": link.get("source_task_id") or None,
        "remediation_id": link.get("remediation_id") or None,
        **details,
    }


def _resolve_cross_task_repair_impl(
        repair_task_id: str, *, actor: str, project: str,
) -> dict[str, Any]:
    """Resolve one explicitly linked source remediation from canonical repair proof."""
    repair_task_id = str(repair_task_id or "").strip().upper()
    now = time.time()
    with _conn(project) as c:
        repair_task = c.execute(
            "SELECT * FROM tasks WHERE task_id=?", (repair_task_id,),
        ).fetchone()
        if not repair_task:
            return _repair_blocked(
                {"repair_task_id": repair_task_id},
                "repair_task_not_found",
            )
        link = _cross_task_repair_link(repair_task)
        if not link:
            return {
                "schema": CROSS_TASK_REPAIR_SCHEMA,
                "status": "not_applicable",
                "repair_task_id": repair_task_id,
            }
        if link["repair_task_id"] != repair_task_id:
            return _repair_blocked(link, "repair_task_mismatch")
        required = {
            "source_task_id": link["source_task_id"],
            "source_verdict_id": link["source_verdict_id"],
            "remediation_id": link["remediation_id"],
            "finding_ids": link["finding_ids"],
        }
        missing = sorted(key for key, value in required.items() if not value)
        if missing:
            return _repair_blocked(link, "repair_link_incomplete", missing=missing)

        repair_state = _json_object(repair_task["agent_state"])
        bug_report = _json_object(repair_state.get("bug_report"))
        if str(bug_report.get("source_task") or "").strip().upper() != link["source_task_id"]:
            return _repair_blocked(link, "bug_source_task_mismatch")

        source_task = c.execute(
            "SELECT * FROM tasks WHERE task_id=?", (link["source_task_id"],),
        ).fetchone()
        remediation = c.execute(
            "SELECT * FROM review_remediations WHERE remediation_id=?",
            (link["remediation_id"],),
        ).fetchone()
        if not source_task:
            return _repair_blocked(link, "source_task_not_found")
        if not remediation:
            return _repair_blocked(link, "source_remediation_not_found")
        if (
            remediation["task_id"] != link["source_task_id"]
            or remediation["verdict_id"] != link["source_verdict_id"]
        ):
            return _repair_blocked(link, "source_remediation_mismatch")

        expected_ids = sorted(
            finding["id"]
            for finding in _json_list(remediation["acceptance_criteria_json"])
            if finding.get("id")
        )
        if link["finding_ids"] != expected_ids:
            return _repair_blocked(
                link,
                "repair_finding_set_mismatch",
                expected_finding_ids=expected_ids,
                supplied_finding_ids=link["finding_ids"],
            )
        if bool(remediation["human_intervention_required"]):
            return _repair_blocked(link, "human_authority_required")

        placeholders = ",".join("?" for _ in link["finding_ids"])
        finding_rows = c.execute(
            f"SELECT * FROM review_findings WHERE verdict_id=? "
            f"AND finding_id IN ({placeholders}) ORDER BY finding_id",
            (link["source_verdict_id"], *link["finding_ids"]),
        ).fetchall()
        if (
            [row["finding_id"] for row in finding_rows] != link["finding_ids"]
            or any(row["finding_class"] != "auto" for row in finding_rows)
        ):
            return _repair_blocked(link, "source_findings_mismatch")

        git_state = c.execute(
            "SELECT * FROM task_git_state WHERE task_id=?", (repair_task_id,),
        ).fetchone()
        repair_head = str((git_state or {})["head_sha"] or "").strip() if git_state else ""
        merged_sha = str((git_state or {})["merged_sha"] or "").strip() if git_state else ""
        if (
            str(repair_task["status"] or "") != "Done"
            or not git_state
            or not bool(git_state["in_main_content"])
            or not repair_head
            or not merged_sha
        ):
            return _repair_waiting(link, "canonical_repair_merge_required")

        repair_verdict = c.execute(
            "SELECT * FROM review_verdicts WHERE task_id=? AND head_sha=?",
            (repair_task_id, repair_head),
        ).fetchone()
        if not repair_verdict or repair_verdict["status"] != "pass":
            return _repair_waiting(
                link, "exact_head_pass_required", repair_head_sha=repair_head)
        open_repair_findings = int(c.execute(
            "SELECT COUNT(*) FROM review_findings WHERE verdict_id=? AND state='open'",
            (repair_verdict["verdict_id"],),
        ).fetchone()[0])
        if open_repair_findings:
            return _repair_blocked(
                link, "repair_verdict_has_open_findings",
                open_finding_count=open_repair_findings,
            )
        if not _exact_head_merge_gate_in(c, repair_task_id, repair_head):
            return _repair_waiting(
                link, "exact_head_merge_gate_pass_required",
                repair_head_sha=repair_head,
            )

        already_resolved = (
            remediation["status"] in RESOLVED_STATUSES
            and remediation["resolved_head_sha"] == repair_head
            and all(
                row["state"] == "fixed" and row["resolved_sha"] == repair_head
                for row in finding_rows
            )
        )
        resolution = {
            "schema": CROSS_TASK_REPAIR_SCHEMA,
            "status": "resolved",
            "source_task_id": link["source_task_id"],
            "source_verdict_id": link["source_verdict_id"],
            "remediation_id": link["remediation_id"],
            "finding_ids": link["finding_ids"],
            "repair_task_id": repair_task_id,
            "repair_head_sha": repair_head,
            "repair_verdict_id": repair_verdict["verdict_id"],
            "repair_merged_sha": merged_sha,
            "resolved_at": link.get("resolved_at") or now,
            "resolved_by": actor,
        }
        if already_resolved:
            resolution["idempotent_replay"] = True
            return resolution
        if remediation["status"] not in HUMAN_RESOLVABLE_STATUSES:
            return _repair_blocked(
                link, "source_remediation_not_resolvable",
                source_remediation_status=remediation["status"],
            )
        if any(row["state"] != "open" for row in finding_rows):
            return _repair_blocked(link, "source_findings_not_open")

        reason = (
            f"Fixed by {repair_task_id} exact head {repair_head}; "
            f"review {repair_verdict['verdict_id']} passed and canonical merge "
            f"{merged_sha} landed."
        )
        finding_update = c.execute(
            f"UPDATE review_findings SET state='fixed', resolved_by=?, "
            f"resolved_reason=?, resolved_sha=?, resolved_at=?, updated_at=? "
            f"WHERE verdict_id=? AND finding_id IN ({placeholders}) "
            "AND finding_class='auto' AND state='open'",
            (
                actor, reason, repair_head, now, now,
                link["source_verdict_id"], *link["finding_ids"],
            ),
        )
        if finding_update.rowcount != len(link["finding_ids"]):
            raise RuntimeError("cross-task repair finding update lost its exact-set fence")
        c.execute(
            "UPDATE review_remediations SET status='resolved', "
            "resolved_without_human=1, resolved_head_sha=?, resolved_at=?, updated_at=? "
            "WHERE remediation_id=?",
            (repair_head, now, now, link["remediation_id"]),
        )
        c.execute(
            "UPDATE tasks SET exit_criteria=?, updated_at=? WHERE task_id=?",
            (remediation["original_exit_criteria"], now, link["source_task_id"]),
        )
        repair_state["review_repair"] = resolution
        c.execute(
            "UPDATE tasks SET agent_state=?, updated_at=? WHERE task_id=?",
            (json.dumps(repair_state, sort_keys=True), now, repair_task_id),
        )
        event = json.dumps(resolution, sort_keys=True)
        c.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
            "VALUES (?,?,?,?,?)",
            (
                link["source_task_id"], actor,
                "review.cross_task_repair_resolved", event, now,
            ),
        )
        c.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
            "VALUES (?,?,?,?,?)",
            (
                repair_task_id, actor,
                "review.cross_task_repair_applied", event, now,
            ),
        )
        resolution["idempotent_replay"] = False
        resolution["metrics"] = _metrics_in(c, link["source_task_id"])
        return resolution


def _metrics_in(c: sqlite3.Connection, task_id: str = "") -> dict[str, Any]:
    query = "SELECT * FROM review_remediations"
    params: tuple[Any, ...] = ()
    if task_id:
        query += " WHERE task_id=?"
        params = (task_id,)
    rows = c.execute(query, params).fetchall()
    drained = [row for row in rows if row["status"] in RESOLVED_STATUSES]
    hands_off = [row for row in drained if bool(row["resolved_without_human"])]
    exceptions = sum(int(row["auto_finding_count"] or 0) for row in hands_off)
    drained_count = len(drained)
    return {
        "schema": REMEDIATION_METRICS_SCHEMA,
        "task_id": task_id or None,
        "remediation_round_count": len(rows),
        "pending_round_count": sum(row["status"] in PENDING_STATUSES for row in rows),
        "escalated_round_count": sum(row["status"] == "escalated" for row in rows),
        "work_units_drained": drained_count,
        "hands_off_work_units": len(hands_off),
        "hands_off_work_unit_rate": round(len(hands_off) / drained_count, 4) if drained_count else None,
        "exceptions_resolved_without_human": exceptions,
        "exceptions_resolved_per_work_unit_drained": (
            round(exceptions / drained_count, 4) if drained_count else None
        ),
        "saves": sum(int(row["save_counted"] or 0) for row in rows),
    }


def review_remediation_summary_in(c: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    row = c.execute(
        "SELECT * FROM review_remediations WHERE task_id=? ORDER BY round_no DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return {
        "schema": REMEDIATION_SUMMARY_SCHEMA,
        "task_id": task_id,
        "current": _remediation_from_row(row) if row else None,
        "metrics": _metrics_in(c, task_id),
    }


def required_review_mode_in(c: sqlite3.Connection, task_id: str,
                            head_sha: str) -> dict[str, Any]:
    """Return the review mode required by an older unresolved remediation."""
    placeholders = ",".join("?" for _ in HUMAN_RESOLVABLE_STATUSES)
    row = c.execute(
        f"SELECT * FROM review_remediations WHERE task_id=? "
        f"AND status IN ({placeholders}) AND source_head_sha<>? "
        "AND requires_adversarial_review=1 ORDER BY round_no DESC LIMIT 1",
        (task_id, *sorted(HUMAN_RESOLVABLE_STATUSES), head_sha),
    ).fetchone()
    return {
        "required": bool(row),
        "mode": "adversarial" if row else "standard",
        "remediation_id": row["remediation_id"] if row else None,
        "source_head_sha": row["source_head_sha"] if row else None,
    }


class ReviewRemediationRepository:
    """Project-scoped review-remediation state and side-effect orchestration."""

    def resolve_cross_task_repair(
            self, repair_task_id: str, *, actor: str,
            project: str = DEFAULT_PROJECT) -> dict[str, Any]:
        return _write_through(
            project,
            lambda: _resolve_cross_task_repair_impl(
                repair_task_id, actor=actor, project=project),
        )

    def reconcile_cross_task_repairs(
            self, *, actor: str = "reconcile/cross-task-review-repair",
            project: str = DEFAULT_PROJECT,
            limit: int = 200) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 1000))
        with _conn(project) as c:
            rows = c.execute(
                "SELECT task_id FROM tasks "
                "WHERE json_extract(agent_state, '$.review_repair.status')='linked' "
                "ORDER BY updated_at LIMIT ?",
                (bounded,),
            ).fetchall()
        results = [
            self.resolve_cross_task_repair(
                row["task_id"], actor=actor, project=project)
            for row in rows
        ]
        return {
            "schema": "switchboard.cross_task_review_repair_reconcile.v1",
            "checked": len(results),
            "resolved": sum(
                result.get("status") == "resolved"
                and not result.get("idempotent_replay")
                for result in results
            ),
            "blocked": sum(result.get("status") == "blocked" for result in results),
            "results": results,
        }

    def handle_verdict(self, verdict: Mapping[str, Any], *, actor: str,
                       project: str = DEFAULT_PROJECT,
                       max_rounds: Optional[int] = None) -> dict[str, Any]:
        payload = dict(verdict or {})
        if payload.get("status") not in {"pass", "changes_requested"}:
            return {"schema": REMEDIATION_SCHEMA, "status": "not_applicable"}
        limit = max(1, int(max_rounds or os.environ.get(
            "PM_REVIEW_REMEDIATION_MAX_ROUNDS", DEFAULT_MAX_ROUNDS)))
        if payload.get("status") == "pass":
            return _write_through(
                project, lambda: self._resolve_pass_impl(
                    payload, actor=actor, project=project))

        staged = _write_through(
            project, lambda: self._queue_impl(
                payload, actor=actor, project=project, max_rounds=limit))
        if staged.get("error"):
            return staged

        if staged.get("needs_wake"):
            staged["needs_wake"] = False
            staged["needs_lifecycle_ensure"] = True
        if staged.get("escalation_required"):
            staged["human_escalation"] = self._deliver_escalation(
                staged, actor=actor, project=project)
        return staged

    def mark_ensured(self, task_id: str, *, wake_id: str = "", actor: str,
                     project: str = DEFAULT_PROJECT) -> dict[str, Any]:
        """Mark the latest queued round as owned by the lifecycle coordinator."""
        task_id = str(task_id or "").strip().upper()
        now = time.time()

        def persist() -> dict[str, Any]:
            with _conn(project) as c:
                row = c.execute(
                    "SELECT * FROM review_remediations WHERE task_id=? AND status='queued' "
                    "ORDER BY round_no DESC LIMIT 1", (task_id,),
                ).fetchone()
                if not row:
                    return {"marked": False, "reason": "queued_remediation_not_found",
                            "task_id": task_id}
                c.execute(
                    "UPDATE review_remediations SET status='remediating', wake_id=?, "
                    "updated_at=? WHERE remediation_id=?",
                    (str(wake_id or ""), now, row["remediation_id"]),
                )
                c.execute(
                    "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (task_id, actor, "review.remediation_session_ensured", json.dumps({
                        "remediation_id": row["remediation_id"],
                        "wake_id": str(wake_id or "") or None,
                    }, sort_keys=True), now),
                )
                return {"marked": True, "task_id": task_id,
                        "remediation_id": row["remediation_id"],
                        "status": "remediating", "wake_id": str(wake_id or "") or None}

        return _write_through(project, persist)

    def _resolve_pass_impl(self, verdict: Mapping[str, Any], *, actor: str,
                           project: str) -> dict[str, Any]:
        task_id = str(verdict.get("task_id") or "").strip().upper()
        head_sha = str(verdict.get("head_sha") or "").strip()
        now = time.time()
        with _conn(project) as c:
            resolved = _resolve_prior_in(
                c, task_id, head_sha, actor, now, final_pass=True)
            first = c.execute(
                "SELECT original_exit_criteria FROM review_remediations WHERE task_id=? "
                "ORDER BY round_no LIMIT 1", (task_id,),
            ).fetchone()
            if resolved and first:
                c.execute(
                    "UPDATE tasks SET exit_criteria=?, updated_at=? WHERE task_id=?",
                    (first["original_exit_criteria"], now, task_id),
                )
            return {
                "schema": REMEDIATION_SCHEMA,
                "status": "resolved" if resolved else "not_applicable",
                "task_id": task_id,
                "resolved_head_sha": head_sha,
                "resolved_remediation_ids": resolved,
                "metrics": _metrics_in(c, task_id),
            }

    def _queue_impl(self, verdict: Mapping[str, Any], *, actor: str,
                    project: str, max_rounds: int) -> dict[str, Any]:
        task_id = str(verdict.get("task_id") or "").strip().upper()
        verdict_id = str(verdict.get("verdict_id") or "").strip()
        head_sha = str(verdict.get("head_sha") or "").strip()
        now = time.time()
        with _conn(project) as c:
            existing = c.execute(
                "SELECT * FROM review_remediations WHERE verdict_id=?", (verdict_id,),
            ).fetchone()
            if existing:
                row = _remediation_from_row(existing)
                row.update({
                    "idempotent_replay": True,
                    "needs_wake": existing["status"] == "queued" and not existing["wake_id"],
                    "escalation_required": bool(existing["human_intervention_required"]),
                })
                return row

            task = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task:
                return {"error": "review_remediation_task_not_found", "task_id": task_id}
            git_state = c.execute(
                "SELECT head_sha, pr_url FROM task_git_state WHERE task_id=?", (task_id,),
            ).fetchone()
            if not git_state or str(git_state["head_sha"] or "") != head_sha:
                return {
                    "error": "stale_review_remediation_head",
                    "task_id": task_id,
                    "expected_head_sha": str((git_state or {}).get("head_sha") or "")
                    if isinstance(git_state, dict) else (str(git_state["head_sha"] or "") if git_state else ""),
                }

            _resolve_prior_in(c, task_id, head_sha, actor, now)
            findings = [
                _acceptance_finding(finding) for finding in verdict.get("findings") or []
                if str(finding.get("state") or "open").lower() == "open"
            ]
            auto = [finding for finding in findings if finding["class"] == "auto"]
            escalations = [finding for finding in findings if finding["class"] == "escalate"]
            round_no = int(c.execute(
                "SELECT COUNT(*) FROM review_remediations WHERE task_id=?", (task_id,),
            ).fetchone()[0]) + 1
            first = c.execute(
                "SELECT original_exit_criteria FROM review_remediations WHERE task_id=? "
                "ORDER BY round_no LIMIT 1", (task_id,),
            ).fetchone()
            original_exit = first["original_exit_criteria"] if first else task["exit_criteria"]
            active_claim = c.execute(
                "SELECT id FROM task_claims WHERE task_id=? AND status='active' "
                "AND expires_at>? LIMIT 1", (task_id, now),
            ).fetchone()
            terminal = str(task["status"] or "") in {"Done", "Cancelled", "Canceled"}
            exhausted = round_no > max_rounds
            adversarial = _needs_adversarial_review(auto)
            human_required = bool(escalations or exhausted or active_claim or terminal)
            queueable = bool(auto) and not exhausted and not active_claim and not terminal
            status = "queued" if queueable else "escalated"
            remediation_id = "reviewremediation-" + uuid.uuid4().hex[:16]
            save_counted = int(
                str(task["status"] or "") == "In Review"
                and _latest_review_only_gate_in(c, task_id, head_sha)
            )
            runtime = _worker_runtime_in(c, task_id, str(task["assignee"] or ""))
            acceptance_doc = _criteria_document(verdict, round_no, auto, adversarial)
            c.execute(
                "INSERT INTO review_remediations("
                "remediation_id, task_id, verdict_id, source_head_sha, source_pr_url, "
                "round_no, status, acceptance_criteria_json, escalation_findings_json, "
                "original_exit_criteria, previous_status, previous_assignee, worker_runtime, "
                "requires_adversarial_review, human_intervention_required, "
                "auto_finding_count, escalate_finding_count, save_counted, created_at, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    remediation_id, task_id, verdict_id, head_sha,
                    str(verdict.get("pr_url") or ""), round_no, status,
                    json.dumps(auto, sort_keys=True), json.dumps(escalations, sort_keys=True),
                    original_exit, task["status"], task["assignee"], runtime,
                    int(adversarial), int(human_required), len(auto), len(escalations),
                    save_counted, now, now,
                ),
            )
            next_status = "Not Started" if queueable else "Blocked"
            c.execute(
                "UPDATE tasks SET status=?, assignee=NULL, exit_criteria=?, updated_at=? "
                "WHERE task_id=? AND status NOT IN ('Done','Cancelled','Canceled')",
                (next_status, acceptance_doc, now, task_id),
            )

            from decisions_store import record_coordinator_decision
            action = "queue_review_remediation" if queueable else "escalate_review_remediation"
            reason = (
                "round_budget_exhausted" if exhausted else
                "active_claim_conflict" if active_claim else
                "terminal_task_conflict" if terminal else
                "escalate_findings_require_human" if escalations and not auto else
                "no_auto_findings" if not auto else
                "auto_findings_ready"
            )
            decision = record_coordinator_decision(
                author="switchboard/auto-remediation",
                title=f"Review remediation round {round_no} for {task_id}",
                inputs_snapshot={
                    "verdict_id": verdict_id, "source_head_sha": head_sha,
                    "auto_findings": auto, "escalation_findings": escalations,
                    "round": round_no, "max_rounds": max_rounds,
                    "active_claim_id": active_claim["id"] if active_claim else None,
                },
                policy_rule=("coord.review.remediation.queue" if queueable
                             else "coord.review.remediation.escalate"),
                chosen_action={"action": action, "task_id": task_id, "reason": reason},
                skipped_alternatives=[
                    {"action": "merge", "reason": "changes_requested"},
                    {"action": "human_copy_paste", "reason": "automatic_rework_contract"},
                ],
                result={"status": status, "remediation_id": remediation_id},
                project=project, task_id=task_id,
                coordinator_agent_id="switchboard/auto-remediation",
                decision_kind="human_escalation" if human_required else "dispatch",
                stable_key=f"review-remediation:{verdict_id}",
                connection=c,
            )
            decision_id = str(decision.get("decision_id") or decision.get("id") or "")
            c.execute(
                "UPDATE review_remediations SET decision_id=? WHERE remediation_id=?",
                (decision_id or None, remediation_id),
            )
            event = {
                "schema": REMEDIATION_SCHEMA,
                "remediation_id": remediation_id,
                "verdict_id": verdict_id,
                "source_head_sha": head_sha,
                "round": round_no,
                "max_rounds": max_rounds,
                "status": status,
                "auto_finding_count": len(auto),
                "escalate_finding_count": len(escalations),
                "requires_adversarial_review": adversarial,
                "human_intervention_required": human_required,
                "save_counted": bool(save_counted),
                "decision_id": decision_id or None,
            }
            c.execute(
                "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                (task_id, "switchboard/auto-remediation",
                 "review.remediation_queued" if queueable else "review.remediation_escalated",
                 json.dumps(event, sort_keys=True), now),
            )
            row = c.execute(
                "SELECT * FROM review_remediations WHERE remediation_id=?", (remediation_id,),
            ).fetchone()
            result = _remediation_from_row(row)
            result.update({
                "idempotent_replay": False,
                "needs_wake": queueable,
                "escalation_required": human_required,
                "escalation_reason": reason,
                "lane": str(task["workstream_id"] or ""),
                "max_rounds": max_rounds,
            })
            return result

    @staticmethod
    def _deliver_escalation(remediation: Mapping[str, Any], *, actor: str,
                            project: str) -> dict[str, Any]:
        import coordinator_escalation
        reason = str(remediation.get("escalation_reason") or "review_remediation_exception")
        klass = (
            "unreachable_agent_no_host" if reason == "wake_failed" else
            "repeated_failures" if reason == "round_budget_exhausted" else
            "policy_violation" if reason in {"active_claim_conflict", "terminal_task_conflict"} else
            "ambiguous_requirements"
        )
        plan = coordinator_escalation.build_escalation_plan(
            escalation_class=klass,
            project=project,
            task_id=str(remediation.get("task_id") or ""),
            failed_condition=(
                f"Review remediation {remediation.get('remediation_id')} requires a human: {reason}."
            ),
            source={"kind": "review_remediation", "remediation": dict(remediation)},
            blocks=["merge", "dispatch"] if not remediation.get("acceptance_criteria") else ["merge"],
        )
        if not plan:
            return {"ok": False, "delivered": False, "error": "escalation_plan_failed"}
        return deliver_coordination_escalation(
            plan, actor="switchboard/auto-remediation", notify_outbound=False,
        )

    def required_review_mode(self, task_id: str, *, head_sha: str,
                             project: str = DEFAULT_PROJECT) -> dict[str, Any]:
        with _conn(project) as c:
            return required_review_mode_in(c, task_id, head_sha)

    def resolve_human_authority(self, task_id: str, *, head_sha: str,
                                actor: str,
                                project: str = DEFAULT_PROJECT) -> dict[str, Any]:
        """Close same-head escalation rounds after COORD-19 authority resolves findings."""
        task_id = str(task_id or "").strip().upper()
        head_sha = str(head_sha or "").strip()
        return _write_through(
            project,
            lambda: self._resolve_human_authority_impl(
                task_id, head_sha=head_sha, actor=actor, project=project),
        )

    @staticmethod
    def _resolve_human_authority_impl(task_id: str, *, head_sha: str,
                                      actor: str, project: str) -> dict[str, Any]:
        now = time.time()
        with _conn(project) as c:
            placeholders = ",".join("?" for _ in HUMAN_RESOLVABLE_STATUSES)
            rows = c.execute(
                f"SELECT * FROM review_remediations WHERE task_id=? "
                f"AND source_head_sha=? AND status IN ({placeholders}) "
                "ORDER BY round_no",
                (task_id, head_sha, *sorted(HUMAN_RESOLVABLE_STATUSES)),
            ).fetchall()
            if not rows:
                return {
                    "schema": REMEDIATION_SCHEMA,
                    "status": "not_applicable",
                    "task_id": task_id,
                    "resolved_head_sha": head_sha,
                    "metrics": _metrics_in(c, task_id),
                }
            remediation_ids = [row["remediation_id"] for row in rows]
            for row in rows:
                c.execute(
                    "UPDATE review_remediations SET status='resolved', "
                    "human_intervention_required=1, resolved_without_human=0, "
                    "resolved_head_sha=?, resolved_at=?, updated_at=? "
                    "WHERE remediation_id=?",
                    (head_sha, now, now, row["remediation_id"]),
                )
                c.execute(
                    "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (task_id, actor, "review.remediation_resolved_by_authority",
                     json.dumps({
                         "remediation_id": row["remediation_id"],
                         "source_head_sha": row["source_head_sha"],
                         "resolved_head_sha": head_sha,
                         "resolved_without_human": False,
                     }, sort_keys=True), now),
                )
            latest = rows[-1]
            restore_status = str(latest["previous_status"] or "In Review")
            if restore_status in {"Done", "Cancelled", "Canceled"}:
                restore_status = "In Review"
            c.execute(
                "UPDATE tasks SET status=?, assignee=NULL, exit_criteria=?, updated_at=? "
                "WHERE task_id=? AND status NOT IN ('Done','Cancelled','Canceled')",
                (restore_status, latest["original_exit_criteria"], now, task_id),
            )
            return {
                "schema": REMEDIATION_SCHEMA,
                "status": "resolved",
                "task_id": task_id,
                "resolved_head_sha": head_sha,
                "resolved_remediation_ids": remediation_ids,
                "resolved_without_human": False,
                "metrics": _metrics_in(c, task_id),
            }

    def get(self, remediation_id: str, *, project: str = DEFAULT_PROJECT
            ) -> Optional[dict[str, Any]]:
        with _conn(project) as c:
            row = c.execute(
                "SELECT * FROM review_remediations WHERE remediation_id=?", (remediation_id,),
            ).fetchone()
        return _remediation_from_row(row) if row else None

    def list(self, *, task_id: str = "", status: str = "",
             project: str = DEFAULT_PROJECT) -> list[dict[str, Any]]:
        where = ["1=1"]
        params: list[Any] = []
        if task_id:
            where.append("task_id=?")
            params.append(task_id)
        if status:
            where.append("status=?")
            params.append(status)
        with _conn(project) as c:
            rows = c.execute(
                "SELECT * FROM review_remediations WHERE " + " AND ".join(where)
                + " ORDER BY created_at DESC", params,
            ).fetchall()
        return [_remediation_from_row(row) for row in rows]

    def metrics(self, *, task_id: str = "", project: str = DEFAULT_PROJECT
                ) -> dict[str, Any]:
        with _conn(project) as c:
            return _metrics_in(c, task_id)

    def record_save(self, task_id: str, head_sha: str, gate: Mapping[str, Any], *,
                    actor: str = "switchboard/merge-gate",
                    project: str = DEFAULT_PROJECT) -> dict[str, Any]:
        """Count a save only when review was the gate's sole blocking condition."""
        if not _gate_is_review_only(gate):
            return {"counted": False, "reason": "non_review_blockers_present"}
        return _write_through(
            project,
            lambda: self._record_save_impl(
                task_id, head_sha, actor=actor, project=project),
        )

    @staticmethod
    def _record_save_impl(task_id: str, head_sha: str, *, actor: str,
                          project: str) -> dict[str, Any]:
        now = time.time()
        with _conn(project) as c:
            row = c.execute(
                "SELECT * FROM review_remediations WHERE task_id=? AND source_head_sha=? "
                "ORDER BY round_no DESC LIMIT 1", (task_id, head_sha),
            ).fetchone()
            if not row:
                return {"counted": False, "reason": "remediation_not_found"}
            already = bool(row["save_counted"])
            c.execute(
                "UPDATE review_remediations SET save_counted=1, updated_at=? "
                "WHERE remediation_id=?", (now, row["remediation_id"]),
            )
            if not already:
                c.execute(
                    "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (task_id, actor, "review.remediation_save_counted", json.dumps({
                        "remediation_id": row["remediation_id"],
                        "head_sha": head_sha,
                        "reason": "review_was_only_merge_blocker",
                    }, sort_keys=True), now),
                )
            return {
                "counted": True, "already_counted": already,
                "remediation_id": row["remediation_id"],
                "metrics": _metrics_in(c, task_id),
            }


default_review_remediation_repository = ReviewRemediationRepository()


def handle_review_verdict(verdict: Mapping[str, Any], *, actor: str,
                          project: str = DEFAULT_PROJECT,
                          max_rounds: Optional[int] = None) -> dict[str, Any]:
    return default_review_remediation_repository.handle_verdict(
        verdict, actor=actor, project=project, max_rounds=max_rounds)


def get_review_remediation(remediation_id: str, *, project: str = DEFAULT_PROJECT
                           ) -> Optional[dict[str, Any]]:
    return default_review_remediation_repository.get(remediation_id, project=project)


def list_review_remediations(*, task_id: str = "", status: str = "",
                             project: str = DEFAULT_PROJECT) -> list[dict[str, Any]]:
    return default_review_remediation_repository.list(
        task_id=task_id, status=status, project=project)


def mark_review_remediation_ensured(task_id: str, *, wake_id: str = "", actor: str,
                                    project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return default_review_remediation_repository.mark_ensured(
        task_id, wake_id=wake_id, actor=actor, project=project)


def review_remediation_metrics(*, task_id: str = "", project: str = DEFAULT_PROJECT
                               ) -> dict[str, Any]:
    return default_review_remediation_repository.metrics(task_id=task_id, project=project)


def required_review_mode(task_id: str, *, head_sha: str,
                         project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return default_review_remediation_repository.required_review_mode(
        task_id, head_sha=head_sha, project=project)


def resolve_human_review_authority(task_id: str, *, head_sha: str, actor: str,
                                   project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return default_review_remediation_repository.resolve_human_authority(
        task_id, head_sha=head_sha, actor=actor, project=project)


def record_review_save(task_id: str, head_sha: str, gate: Mapping[str, Any], *,
                       actor: str = "switchboard/merge-gate",
                       project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return default_review_remediation_repository.record_save(
        task_id, head_sha, gate, actor=actor, project=project)


def resolve_cross_task_review_repair(
        repair_task_id: str, *, actor: str,
        project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return default_review_remediation_repository.resolve_cross_task_repair(
        repair_task_id, actor=actor, project=project)


def reconcile_cross_task_review_repairs(
        *, actor: str = "reconcile/cross-task-review-repair",
        project: str = DEFAULT_PROJECT,
        limit: int = 200) -> dict[str, Any]:
    return default_review_remediation_repository.reconcile_cross_task_repairs(
        actor=actor, project=project, limit=limit)


__all__ = [
    "ACCEPTANCE_SCHEMA", "CROSS_TASK_REPAIR_SCHEMA", "DEFAULT_MAX_ROUNDS",
    "REMEDIATION_METRICS_SCHEMA",
    "REMEDIATION_SCHEMA", "REMEDIATION_SUMMARY_SCHEMA", "ReviewRemediationRepository",
    "default_review_remediation_repository", "get_review_remediation",
    "handle_review_verdict", "list_review_remediations", "mark_review_remediation_ensured",
    "record_review_save", "reconcile_cross_task_review_repairs",
    "resolve_cross_task_review_repair",
    "required_review_mode", "required_review_mode_in", "resolve_human_review_authority",
    "review_remediation_metrics",
    "review_remediation_summary_in",
]
