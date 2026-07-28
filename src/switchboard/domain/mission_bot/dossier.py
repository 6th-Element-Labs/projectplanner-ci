"""Package GitHub/Switchboard facts into an unchanged mission dossier.

The Mission Bot copies facts. It does not diagnose them. Every remediation boot
must carry the full evidence identity: finding codes, failing contexts, check
URLs, and summaries.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from switchboard.domain.mission_bot.outputs import MISSION_DOSSIER_SCHEMA


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _text_raw(value: Any) -> str:
    return str(value or "").strip()


def _finding_rows(findings: Any) -> list[dict[str, Any]]:
    if isinstance(findings, Mapping) or isinstance(findings, (str, bytes)):
        return []
    if not isinstance(findings, Sequence):
        return []
    rows: list[dict[str, Any]] = []
    for item in findings:
        if isinstance(item, Mapping) and item.get("blocking") is not False:
            rows.append(dict(item))
    return rows


def _failing_check_identity(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Collect failing required-context identity; never drop URL/summary."""
    required = list(snapshot.get("required_status_contexts") or [])
    contexts = _map(snapshot.get("status_contexts"))
    failing: list[tuple[str, dict[str, Any]]] = []
    for name in required:
        row = _map(contexts.get(name))
        state = _text(row.get("conclusion") or row.get("state") or row.get("status")).lower()
        if not row or not state:
            continue
        if state in {
            "failure", "failed", "error", "timed_out", "action_required",
            "cancelled", "canceled", "stale", "startup_failure",
        }:
            failing.append((_text_raw(name), row))
    if not failing:
        return {}
    ordered = sorted(failing, key=lambda item: item[0])
    identity: dict[str, Any] = {
        "failing_contexts": [name for name, _ in ordered],
    }
    _, representative = ordered[0]
    url = _text_raw(
        representative.get("target_url") or representative.get("url")
        or representative.get("details_url") or representative.get("detailsUrl")
        or representative.get("run_url")
    )
    if url:
        identity["failing_check_url"] = url
    try:
        run_attempt = int(
            representative.get("run_attempt")
            or representative.get("runAttempt")
            or 0
        )
    except (TypeError, ValueError):
        run_attempt = 0
    if run_attempt > 0:
        identity["failing_run_attempt"] = run_attempt
    summary = _text_raw(
        representative.get("description") or representative.get("summary")
        or representative.get("output_title")
    )
    if summary:
        identity["failing_check_summary"] = summary
    return identity


def build_dossier(
    snapshot: Mapping[str, Any],
    *,
    reason_code: str,
    mission: str,
) -> dict[str, Any]:
    """Build the full mission dossier from live facts.

    Findings and CI identity are always merged. Classifier-shaped early returns
    that drop check URLs are forbidden here.
    """
    snap = _map(snapshot)
    findings = _finding_rows(snap.get("findings"))
    review = _map(snap.get("review"))
    for item in _finding_rows(review.get("findings")):
        findings.append(item)
    identity = _failing_check_identity(snap)
    if identity.get("failing_contexts") and not findings:
        findings.append({
            "code": reason_code or "required_ci_failed",
            "message": identity.get("failing_check_summary") or reason_code,
            "finding_class": "automatic",
            "blocking": True,
            "failing_contexts": list(identity.get("failing_contexts") or []),
            "failing_check_url": identity.get("failing_check_url") or "",
        })
    # Attach CI identity onto the first finding so boot prompts always see it.
    if findings and identity:
        head = dict(findings[0])
        for key, value in identity.items():
            if value not in (None, "", [], {}):
                head.setdefault(key, value)
        findings[0] = head
    agent_blocker = _map(_map(_map(snap.get("work_session")).get("hygiene")).get("blocker"))
    missing_artifact = _missing_artifact_from_findings(findings)
    return {
        "schema": MISSION_DOSSIER_SCHEMA,
        "mission": mission,
        "reason_code": reason_code,
        "task_id": _text(snap.get("task_id")).upper(),
        "pr_number": int(snap.get("pr_number") or 0),
        "pr_url": _text_raw(snap.get("pr_url")),
        "head_sha": _text(snap.get("head_sha")).lower(),
        "board_status": _text_raw(snap.get("board_status")),
        "acceptance_findings": findings,
        "missing_artifact": missing_artifact or None,
        "failing_contexts": list(identity.get("failing_contexts") or []),
        "failing_check_url": identity.get("failing_check_url") or "",
        "failing_run_attempt": int(identity.get("failing_run_attempt") or 0),
        "failing_check_summary": identity.get("failing_check_summary") or "",
        "merge_queue": _map(snap.get("merge_queue")),
        "github_pr": {
            "state": _text(_map(snap.get("github_pr")).get("state")),
            "draft": bool(_map(snap.get("github_pr")).get("draft")),
            "mergeable": _map(snap.get("github_pr")).get("mergeable"),
            "merge_state": _text(
                _map(snap.get("github_pr")).get("mergeStateStatus")
                or _map(snap.get("github_pr")).get("mergeable_state")
                or _map(snap.get("github_pr")).get("merge_state")
            ),
        },
        "agent_blocker": agent_blocker or None,
    }


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


__all__ = ["build_dossier"]
