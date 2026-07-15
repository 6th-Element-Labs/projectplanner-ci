"""Durable preflight prediction runs + calibration joins (SESSION-15).

``repo_preflight`` stays side-effect-free. Callers (``preflight_work_session``,
managed create) persist predictions here. Calibration joins against
``task_git_state``, ``external_ci_runs``, and ``merge.gate`` activity —
never inventing green when outcome rows are missing.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional

from constants import (
    DEFAULT_PROJECT,
    PREFLIGHT_CALIBRATION_SCHEMA,
    PREFLIGHT_FINDING_SCHEMA,
    PREFLIGHT_RUN_SCHEMA,
)


def _conn(project: str = DEFAULT_PROJECT):
    from db.connection import _conn as conn_impl
    return conn_impl(project)


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, default=str)


def _run_from_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "schema": PREFLIGHT_RUN_SCHEMA,
        "run_id": row["run_id"],
        "task_id": row["task_id"] or "",
        "work_session_id": row["work_session_id"] or "",
        "claim_id": row["claim_id"] or "",
        "agent_id": row["agent_id"] or "",
        "head_sha": row["head_sha"] or "",
        "base_sha": row["base_sha"] or "",
        "branch": row["branch"] or "",
        "repo_role": row["repo_role"] or "canonical",
        "repo_path": row["repo_path"] or "",
        "verdict": row["verdict"] or "",
        "ok": bool(row["ok"]),
        "finding_count": int(row["finding_count"] or 0),
        "blocking_count": int(row["blocking_count"] or 0),
        "source": row["source"] or "",
        "actor": row["actor"] or "",
        "created_at": float(row["created_at"] or 0),
    }


def _finding_from_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        details = json.loads(row["details_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        details = {}
    finding = {
        "schema": PREFLIGHT_FINDING_SCHEMA,
        "run_id": row["run_id"],
        "finding_seq": int(row["finding_seq"] or 0),
        "code": row["code"] or "",
        "failure_class": row["failure_class"] or "",
        "severity": row["severity"] or "",
        "blocking": bool(row["blocking"]),
        "message": row["message"] or "",
        "remediation": row["remediation"] or "",
        "details": details if isinstance(details, dict) else {},
    }
    return finding


def record_preflight_run(
    report: Mapping[str, Any],
    *,
    work_session_id: str = "",
    claim_id: str = "",
    actor: str = "system",
    source: str = "preflight_work_session",
    project: str = DEFAULT_PROJECT,
) -> Dict[str, Any]:
    """Persist one preflight report (all findings, blocking and warn-only)."""
    if not isinstance(report, Mapping):
        return {"error": "preflight_report_required"}
    head_sha = str(report.get("head_sha") or "").strip()
    if not head_sha:
        # Detached / missing HEAD — still record under a sentinel so we keep the
        # failing signal rather than dropping the prediction (fail_loud).
        head_sha = "unknown"
    findings = list(report.get("findings") or [])
    blocking_count = sum(1 for f in findings if isinstance(f, Mapping) and f.get("blocking"))
    run_id = "preflightrun-" + uuid.uuid4().hex[:16]
    now = time.time()
    with _conn(project) as c:
        c.execute(
            "INSERT INTO preflight_runs("
            "run_id, task_id, work_session_id, claim_id, agent_id, head_sha, base_sha, "
            "branch, repo_role, repo_path, verdict, ok, finding_count, blocking_count, "
            "source, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                str(report.get("task_id") or "").strip().upper() or None,
                (work_session_id or "").strip() or None,
                (claim_id or "").strip() or None,
                str(report.get("agent_id") or "").strip() or None,
                head_sha,
                str(report.get("base_sha") or "").strip() or None,
                str(report.get("branch") or "").strip() or None,
                str(report.get("repo_role") or "canonical").strip() or "canonical",
                str(report.get("repo_path") or "").strip() or None,
                str(report.get("verdict") or "deny").strip() or "deny",
                1 if report.get("ok") else 0,
                len(findings),
                int(blocking_count),
                (source or "preflight_work_session").strip() or "preflight_work_session",
                (actor or "system").strip() or "system",
                now,
            ),
        )
        for seq, raw in enumerate(findings):
            if not isinstance(raw, Mapping):
                continue
            details = {k: v for k, v in raw.items()
                       if k not in {"code", "failure_class", "severity", "blocking",
                                    "message", "remediation", "schema"}}
            c.execute(
                "INSERT INTO preflight_findings("
                "run_id, finding_seq, code, failure_class, severity, blocking, "
                "message, remediation, details_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    seq,
                    str(raw.get("code") or "").strip() or f"finding_{seq}",
                    str(raw.get("failure_class") or "").strip() or "unknown",
                    str(raw.get("severity") or "medium").strip() or "medium",
                    1 if raw.get("blocking") else 0,
                    str(raw.get("message") or "").strip() or "",
                    str(raw.get("remediation") or "").strip() or "",
                    _json_dumps(details),
                ),
            )
        run_row = c.execute(
            "SELECT * FROM preflight_runs WHERE run_id=?", (run_id,),
        ).fetchone()
        finding_rows = c.execute(
            "SELECT * FROM preflight_findings WHERE run_id=? ORDER BY finding_seq",
            (run_id,),
        ).fetchall()
    return {
        "recorded": True,
        "run": _run_from_row(run_row),
        "findings": [_finding_from_row(r) for r in finding_rows],
    }


def get_preflight_run(run_id: str, *, project: str = DEFAULT_PROJECT
                      ) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        row = c.execute(
            "SELECT * FROM preflight_runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if not row:
            return None
        findings = c.execute(
            "SELECT * FROM preflight_findings WHERE run_id=? ORDER BY finding_seq",
            (run_id,),
        ).fetchall()
    result = _run_from_row(row)
    result["findings"] = [_finding_from_row(r) for r in findings]
    return result


def list_preflight_runs(*, task_id: str = "", head_sha: str = "",
                        work_session_id: str = "", limit: int = 50,
                        project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    where: List[str] = []
    args: List[Any] = []
    if task_id:
        where.append("task_id=?")
        args.append(task_id.strip().upper())
    if head_sha:
        where.append("head_sha=?")
        args.append(head_sha.strip())
    if work_session_id:
        where.append("work_session_id=?")
        args.append(work_session_id.strip())
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(int(limit or 50), 200))
    with _conn(project) as c:
        rows = c.execute(
            f"SELECT * FROM preflight_runs{clause} "
            f"ORDER BY created_at DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
    return [_run_from_row(r) for r in rows]


def preflight_calibration(*, code: str = "", since: float = 0.0,
                          min_outcomes: int = 3,
                          project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Join predictions to merge/CI/merge-gate outcomes per finding code.

    Missing outcomes stay as ``open_predictions`` — never treated as green.
    """
    min_outcomes = max(1, int(min_outcomes or 3))
    since = float(since or 0.0)
    code_filter = (code or "").strip()
    with _conn(project) as c:
        params: List[Any] = []
        where = ["1=1"]
        if since > 0:
            where.append("r.created_at >= ?")
            params.append(since)
        if code_filter:
            where.append("f.code = ?")
            params.append(code_filter)
        rows = c.execute(
            "SELECT f.code, f.failure_class, f.blocking, f.severity, "
            "r.task_id, r.head_sha, r.verdict, r.created_at, "
            "g.merged_sha, g.head_sha AS git_head_sha, "
            "ci.conclusion AS ci_conclusion, ci.failure_class AS ci_failure_class "
            "FROM preflight_findings f "
            "JOIN preflight_runs r ON r.run_id = f.run_id "
            "LEFT JOIN task_git_state g ON g.task_id = r.task_id "
            "LEFT JOIN ("
            "  SELECT task_id, source_sha, conclusion, failure_class, "
            "         ROW_NUMBER() OVER ("
            "           PARTITION BY task_id, source_sha ORDER BY requested_at DESC"
            "         ) AS rn "
            "  FROM external_ci_runs"
            ") ci ON ci.task_id = r.task_id AND ci.source_sha = r.head_sha AND ci.rn = 1 "
            f"WHERE {' AND '.join(where)}",
            params,
        ).fetchall()

        # Latest merge.gate activity per (task_id, head_sha) — scan activity JSON.
        gate_blocks: Dict[tuple, bool] = {}
        activity_rows = c.execute(
            "SELECT task_id, payload, created_at FROM activity "
            "WHERE kind='merge.gate' ORDER BY created_at DESC LIMIT 2000",
        ).fetchall()
    for act in activity_rows:
        try:
            payload = json.loads(act["payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        tid = str(act["task_id"] or payload.get("task_id") or "").strip().upper()
        sha = str(payload.get("head_sha") or "").strip()
        if not tid or not sha:
            continue
        key = (tid, sha)
        if key in gate_blocks:
            continue
        status = str(payload.get("status") or payload.get("verdict") or "").lower()
        ok = payload.get("ok")
        blocked = (ok is False) or status in {"deny", "red", "blocked", "fail", "failed"}
        gate_blocks[key] = bool(blocked)

    by_code: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        code_key = row["code"] or "unknown"
        bucket = by_code.setdefault(code_key, {
            "code": code_key,
            "failure_class": row["failure_class"] or "",
            "predictions": 0,
            "blocking_predictions": 0,
            "warn_predictions": 0,
            "open_predictions": 0,
            "merged_after": 0,
            "ci_failures_after": 0,
            "merge_gate_blocks_after": 0,
            "outcome_count": 0,
        })
        bucket["predictions"] += 1
        if row["blocking"]:
            bucket["blocking_predictions"] += 1
        else:
            bucket["warn_predictions"] += 1

        tid = str(row["task_id"] or "").strip().upper()
        sha = str(row["head_sha"] or "").strip()
        merged = bool(row["merged_sha"])
        ci_conclusion = str(row["ci_conclusion"] or "").lower()
        ci_failed = ci_conclusion in {"failure", "failed", "cancelled", "timed_out", "error"}
        gate_blocked = gate_blocks.get((tid, sha), False) if tid and sha else False

        has_outcome = merged or bool(ci_conclusion) or ((tid, sha) in gate_blocks)
        if not has_outcome:
            bucket["open_predictions"] += 1
            continue
        bucket["outcome_count"] += 1
        if merged:
            bucket["merged_after"] += 1
        if ci_failed:
            bucket["ci_failures_after"] += 1
        if gate_blocked:
            bucket["merge_gate_blocks_after"] += 1

    codes_out: List[Dict[str, Any]] = []
    for bucket in sorted(by_code.values(), key=lambda b: (-b["predictions"], b["code"])):
        outcomes = int(bucket["outcome_count"])
        signal_hits = int(bucket["ci_failures_after"]) + int(bucket["merge_gate_blocks_after"])
        if outcomes < min_outcomes:
            recommendation = "insufficient_outcomes"
            hit_rate = None
        else:
            hit_rate = round(signal_hits / outcomes, 4)
            # Predictive of a real gate/CI failure → keep blocking when commonly used as block.
            if bucket["blocking_predictions"] > 0 and hit_rate >= 0.3:
                recommendation = "keep_blocking"
            elif bucket["blocking_predictions"] > 0 and hit_rate < 0.15:
                recommendation = "consider_warn"
            elif hit_rate >= 0.3:
                recommendation = "consider_blocking"
            else:
                recommendation = "keep_warn"
        bucket["signal_hits"] = signal_hits
        bucket["hit_rate"] = hit_rate
        bucket["recommendation"] = recommendation
        codes_out.append(bucket)

    return {
        "schema": PREFLIGHT_CALIBRATION_SCHEMA,
        "project": project,
        "code_filter": code_filter,
        "since": since,
        "min_outcomes": min_outcomes,
        "code_count": len(codes_out),
        "codes": codes_out,
        "notes": [
            "open_predictions have no merge/CI/merge-gate outcome yet — not treated as green",
            "hit_rate = (ci_failures_after + merge_gate_blocks_after) / outcome_count",
            "recommendation uses min_outcomes before advising keep_blocking/consider_warn",
        ],
    }


default_preflight_run_repository = None  # module-level functions are the API


__all__ = [
    "get_preflight_run",
    "list_preflight_runs",
    "preflight_calibration",
    "record_preflight_run",
]
