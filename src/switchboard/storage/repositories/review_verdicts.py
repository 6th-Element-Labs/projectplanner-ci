"""Durable, head-SHA-keyed code-review verdict persistence (COORD-18)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any, Iterable, Mapping, Optional

from constants import DEFAULT_PROJECT
from db.connection import _conn, _write_through
from switchboard.contracts.reviews import REVIEW_FINDING_SCHEMA, REVIEW_VERDICT_SCHEMA


REVIEW_SUMMARY_SCHEMA = "switchboard.review_summary.v1"
REVIEW_MERGE_GATE_SCHEMA = "switchboard.review_merge_gate.v1"
REVIEW_MAX_ROUNDS = 3
# Liveness fence for the review gate. Rounds-based escalation only fires once reviewers have
# ENGAGED (verdict_count >= max_rounds); it can never fire when no verdict is ever recorded,
# because the count stays 0 — so a change nobody reviews blocks forever, in silence, with no
# path forward. A head awaiting a verdict for longer than this is not "in review", it is
# stuck: escalate to COORD-6 so an operator can act. Tunable per call; keep it well above a
# normal review turnaround.
REVIEW_STALL_ESCALATION_S = 30 * 60
HISTORICAL_CO8_VERDICT_ID = "reviewverdict-co8-pr441-94f03c6f"


class ReviewVerdictError(ValueError):
    """Typed failure returned by application and transport adapters."""

    def __init__(self, code: str, message: str, *, status_code: int = 400,
                 details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "error_code": self.code,
            "message": self.message,
            **self.details,
        }


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _current_git_state_in(c: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    row = c.execute(
        "SELECT head_sha, pr_url, pushed_at, evidence_json FROM task_git_state WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if not row:
        return {"head_sha": "", "pr_url": "", "pushed_at": None, "evidence": {}}
    return {
        "head_sha": str(row["head_sha"] or "").strip(),
        "pr_url": str(row["pr_url"] or "").strip(),
        # When this head started waiting on review — the clock for the liveness fence.
        "pushed_at": row["pushed_at"],
        "evidence": _json_object(row["evidence_json"]),
    }


def _finding_from_row(row: sqlite3.Row) -> dict[str, Any]:
    columns = set(row.keys())
    return {
        "schema": REVIEW_FINDING_SCHEMA,
        "id": row["finding_id"],
        "location": row["location"],
        "category": row["category"],
        "severity": row["severity"],
        "invariant_violated": row["invariant_violated"],
        "repair_requirement": row["repair_requirement"],
        "class": row["finding_class"],
        "state": row["state"],
        "resolved_by": row["resolved_by"],
        "resolved_principal_id": (
            row["resolved_principal_id"] if "resolved_principal_id" in columns else None
        ),
        "resolved_reason": row["resolved_reason"],
        "resolved_sha": row["resolved_sha"],
        "resolved_at": row["resolved_at"] if "resolved_at" in columns else None,
    }


def _verdict_from_row(c: sqlite3.Connection, row: sqlite3.Row,
                      current_head_sha: str = "",
                      current_pr_url: str = "") -> dict[str, Any]:
    findings = [
        _finding_from_row(finding)
        for finding in c.execute(
            "SELECT * FROM review_findings WHERE verdict_id=? ORDER BY finding_id",
            (row["verdict_id"],),
        ).fetchall()
    ]
    current = str(current_head_sha or "").strip()
    current_pr = str(current_pr_url or "").strip()
    valid = bool(
        (not current or current == row["head_sha"])
        and (not current_pr or current_pr == row["pr_url"])
    )
    return {
        "schema": REVIEW_VERDICT_SCHEMA,
        "verdict_id": row["verdict_id"],
        "task_id": row["task_id"],
        "pr_url": row["pr_url"],
        "head_sha": row["head_sha"],
        "reviewer_principal": row["reviewer_principal"],
        "reviewer_principal_id": row["reviewer_principal_id"],
        "review_mode": row["review_mode"],
        "status": row["status"],
        "created_at": row["created_at"],
        "findings": findings,
        "finding_count": len(findings),
        "open_finding_count": sum(1 for item in findings if item["state"] == "open"),
        "valid_for_current_head": valid,
        "invalidated_by_head_sha": (
            current if current and current != row["head_sha"] else None
        ),
        "invalidated_by_pr_url": (
            current_pr if current_pr and current_pr != row["pr_url"] else None
        ),
        "source": row["source"],
    }


def _canonical_command(data: Mapping[str, Any]) -> str:
    payload = {
        "task_id": data.get("task_id"),
        "pr_url": data.get("pr_url"),
        "head_sha": data.get("head_sha"),
        "reviewer_principal": data.get("reviewer_principal"),
        "reviewer_principal_id": data.get("reviewer_principal_id"),
        "review_mode": data.get("review_mode") or "standard",
        "status": data.get("status"),
        "findings": sorted(
            [dict(item or {}) for item in data.get("findings") or []],
            key=lambda item: str(item.get("id") or ""),
        ),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _canonical_verdict(verdict: Mapping[str, Any]) -> str:
    return _canonical_command({
        "task_id": verdict.get("task_id"),
        "pr_url": verdict.get("pr_url"),
        "head_sha": verdict.get("head_sha"),
        "reviewer_principal": verdict.get("reviewer_principal"),
        "reviewer_principal_id": verdict.get("reviewer_principal_id"),
        "review_mode": verdict.get("review_mode") or "standard",
        "status": verdict.get("status"),
        "findings": verdict.get("findings") or [],
    })


def _insert_verdict_row_in(c: sqlite3.Connection, data: Mapping[str, Any], *,
                           source: str, created_at: float, recorded_at: float) -> str:
    digest = hashlib.sha256(
        (
            f"{data['task_id']}\x1f{data['pr_url']}\x1f"
            f"{data['head_sha']}"
        ).encode("utf-8")
    ).hexdigest()[:16]
    verdict_id = str(data.get("verdict_id") or f"reviewverdict-{digest}")
    c.execute(
        "INSERT INTO review_verdicts("
        "verdict_id, task_id, pr_url, head_sha, reviewer_principal, "
        "reviewer_principal_id, review_mode, status, source, created_at, recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            verdict_id, data["task_id"], data["pr_url"], data["head_sha"],
            data["reviewer_principal"], data["reviewer_principal_id"],
            data.get("review_mode") or "standard", data["status"], source,
            created_at, recorded_at,
        ),
    )
    return verdict_id


def _insert_findings_in(c: sqlite3.Connection, verdict_id: str,
                        data: Mapping[str, Any], *, created_at: float,
                        recorded_at: float) -> None:
    for finding in data.get("findings") or []:
        state = str(finding.get("state") or "open")
        c.execute(
            "INSERT INTO review_findings("
            "verdict_id, task_id, finding_id, location, category, severity, "
            "invariant_violated, repair_requirement, finding_class, state, resolved_by, "
            "resolved_principal_id, resolved_reason, resolved_sha, resolved_at, "
            "created_at, updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                verdict_id, data["task_id"], finding["id"], finding["location"],
                finding["category"], finding["severity"], finding["invariant_violated"],
                finding["repair_requirement"], finding["class"], state,
                finding.get("resolved_by"), finding.get("resolved_principal_id"),
                finding.get("resolved_reason"), finding.get("resolved_sha"),
                finding.get("resolved_at"), created_at, recorded_at,
            ),
        )


def _assert_adversarial_reviewer_independence_in(
        c: sqlite3.Connection, task_id: str, reviewer: str) -> None:
    """Require a different recorded agent for an adversarial verdict.

    Fleet authentication may still share one service principal, so the
    server-resolved actor is the useful identity here. Claims and Work Sessions
    retain the implementation/remediation actor even after the lease ends.
    Merely labeling a self-review "adversarial" must never authorize merge.
    """
    rows = c.execute(
        "SELECT agent_id, COALESCE(NULLIF(execution_role,''),'implementation') AS role "
        "FROM task_claims WHERE task_id=? "
        "UNION ALL "
        "SELECT agent_id, COALESCE(NULLIF(execution_role,''),'implementation') AS role "
        "FROM work_sessions WHERE task_id=?",
        (task_id, task_id),
    ).fetchall()
    implementers = sorted({
        str(row["agent_id"] or "").strip()
        for row in rows
        if str(row["role"] or "").strip().lower()
        in {"implementation", "remediation"}
        and str(row["agent_id"] or "").strip()
    })
    if not implementers:
        raise ReviewVerdictError(
            "adversarial_review_independence_unverifiable",
            "adversarial review requires a recorded implementation identity",
            status_code=409,
            details={"task_id": task_id},
        )
    if reviewer.strip().lower() in {
        item.strip().lower() for item in implementers
    }:
        raise ReviewVerdictError(
            "adversarial_self_review_forbidden",
            "the implementing or remediating agent cannot author the adversarial verdict",
            status_code=403,
            details={"task_id": task_id, "implementing_agents": implementers},
        )


class ReviewVerdictRepository:
    """Project-scoped persistence and exact-head review fencing."""

    def record(self, data: Mapping[str, Any], *, actor: str, principal_id: str = "",
               project: str = DEFAULT_PROJECT) -> dict[str, Any]:
        payload = dict(data or {})
        return _write_through(
            project,
            lambda: self._record_impl(
                payload, actor=actor, principal_id=principal_id, project=project),
        )

    def _record_impl(self, data: Mapping[str, Any], *, actor: str, principal_id: str,
                     project: str) -> dict[str, Any]:
        payload = dict(data or {})
        task_id = str(payload.get("task_id") or "").strip()
        reviewer = str(payload.get("reviewer_principal") or "").strip()
        reviewer_principal_id = str(principal_id or "").strip()
        payload["reviewer_principal_id"] = reviewer_principal_id
        with _conn(project) as c:
            task = c.execute("SELECT task_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task:
                raise ReviewVerdictError(
                    "review_task_not_found", "review task does not exist", status_code=404,
                    details={"task_id": task_id},
                )
            if reviewer != str(actor or "").strip():
                raise ReviewVerdictError(
                    "reviewer_principal_mismatch",
                    "reviewer_principal must match the authenticated write actor",
                    status_code=403,
                )
            git_state = _current_git_state_in(c, task_id)
            current_head = git_state["head_sha"]
            if not current_head:
                raise ReviewVerdictError(
                    "review_head_unbound",
                    "task has no recorded PR head_sha; review cannot be fenced",
                    status_code=409,
                )
            if payload.get("head_sha") != current_head:
                raise ReviewVerdictError(
                    "stale_review_head",
                    "review head_sha does not match the task's current PR head",
                    status_code=409,
                    details={"expected_head_sha": current_head},
                )
            # Persistence is the authority boundary: internal callers through the
            # store facade cannot bypass COORD-20's adversarial re-review fence.
            from switchboard.storage.repositories.review_remediations import (
                required_review_mode_in,
            )
            review_requirement = required_review_mode_in(
                c, task_id, current_head, git_state["pr_url"])
            if (review_requirement.get("required")
                    and payload.get("review_mode", "standard")
                    != review_requirement.get("mode")):
                raise ReviewVerdictError(
                    "adversarial_review_required",
                    "active concurrency/lease remediation requires review_mode=adversarial",
                    status_code=409,
                    details={"review_requirement": review_requirement},
                )
            current_pr = git_state["pr_url"]
            if not current_pr:
                raise ReviewVerdictError(
                    "review_pr_unbound",
                    "task has no recorded PR URL; review cannot be fenced",
                    status_code=409,
                )
            if payload.get("pr_url") != current_pr:
                raise ReviewVerdictError(
                    "review_pr_mismatch",
                    "review pr_url does not match the task's current PR",
                    status_code=409,
                    details={"expected_pr_url": current_pr},
                )
            # Reviewer independence is deliberately NOT enforced. It was added in COORD-18 and
            # removed here: every fleet agent authenticates through the same shared
            # `env-mcp-token` principal (377 of 391 recorded claims), so "reviewer principal
            # must differ from every worker principal" is unsatisfiable — it rejected EVERY
            # review by EVERY agent, not just self-review, and deadlocked the board with no
            # path forward. It bought no real independence (one identity cannot be compared
            # against itself); it only blocked. Do NOT re-add it without genuine per-agent
            # principals to compare. Authentication is still required, below.
            if not reviewer_principal_id:
                raise ReviewVerdictError(
                    "reviewer_principal_unbound",
                    "review requires an authenticated principal ID",
                    status_code=403,
                )
            if str(payload.get("review_mode") or "standard") == "adversarial":
                _assert_adversarial_reviewer_independence_in(
                    c, task_id, reviewer,
                )
            existing_result = self._existing_result_in(
                c, payload, task_id=task_id, pr_url=current_pr,
                head_sha=current_head)
            if existing_result is not None:
                return existing_result
            now = time.time()
            try:
                verdict_id = _insert_verdict_row_in(
                    c, payload, source="review_command", created_at=now, recorded_at=now)
            except sqlite3.IntegrityError:
                # Defense in depth for another process winning the unique task/head race.
                # The normal same-process path is serialized by _write_through above.
                existing_result = self._existing_result_in(
                    c, payload, task_id=task_id, pr_url=current_pr,
                    head_sha=current_head)
                if existing_result is None:
                    raise
                return existing_result
            _insert_findings_in(
                c, verdict_id, payload, created_at=now, recorded_at=now)
            event = {
                "schema": REVIEW_VERDICT_SCHEMA,
                "verdict_id": verdict_id,
                "head_sha": current_head,
                "pr_url": payload["pr_url"],
                "reviewer_principal": reviewer,
                "reviewer_principal_id": reviewer_principal_id,
                "review_mode": payload.get("review_mode") or "standard",
                "status": payload["status"],
                "finding_count": len(payload.get("findings") or []),
                "principal_id": reviewer_principal_id,
            }
            c.execute(
                "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                "VALUES (?,?,?,?,?)",
                (task_id, actor, "review.verdict_recorded",
                 json.dumps(event, sort_keys=True), now),
            )
            row = c.execute(
                "SELECT * FROM review_verdicts WHERE verdict_id=?", (verdict_id,)
            ).fetchone()
            return {"created": True, "idempotent_replay": False,
                    "verdict": _verdict_from_row(
                        c, row, current_head, current_pr)}

    @staticmethod
    def _existing_result_in(c: sqlite3.Connection, payload: Mapping[str, Any], *,
                            task_id: str, pr_url: str,
                            head_sha: str) -> Optional[dict[str, Any]]:
        existing = c.execute(
            "SELECT * FROM review_verdicts "
            "WHERE task_id=? AND pr_url=? AND head_sha=?",
            (task_id, pr_url, head_sha),
        ).fetchone()
        if not existing:
            return None
        verdict = _verdict_from_row(c, existing, head_sha, pr_url)
        if _canonical_verdict(verdict) == _canonical_command(payload):
            return {"created": False, "idempotent_replay": True, "verdict": verdict}
        raise ReviewVerdictError(
            "review_verdict_conflict",
            "a different review verdict already exists for this task PR/head",
            status_code=409,
            details={"verdict_id": existing["verdict_id"]},
        )

    def get(self, task_id: str, *, head_sha: str = "", pr_url: str = "",
            project: str = DEFAULT_PROJECT) -> Optional[dict[str, Any]]:
        with _conn(project) as c:
            git_state = _current_git_state_in(c, task_id)
            current_head = git_state["head_sha"]
            current_pr = git_state["pr_url"]
            selected_head = str(head_sha or current_head).strip()
            selected_pr = str(
                pr_url or (current_pr if selected_head == current_head else "")
            ).strip()
            if not selected_head:
                return None
            if selected_pr:
                row = c.execute(
                    "SELECT * FROM review_verdicts "
                    "WHERE task_id=? AND pr_url=? AND head_sha=?",
                    (task_id, selected_pr, selected_head),
                ).fetchone()
            else:
                rows = c.execute(
                    "SELECT * FROM review_verdicts "
                    "WHERE task_id=? AND head_sha=? ORDER BY created_at",
                    (task_id, selected_head),
                ).fetchall()
                # A legacy historical lookup is safe only while the SHA maps
                # to exactly one PR. Replacement-PR ambiguity fails closed.
                row = rows[0] if len(rows) == 1 else None
            return (
                _verdict_from_row(c, row, current_head, current_pr)
                if row else None
            )

    def resolve_finding(self, data: Mapping[str, Any], *, actor: str,
                        principal_id: str = "", authorized: bool = False,
                        project: str = DEFAULT_PROJECT) -> dict[str, Any]:
        """Move one exact-head finding open -> waived|overridden with durable authority."""
        payload = dict(data or {})
        return _write_through(
            project,
            lambda: self._resolve_finding_impl(
                payload, actor=actor, principal_id=principal_id,
                authorized=authorized, project=project),
        )

    def _resolve_finding_impl(self, data: Mapping[str, Any], *, actor: str,
                              principal_id: str, authorized: bool,
                              project: str) -> dict[str, Any]:
        payload = dict(data or {})
        task_id = str(payload.get("task_id") or "").strip()
        head_sha = str(payload.get("head_sha") or "").strip()
        finding_id = str(payload.get("finding_id") or "").strip()
        resolver = str(payload.get("resolver_principal") or "").strip()
        resolver_principal_id = str(principal_id or "").strip()
        state = str(payload.get("state") or "").strip().lower()
        reason = str(payload.get("resolved_reason") or "").strip()
        resolved_sha = str(payload.get("resolved_sha") or "").strip()
        if state not in {"waived", "overridden"} or not all(
                (task_id, head_sha, finding_id, reason, resolved_sha, resolver)):
            raise ReviewVerdictError(
                "invalid_review_finding_resolution",
                "resolution requires task/head/finding/reason/resolver and state waived|overridden",
                status_code=400,
            )
        if not authorized:
            raise ReviewVerdictError(
                "review_resolution_forbidden",
                "review finding waiver/override requires explicit admin authority",
                status_code=403,
            )
        if resolver != str(actor or "").strip():
            raise ReviewVerdictError(
                "review_resolver_principal_mismatch",
                "resolver_principal must match the authenticated write actor",
                status_code=403,
            )
        if not resolver_principal_id:
            raise ReviewVerdictError(
                "review_resolver_principal_unbound",
                "review finding resolution requires an authenticated principal ID",
                status_code=403,
            )
        with _conn(project) as c:
            task = c.execute(
                "SELECT task_id FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if not task:
                raise ReviewVerdictError(
                    "review_task_not_found", "review task does not exist", status_code=404,
                    details={"task_id": task_id},
                )
            git_state = _current_git_state_in(c, task_id)
            current_head = git_state["head_sha"]
            current_pr = git_state["pr_url"]
            if not current_head:
                raise ReviewVerdictError(
                    "review_head_unbound",
                    "task has no recorded PR head_sha; finding resolution cannot be fenced",
                    status_code=409,
                )
            if head_sha != current_head or resolved_sha != current_head:
                raise ReviewVerdictError(
                    "stale_review_head",
                    "finding resolution must match the task's exact current PR head",
                    status_code=409,
                    details={"expected_head_sha": current_head},
                )
            if not current_pr:
                raise ReviewVerdictError(
                    "review_pr_unbound",
                    "task has no recorded PR URL; finding resolution cannot be fenced",
                    status_code=409,
                )
            verdict_row = c.execute(
                "SELECT * FROM review_verdicts "
                "WHERE task_id=? AND pr_url=? AND head_sha=?",
                (task_id, current_pr, current_head),
            ).fetchone()
            if not verdict_row:
                raise ReviewVerdictError(
                    "review_verdict_not_found",
                    "no review verdict exists for the task's current head_sha",
                    status_code=404,
                    details={"head_sha": current_head},
                )
            finding_row = c.execute(
                "SELECT * FROM review_findings WHERE verdict_id=? AND finding_id=?",
                (verdict_row["verdict_id"], finding_id),
            ).fetchone()
            if not finding_row:
                raise ReviewVerdictError(
                    "review_finding_not_found", "review finding does not exist",
                    status_code=404, details={"finding_id": finding_id},
                )
            existing = _finding_from_row(finding_row)
            if existing["state"] != "open":
                same_resolution = (
                    existing["state"] == state
                    and existing["resolved_by"] == resolver
                    and existing.get("resolved_principal_id") == resolver_principal_id
                    and existing["resolved_reason"] == reason
                    and existing["resolved_sha"] == resolved_sha
                )
                if same_resolution:
                    return {
                        "resolved": False,
                        "idempotent_replay": True,
                        "finding": existing,
                        "verdict": _verdict_from_row(
                            c, verdict_row, current_head, current_pr),
                    }
                raise ReviewVerdictError(
                    "review_finding_not_open",
                    "only an open review finding may be waived or overridden",
                    status_code=409,
                    details={"finding_id": finding_id, "state": existing["state"]},
                )
            now = time.time()
            previous_verdict_status = str(verdict_row["status"] or "").strip()
            c.execute(
                "UPDATE review_findings SET state=?, resolved_by=?, "
                "resolved_principal_id=?, resolved_reason=?, resolved_sha=?, "
                "resolved_at=?, updated_at=? WHERE verdict_id=? AND finding_id=?",
                (
                    state, resolver, resolver_principal_id, reason, resolved_sha,
                    now, now, verdict_row["verdict_id"], finding_id,
                ),
            )
            open_count = int(c.execute(
                "SELECT COUNT(*) FROM review_findings WHERE verdict_id=? AND state='open'",
                (verdict_row["verdict_id"],),
            ).fetchone()[0])
            promoted = open_count == 0 and previous_verdict_status != "pass"
            if promoted:
                c.execute(
                    "UPDATE review_verdicts SET status='pass' WHERE verdict_id=?",
                    (verdict_row["verdict_id"],),
                )
            event = {
                "schema": "switchboard.review_finding_resolution.v1",
                "verdict_id": verdict_row["verdict_id"],
                "finding_id": finding_id,
                "head_sha": current_head,
                "state": state,
                "resolved_reason": reason,
                "resolved_sha": resolved_sha,
                "resolver_principal": resolver,
                "resolver_principal_id": resolver_principal_id,
                "reviewer_principal": verdict_row["reviewer_principal"],
                "reviewer_principal_id": verdict_row["reviewer_principal_id"],
                "previous_verdict_status": previous_verdict_status,
                "verdict_status": "pass" if promoted else previous_verdict_status,
                "remaining_open_finding_count": open_count,
                "reviewer_quality_signal": state,
            }
            c.execute(
                "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                "VALUES (?,?,?,?,?)",
                (task_id, actor, "review.finding_resolved",
                 json.dumps(event, sort_keys=True), now),
            )
            updated_finding = c.execute(
                "SELECT * FROM review_findings WHERE verdict_id=? AND finding_id=?",
                (verdict_row["verdict_id"], finding_id),
            ).fetchone()
            updated_verdict = c.execute(
                "SELECT * FROM review_verdicts WHERE verdict_id=?",
                (verdict_row["verdict_id"],),
            ).fetchone()
            return {
                "resolved": True,
                "idempotent_replay": False,
                "finding": _finding_from_row(updated_finding),
                "verdict": _verdict_from_row(
                    c, updated_verdict, current_head, current_pr),
                "audit": event,
            }

    def list_findings(self, *, task_id: str = "", head_sha: str = "", state: str = "",
                      finding_class: str = "", severity: str = "",
                      current_head_only: bool = False,
                      project: str = DEFAULT_PROJECT) -> list[dict[str, Any]]:
        where = ["1=1"]
        params: list[Any] = []
        if task_id:
            where.append("f.task_id=?")
            params.append(task_id)
        if head_sha:
            where.append("v.head_sha=?")
            params.append(head_sha)
        if state:
            where.append("f.state=?")
            params.append(state)
        if finding_class:
            where.append("f.finding_class=?")
            params.append(finding_class)
        if severity:
            where.append("LOWER(f.severity)=?")
            params.append(severity.lower())
        with _conn(project) as c:
            current_git: dict[str, dict[str, Any]] = {}
            if task_id:
                current_git[task_id] = _current_git_state_in(c, task_id)
                if current_head_only:
                    if (
                        not current_git[task_id]["head_sha"]
                        or not current_git[task_id]["pr_url"]
                    ):
                        return []
                    where.append("v.head_sha=?")
                    params.append(current_git[task_id]["head_sha"])
                    where.append("v.pr_url=?")
                    params.append(current_git[task_id]["pr_url"])
            rows = c.execute(
                "SELECT f.*, v.pr_url, v.head_sha, v.reviewer_principal, "
                "v.reviewer_principal_id, "
                "v.status AS verdict_status, v.created_at AS verdict_created_at "
                "FROM review_findings f JOIN review_verdicts v "
                "ON v.verdict_id=f.verdict_id WHERE " + " AND ".join(where) +
                " ORDER BY v.created_at DESC, f.finding_id",
                params,
            ).fetchall()
            results = []
            for row in rows:
                item = _finding_from_row(row)
                git_state = current_git.get(row["task_id"])
                if git_state is None:
                    git_state = _current_git_state_in(c, row["task_id"])
                    current_git[row["task_id"]] = git_state
                current = git_state["head_sha"]
                current_pr = git_state["pr_url"]
                item.update({
                    "verdict_id": row["verdict_id"],
                    "task_id": row["task_id"],
                    "pr_url": row["pr_url"],
                    "head_sha": row["head_sha"],
                    "reviewer_principal": row["reviewer_principal"],
                    "reviewer_principal_id": row["reviewer_principal_id"],
                    "verdict_status": row["verdict_status"],
                    "verdict_created_at": row["verdict_created_at"],
                    "valid_for_current_head": bool(
                        current
                        and current_pr
                        and current == row["head_sha"]
                        and current_pr == row["pr_url"]
                    ),
                    "invalidated_by_head_sha": (
                        current if current and current != row["head_sha"] else None
                    ),
                    "invalidated_by_pr_url": (
                        current_pr
                        if current_pr and current_pr != row["pr_url"]
                        else None
                    ),
                })
                results.append(item)
            return results

    def summary(self, task_id: str, *, project: str = DEFAULT_PROJECT) -> dict[str, Any]:
        with _conn(project) as c:
            git_state = _current_git_state_in(c, task_id)
            return review_verdict_summary_in(
                c, task_id, git_state["head_sha"], git_state["pr_url"])


def review_verdict_summary_in(c: sqlite3.Connection, task_id: str,
                              current_head_sha: str = "",
                              current_pr_url: str = "") -> dict[str, Any]:
    counts = c.execute(
        "SELECT COUNT(*) AS finding_count, "
        "SUM(CASE WHEN state='open' THEN 1 ELSE 0 END) AS open_count "
        "FROM review_findings WHERE task_id=?",
        (task_id,),
    ).fetchone()
    total = int(counts["finding_count"] or 0)
    total_open = int(counts["open_count"] or 0)
    current_row = None
    if current_head_sha and current_pr_url:
        current_row = c.execute(
            "SELECT * FROM review_verdicts "
            "WHERE task_id=? AND pr_url=? AND head_sha=?",
            (task_id, current_pr_url, current_head_sha),
        ).fetchone()
    current_verdict = (
        _verdict_from_row(
            c, current_row, current_head_sha, current_pr_url)
        if current_row else None
    )
    verdict_count = int(c.execute(
        "SELECT COUNT(*) FROM review_verdicts WHERE task_id=?", (task_id,)
    ).fetchone()[0])
    current_count = int((current_verdict or {}).get("finding_count") or 0)
    return {
        "schema": REVIEW_SUMMARY_SCHEMA,
        "task_id": task_id,
        "current_head_sha": current_head_sha or None,
        "current_pr_url": current_pr_url or None,
        "current_verdict": current_verdict,
        "current_verdict_status": (current_verdict or {}).get("status") or "missing",
        "finding_count": total,
        "open_finding_count": total_open,
        "current_head_finding_count": current_count,
        "current_head_open_finding_count": int(
            (current_verdict or {}).get("open_finding_count") or 0
        ),
        "historical_finding_count": max(0, total - current_count),
        "verdict_count": verdict_count,
        "stale_verdict_count": verdict_count - (1 if current_verdict else 0),
    }


def review_merge_gate(task_id: str, head_sha: str, *,
                      project: str = DEFAULT_PROJECT,
                      max_rounds: int = REVIEW_MAX_ROUNDS,
                      now: Optional[float] = None,
                      stall_seconds: float = REVIEW_STALL_ESCALATION_S) -> dict[str, Any]:
    """Return the deterministic exact-head review input consumed by merge_gate.

    ``now``/``stall_seconds`` fence review *liveness* (see REVIEW_STALL_ESCALATION_S) and are
    injectable so the stall rule is unit-testable without sleeping."""
    requested_head = str(head_sha or "").strip()
    with _conn(project) as c:
        git_state = _current_git_state_in(c, task_id)
        current_head = git_state["head_sha"]
        current_pr = git_state["pr_url"]
        summary = review_verdict_summary_in(
            c, task_id, current_head, current_pr)
        row = None
        if requested_head and current_pr:
            row = c.execute(
                "SELECT * FROM review_verdicts "
                "WHERE task_id=? AND pr_url=? AND head_sha=?",
                (task_id, current_pr, requested_head),
            ).fetchone()
        verdict = (
            _verdict_from_row(c, row, current_head, current_pr)
            if row else None
        )

    code = ""
    message = ""
    if not requested_head:
        code = "review_head_sha_required"
        message = "Review required, but the current PR head_sha is unavailable."
    elif not current_head:
        code = "review_head_sha_required"
        message = "Review required, but the task has no current recorded PR head_sha."
    elif not current_pr:
        code = "review_pr_identity_required"
        message = "Review required, but the task has no current recorded PR URL."
    elif current_head and requested_head != current_head:
        code = "stale_review_verdict"
        message = (
            f"Review required for current head {current_head}; merge intent used "
            f"stale head {requested_head}."
        )
    elif not verdict:
        code = "review_required"
        message = f"Review required for current head {requested_head}."
    elif int(verdict.get("open_finding_count") or 0) > 0:
        count = int(verdict.get("open_finding_count") or 0)
        message = (
            f"{count} open review finding{'s' if count != 1 else ''} at "
            f"{requested_head}."
        )
        code = "open_review_findings"
    elif verdict.get("status") != "pass":
        code = "review_not_passed"
        message = (
            f"Review verdict at {requested_head} has status "
            f"{verdict.get('status') or 'missing'}; pass is required."
        )

    ok = not code
    rounds = int(summary.get("verdict_count") or 0)
    bounded_rounds = max(1, int(max_rounds or REVIEW_MAX_ROUNDS))
    # Reviewers engaged but the change keeps failing review.
    rounds_exhausted = not ok and rounds >= bounded_rounds
    # Reviewers never engaged at all. `rounds` stays 0 here, so rounds_exhausted can NEVER
    # fire — this is the branch that turns a silent permanent block (no verdict recorded)
    # into a COORD-6 escalation an operator can act on.
    pushed_at = git_state.get("pushed_at")
    waited_s: Optional[float] = None
    if pushed_at:
        clock = float(now) if now is not None else time.time()
        waited_s = max(0.0, clock - float(pushed_at))
    review_stalled = bool(
        not ok
        and rounds == 0
        and waited_s is not None
        and waited_s >= float(stall_seconds)
    )
    escalation_required = rounds_exhausted or review_stalled
    escalation_reason = (
        "review_round_limit_reached" if rounds_exhausted
        else "review_stalled_no_verdict" if review_stalled
        else None
    )
    return {
        "schema": REVIEW_MERGE_GATE_SCHEMA,
        "task_id": task_id,
        "head_sha": requested_head or None,
        "current_head_sha": current_head or None,
        "current_pr_url": current_pr or None,
        "required": True,
        "ok": ok,
        "status": "passed" if ok else "blocked",
        "code": code or None,
        "message": message or "Passing review verdict recorded for the current head_sha.",
        "verdict_status": (verdict or {}).get("status") or "missing",
        "open_finding_count": int((verdict or {}).get("open_finding_count") or 0),
        "open_finding_ids": [
            item.get("id") for item in (verdict or {}).get("findings") or []
            if item.get("state") == "open"
        ],
        "verdict": verdict,
        "round": rounds,
        "max_rounds": bounded_rounds,
        "waited_seconds": round(waited_s, 1) if waited_s is not None else None,
        "stall_seconds": float(stall_seconds),
        "review_stalled": review_stalled,
        "escalation_required": escalation_required,
        "escalation_reason": escalation_reason,
        "escalation_task_id": "COORD-6" if escalation_required else None,
    }


def review_merge_gate_findings(task_id: str, head_sha: str, *,
                               project: str = DEFAULT_PROJECT,
                               max_rounds: int = REVIEW_MAX_ROUNDS,
                               now: Optional[float] = None,
                               stall_seconds: float = REVIEW_STALL_ESCALATION_S,
                               ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Adapt the exact-head review gate to merge-gate blocking findings."""
    gate = review_merge_gate(
        task_id, head_sha, project=project, max_rounds=max_rounds,
        now=now, stall_seconds=stall_seconds,
    )
    if gate.get("ok"):
        return gate, []

    code = str(gate.get("code") or "review_required")
    findings = [{
        "code": code,
        "message": str(gate.get("message") or "Review required before merge."),
        "failure_class": (
            "missing_data"
            if code in {"review_head_sha_required", "review_pr_identity_required"}
            else "failed_gate"
        ),
        "severity": "high",
        "blocking": True,
        "review_gate": gate,
    }]
    if gate.get("escalation_required"):
        reason = str(gate.get("escalation_reason") or "review_round_limit_reached")
        if reason == "review_stalled_no_verdict":
            waited_min = int((gate.get("waited_seconds") or 0) // 60)
            escalation_message = (
                f"No review verdict in {waited_min} min for this head. This is a "
                "stall, not a queue — escalate through COORD-6 so an operator can "
                "assign a reviewer or record the verdict."
            )
        else:
            escalation_message = (
                f"Review remains blocked after {gate.get('round')} rounds; "
                "escalate through COORD-6."
            )
        findings.append({
            "code": reason,
            "message": escalation_message,
            "failure_class": "failed_gate",
            "severity": "high",
            "blocking": True,
            "review_round": gate.get("round"),
            "max_review_rounds": gate.get("max_rounds"),
            "waited_seconds": gate.get("waited_seconds"),
            "escalation_task_id": gate.get("escalation_task_id"),
            "head_sha": str(head_sha or "").strip() or None,
        })
    return gate, findings


def ensure_historical_review_backfills_in(c: sqlite3.Connection,
                                          project: str) -> dict[str, Any]:
    """Idempotently restore the four findings that triggered the CO-8 remediation PR."""
    if project != "switchboard":
        return {"backfilled": False, "reason": "project_not_applicable"}
    if not c.execute("SELECT 1 FROM tasks WHERE task_id='CO-8'").fetchone():
        return {"backfilled": False, "reason": "task_not_present"}

    original_head = "94f03c6fb485bd0959eff9070a50c9356218f3ee"
    resolved_sha = "0b960517fdc9f1a9b269fc77e796e776edf4ed8c"
    created_at = 1784008391.571583
    now = time.time()
    verdict_created = c.execute(
        "INSERT OR IGNORE INTO review_verdicts("
        "verdict_id, task_id, pr_url, head_sha, reviewer_principal, "
        "reviewer_principal_id, status, source, created_at, recorded_at) "
        "VALUES (?,?,?,?,?,NULL,'changes_requested',?,?,?)",
        (
            HISTORICAL_CO8_VERDICT_ID, "CO-8",
            "https://github.com/6th-Element-Labs/projectplanner/pull/441",
            original_head, "codex-co-8-merge-review-20260714",
            "historical_backfill", created_at, now,
        ),
    ).rowcount > 0
    findings = [
        {
            "id": "CO8-REVIEW-1",
            "location": "src/switchboard/domain/provider_capacity/state_machine.py:193",
            "category": "authorization",
            "severity": "high",
            "invariant": "Denial, revocation, billing, and capacity signals override an explicit ready state.",
            "repair": "Evaluate denial and cooldown signals before accepting explicit ready state.",
            "reason": "Remediation reordered classification so fail-closed signals take precedence.",
        },
        {
            "id": "CO8-REVIEW-2",
            "location": "src/switchboard/domain/provider_capacity/policy.py:46",
            "category": "cost_policy",
            "severity": "high",
            "invariant": "Unknown or aliased metered lanes never become free personal-subscription capacity.",
            "repair": "Allowlist personal lanes, canonicalize metered aliases, and deny unknown lane kinds.",
            "reason": "Remediation added explicit personal and metered allowlists with unknown-lane denial.",
        },
        {
            "id": "CO8-REVIEW-3",
            "location": "src/switchboard/storage/repositories/provider_capacity.py:43",
            "category": "secret_redaction",
            "severity": "high",
            "invariant": "Durable checkpoints never persist commands that can embed provider credentials.",
            "repair": "Remove last_command from the checkpoint allowlist and cover secret-bearing commands.",
            "reason": "Remediation removed command persistence and added a raw-secret regression fixture.",
        },
        {
            "id": "CO8-REVIEW-4",
            "location": "src/switchboard/storage/repositories/provider_capacity.py:554",
            "category": "lease_concurrency",
            "severity": "high",
            "invariant": "Expired capacity-poll leases are reclaimable and stale generations cannot commit.",
            "repair": "Reclaim expired started polls with an incremented attempt fence and validate it on completion.",
            "reason": "Remediation added lease-expiry reclaim and attempt-generation fencing.",
        },
    ]
    inserted = 0
    for finding in findings:
        inserted += max(0, c.execute(
            "INSERT OR IGNORE INTO review_findings("
            "verdict_id, task_id, finding_id, location, category, severity, "
            "invariant_violated, repair_requirement, finding_class, state, resolved_by, "
            "resolved_reason, resolved_sha, created_at, updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,'fixed',?,?,?,?,?)",
            (
                HISTORICAL_CO8_VERDICT_ID, "CO-8", finding["id"], finding["location"],
                finding["category"], finding["severity"], finding["invariant"],
                finding["repair"], "auto", "codex-co-8-remediation-20260714",
                finding["reason"], resolved_sha, created_at, now,
            ),
        ).rowcount)
    if verdict_created or inserted:
        c.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
            "VALUES ('CO-8','migration:COORD-18','review.verdict_backfilled',?,?)",
            (json.dumps({
                "verdict_id": HISTORICAL_CO8_VERDICT_ID,
                "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/441",
                "head_sha": original_head,
                "status": "changes_requested",
                "finding_count": 4,
                "resolved_sha": resolved_sha,
                "source_task_id": "COORD-18",
            }, sort_keys=True), now),
        )
    return {"backfilled": bool(verdict_created or inserted),
            "verdict_created": verdict_created, "finding_count": inserted}


