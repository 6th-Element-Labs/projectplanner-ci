"""Pure routing of authenticated exact-head review findings."""
from __future__ import annotations

from .contracts import MissionResult


def route_review_findings(
        findings: list[dict[str, object]], *, round_no: int,
) -> dict[str, object]:
    """Classify one exact-head verdict; round_no is returned as telemetry only."""
    open_findings = [
        row for row in findings
        if str(row.get("state") or "open") == "open"
    ]
    automatic = [row for row in open_findings if row.get("class") == "auto"]
    escalations = [
        row for row in open_findings if row.get("class") == "escalate"
    ]
    result: MissionResult = (
        "human" if escalations else "continue" if automatic else "wait"
    )
    return {
        "result": result,
        "reason": (
            "escalate_findings_require_human" if escalations else
            "auto_findings_ready" if automatic else
            "no_auto_findings"
        ),
        "requested_role": "remediation" if automatic else None,
        "round_no": round_no,
        "human_required": bool(escalations),
        "automatic": automatic,
        "escalations": escalations,
    }


__all__ = ["route_review_findings"]
