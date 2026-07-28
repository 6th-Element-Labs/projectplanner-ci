"""Package GitHub/Switchboard facts into an unchanged mission dossier.

The Mission Bot copies facts. It does not diagnose, compress, or truncate them.
Every boot receives the full nested evidence identity.
"""
from __future__ import annotations

import copy
from typing import Any, Mapping, Sequence

from switchboard.domain.mission_bot.outputs import MISSION_DOSSIER_SCHEMA


_FAILED = {
    "failure", "failed", "error", "timed_out", "action_required",
    "cancelled", "canceled", "stale", "startup_failure",
}


def _map(value: Any) -> dict[str, Any]:
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _text_raw(value: Any) -> str:
    return str(value or "").strip()


def _deepcopy_rows(value: Any) -> list[Any]:
    if isinstance(value, Mapping) or isinstance(value, (str, bytes)):
        return []
    if not isinstance(value, Sequence):
        return []
    return [copy.deepcopy(item) for item in value]


def _finding_rows(findings: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in _deepcopy_rows(findings):
        if isinstance(item, Mapping) and item.get("blocking") is not False:
            rows.append(dict(item))
    return rows


def _failing_checks(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every failing required context, unchanged — not a single representative."""
    required = list(snapshot.get("required_status_contexts") or [])
    contexts = _map(snapshot.get("status_contexts"))
    failing: list[dict[str, Any]] = []
    for name in required:
        row = _map(contexts.get(name))
        state = _text(
            row.get("conclusion") or row.get("state") or row.get("status")
        ).lower().replace("-", "_").replace(" ", "_")
        if not row or state not in _FAILED:
            continue
        entry = dict(row)
        entry.setdefault("context", _text_raw(name))
        entry.setdefault("name", _text_raw(name))
        failing.append(entry)
    failing.sort(key=lambda item: str(item.get("context") or item.get("name") or ""))
    return failing


def _check_url(row: Mapping[str, Any]) -> str:
    return _text_raw(
        row.get("target_url") or row.get("url")
        or row.get("details_url") or row.get("detailsUrl")
        or row.get("run_url")
    )


def _check_summary(row: Mapping[str, Any]) -> str:
    return _text_raw(
        row.get("description") or row.get("summary") or row.get("output_title")
    )


def _run_attempt(row: Mapping[str, Any]) -> int:
    try:
        return int(row.get("run_attempt") or row.get("runAttempt") or 0)
    except (TypeError, ValueError):
        return 0


def evidence_identity(dossier: Mapping[str, Any]) -> dict[str, Any]:
    """Stable evidence keys that must enter mission idempotency."""
    failing = list(dossier.get("failing_checks") or [])
    findings = list(dossier.get("acceptance_findings") or [])
    return {
        "failing_contexts": [
            str(item.get("context") or item.get("name") or "")
            for item in failing
            if isinstance(item, Mapping)
        ],
        "failing_check_urls": sorted({
            _check_url(item) for item in failing
            if isinstance(item, Mapping) and _check_url(item)
        }),
        "failing_run_attempts": sorted({
            _run_attempt(item) for item in failing
            if isinstance(item, Mapping) and _run_attempt(item) > 0
        }),
        "finding_codes": [
            str(item.get("code") or "")
            for item in findings
            if isinstance(item, Mapping) and item.get("code")
        ],
        "missing_artifact_expected_key": str(
            _map(dossier.get("missing_artifact")).get("expected_key") or ""
        ),
    }


def build_dossier(
    snapshot: Mapping[str, Any],
    *,
    reason_code: str,
    mission: str,
) -> dict[str, Any]:
    """Copy live facts into the mission dossier without compression."""
    snap = _map(snapshot)
    findings = _finding_rows(snap.get("findings"))
    review = _map(snap.get("review"))
    for item in _finding_rows(review.get("findings")):
        findings.append(item)
    failing_checks = _failing_checks(snap)
    if failing_checks and not findings:
        for row in failing_checks:
            findings.append({
                "code": reason_code or "required_ci_failed",
                "message": _check_summary(row) or reason_code,
                "finding_class": "automatic",
                "blocking": True,
                "failing_contexts": [
                    str(row.get("context") or row.get("name") or "")
                ],
                "failing_check_url": _check_url(row),
                "failing_run_attempt": _run_attempt(row),
                "failing_check_summary": _check_summary(row),
                "check": row,
            })
    # Attach per-check identity onto findings without dropping nested evidence.
    if findings and failing_checks:
        head = dict(findings[0])
        head.setdefault("failing_contexts", [
            str(item.get("context") or item.get("name") or "")
            for item in failing_checks
        ])
        head.setdefault("failing_check_url", _check_url(failing_checks[0]))
        head.setdefault("failing_run_attempt", _run_attempt(failing_checks[0]))
        head.setdefault("failing_check_summary", _check_summary(failing_checks[0]))
        head.setdefault("failing_checks", failing_checks)
        findings[0] = head
    agent_blocker = _map(_map(_map(snap.get("work_session")).get("hygiene")).get("blocker"))
    missing_artifact = _missing_artifact_from_findings(findings)
    dossier = {
        "schema": MISSION_DOSSIER_SCHEMA,
        "mission": mission,
        "reason_code": reason_code,
        "task_id": _text(snap.get("task_id")).upper(),
        "pr_number": int(snap.get("pr_number") or 0),
        "pr_url": _text_raw(snap.get("pr_url")),
        "head_sha": _text(snap.get("head_sha")).lower(),
        "board_status": _text_raw(snap.get("board_status")),
        # Full nested fact surfaces — unchanged copies.
        "task": _map(snap.get("task")),
        "github_pr": _map(snap.get("github_pr")),
        "required_status_contexts": list(snap.get("required_status_contexts") or []),
        "status_contexts": _map(snap.get("status_contexts")),
        "review": review,
        "merge_gate": _map(snap.get("merge_gate")),
        "merge_queue": _map(snap.get("merge_queue")),
        "merge_provenance": _map(snap.get("merge_provenance")),
        "work_session": _map(snap.get("work_session")),
        "runner": _map(snap.get("runner")),
        "dependency_state": _map(
            snap.get("dependency_state")
            or _map(snap.get("task")).get("dependency_state")
        ),
        "acceptance_findings": findings,
        "failing_checks": failing_checks,
        "missing_artifact": missing_artifact or None,
        "failing_contexts": [
            str(item.get("context") or item.get("name") or "")
            for item in failing_checks
        ],
        "failing_check_url": (
            _check_url(failing_checks[0]) if failing_checks else ""
        ),
        "failing_run_attempt": (
            _run_attempt(failing_checks[0]) if failing_checks else 0
        ),
        "failing_check_summary": (
            _check_summary(failing_checks[0]) if failing_checks else ""
        ),
        "agent_blocker": agent_blocker or None,
    }
    dossier["evidence_identity"] = evidence_identity(dossier)
    return dossier


def _missing_artifact_from_findings(
    findings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Lift the gate's missing_artifact report into the dossier unchanged."""
    for item in findings:
        row = _map(item)
        direct = _map(row.get("missing_artifact"))
        if direct:
            return direct
        nested = _map(_map(row.get("executed_test_gate")).get("missing_artifact"))
        if nested:
            return nested
    return {}


__all__ = ["build_dossier", "evidence_identity"]