default_review_verdict_repository = ReviewVerdictRepository()


def record_review_verdict(data: Mapping[str, Any], *, actor: str, principal_id: str = "",
                          project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return default_review_verdict_repository.record(
        data, actor=actor, principal_id=principal_id, project=project)


def get_review_verdict(task_id: str, *, head_sha: str = "", pr_url: str = "",
                       project: str = DEFAULT_PROJECT) -> Optional[dict[str, Any]]:
    return default_review_verdict_repository.get(
        task_id, head_sha=head_sha, pr_url=pr_url, project=project)


def list_review_findings(*, task_id: str = "", head_sha: str = "", state: str = "",
                         finding_class: str = "", severity: str = "",
                         current_head_only: bool = False,
                         project: str = DEFAULT_PROJECT) -> list[dict[str, Any]]:
    return default_review_verdict_repository.list_findings(
        task_id=task_id, head_sha=head_sha, state=state, finding_class=finding_class,
        severity=severity, current_head_only=current_head_only, project=project)


def resolve_review_finding(data: Mapping[str, Any], *, actor: str,
                           principal_id: str = "", authorized: bool = False,
                           project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return default_review_verdict_repository.resolve_finding(
        data, actor=actor, principal_id=principal_id, authorized=authorized,
        project=project)


__all__ = [
    "HISTORICAL_CO8_VERDICT_ID",
    "REVIEW_MAX_ROUNDS",
    "REVIEW_MERGE_GATE_SCHEMA",
    "REVIEW_SUMMARY_SCHEMA",
    "ReviewVerdictError",
    "ReviewVerdictRepository",
    "default_review_verdict_repository",
    "ensure_historical_review_backfills_in",
    "get_review_verdict",
    "list_review_findings",
    "record_review_verdict",
    "resolve_review_finding",
    "review_merge_gate",
    "review_merge_gate_findings",
    "review_verdict_summary_in",
]
