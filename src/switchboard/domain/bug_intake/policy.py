"""Bug intake + fail-fix policy constants (ARCH-MS-59).

Moved from ``repositories/shell.py``. Pure domain policy — no SQL, no task
creation. ``application/commands/submit_bug`` owns orchestration; repositories
remain write-only helpers.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from db.core import _slug_token

BUG_INTAKE_POLICY = {
    "scope": "write:bug_intake",
    "agent_role": (
        "Receive agent-discovered bugs, normalize them into reproducible BUG reports, "
        "dedupe them, score severity, and route them into the normal task lifecycle."
    ),
    "allowed_automation": [
        "create or update BUG intake records through the dedicated bug-intake surface",
        "link duplicate BUG reports to a canonical BUG task",
        "request missing reproduction evidence from the reporting agent",
        "assign severity_hint and affected_surface on BUG intake records",
        "create implementation work outside the BUG lane",
        "mark converted implementation work Ready or claimable",
        "change priority, sort_order, is_blocking, or dependency-critical fields",
        "dispatch, claim, wake, or otherwise start implementation work",
    ],
    "conversion_policy": {
        "approval_required": False,
        "audit_required": True,
        "preserve_source_bug": True,
    },
}
BUG_REPORT_REQUIRED_FIELDS = [
    "source_task",
    "observed_behavior",
    "expected_behavior",
    "repro_steps",
    "evidence",
    "severity_hint",
    "affected_surface",
]
BUG_SEVERITIES = {"low": "Low", "medium": "Medium", "high": "High", "critical": "High"}
FAIL_FIX_REQUIRED_FIELDS = [
    "source",
    "failure_class",
    "severity",
    "affected_surface",
    "observed_behavior",
    "expected_behavior",
    "repro_steps",
    "evidence",
    "task_id",
]
FAIL_FIX_FAILURE_CLASSES = {
    "missing_data": {
        "label": "Missing data",
        "default_severity": "medium",
        "description": "A required field, artifact, status, or provenance signal is absent.",
        "expected_signal": "Required data is present before workflow execution continues.",
    },
    "broken_connection": {
        "label": "Broken connection",
        "default_severity": "medium",
        "description": "A network, GitHub, MCP, provider, or service dependency cannot be reached.",
        "expected_signal": "The dependency returns a structured response or a loud connection error.",
    },
    "invalid_input": {
        "label": "Invalid input",
        "default_severity": "medium",
        "description": "A caller supplied a known field with an invalid value or unsafe state transition.",
        "expected_signal": "The invalid value is rejected before downstream state changes.",
    },
    "stale_branch": {
        "label": "Stale branch",
        "default_severity": "high",
        "description": "Git or board state points at a stale, missing, or unreachable branch/SHA.",
        "expected_signal": "The current branch, head SHA, and canonical main proof are reachable.",
    },
    "absent_permission": {
        "label": "Absent permission",
        "default_severity": "high",
        "description": "A principal lacks the scope, token, approval, or policy authority for an action.",
        "expected_signal": "The action is denied with the missing authority named.",
    },
    "malformed_payload": {
        "label": "Malformed payload",
        "default_severity": "medium",
        "description": "A request or stored payload is syntactically malformed or cannot be decoded.",
        "expected_signal": "Payload shape is validated and malformed input fails closed.",
    },
    "failed_gate": {
        "label": "Failed gate",
        "default_severity": "high",
        "description": "A CI, QA, review, or lifecycle gate failed or was bypassed.",
        "expected_signal": "The gate failure is visible and blocks release/dispatch until repaired.",
    },
    "unreachable_agent": {
        "label": "Unreachable agent",
        "default_severity": "medium",
        "description": "A directed agent, runtime, or host could not be reached or did not ack.",
        "expected_signal": "Delivery, mailbox, wakeability, and fallback state are explicit.",
    },
    "unbound_identity": {
        "label": "Unbound identity",
        "default_severity": "high",
        "description": "Work was written by a shared/system principal without a bound active runtime.",
        "expected_signal": "The runtime identity is registered, bound, and visible to operators.",
    },
    "hidden_fallback": {
        "label": "Hidden fallback",
        "default_severity": "critical",
        "description": "A fallback, placeholder, or optimistic status masks the original failure.",
        "expected_signal": "Fallbacks are named and preserve a red/yellow auditable signal.",
    },
}
BUG_FAILURE_CLASSES = set(FAIL_FIX_FAILURE_CLASSES)


def bug_intake_policy() -> Dict[str, Any]:
    return json.loads(json.dumps(BUG_INTAKE_POLICY))


def bug_report_value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


# Historic private name kept for shell/store re-export compatibility.
_bug_report_value_present = bug_report_value_present


def fail_fix_signal_schema() -> Dict[str, Any]:
    return {
        "schema": "fail_fix_signal.v1",
        "required_fields": list(FAIL_FIX_REQUIRED_FIELDS),
        "failure_classes": {
            key: dict(value)
            for key, value in sorted(FAIL_FIX_FAILURE_CLASSES.items())
        },
        "reporting_rule": (
            "Preserve the original failing signal. Do not replace it with a placeholder, "
            "silent default, optimistic status, or hidden fallback."
        ),
        "visible_fallback_rule": (
            "Fallbacks are allowed only when they are named and leave an auditable "
            "red/yellow signal such as a BUG report, reconcile finding, monitor event, "
            "task comment, or blocker."
        ),
    }


def failure_class_detail(failure_class: str) -> Optional[Dict[str, Any]]:
    detail = FAIL_FIX_FAILURE_CLASSES.get(_slug_token(failure_class or ""))
    return dict(detail) if detail else None


_failure_class_detail = failure_class_detail


def bug_title(surface: str, observed: str, explicit_title: str = "") -> str:
    if explicit_title and explicit_title.strip():
        return explicit_title.strip()[:160]
    summary = " ".join((observed or "").strip().split())
    if not summary:
        summary = "agent-submitted bug"
    if len(summary) > 96:
        summary = summary[:93].rstrip() + "..."
    surface = (surface or "unknown surface").strip()
    return f"{surface}: {summary}"[:160]


_bug_title = bug_title


def bug_report_description(report: Dict[str, Any]) -> str:
    evidence = report.get("evidence")
    if isinstance(evidence, (dict, list)):
        evidence_text = json.dumps(evidence, indent=2, sort_keys=True)
    else:
        evidence_text = str(evidence or "")
    failure_detail = failure_class_detail(str(report.get("failure_class") or "")) or {}
    failure_label = failure_detail.get("label") or report.get("failure_class") or "(unspecified)"
    return "\n".join([
        f"Bug submitted by: {report.get('source_agent')}",
        f"Source task: {report.get('source_task')}",
        f"Affected surface: {report.get('affected_surface')}",
        f"Severity hint: {report.get('severity_hint')}",
        f"Failure class: {failure_label}",
        f"Expected fail-fix signal: {failure_detail.get('expected_signal') or '(unspecified)'}",
        f"Duplicate of: {report.get('duplicate_of') or '(none)'}",
        "",
        "Observed behavior:",
        str(report.get("observed_behavior") or ""),
        "",
        "Expected behavior:",
        str(report.get("expected_behavior") or ""),
        "",
        "Repro steps:",
        str(report.get("repro_steps") or ""),
        "",
        "Evidence:",
        evidence_text,
    ])


_bug_report_description = bug_report_description


__all__ = [
    "BUG_FAILURE_CLASSES",
    "BUG_INTAKE_POLICY",
    "BUG_REPORT_REQUIRED_FIELDS",
    "BUG_SEVERITIES",
    "FAIL_FIX_FAILURE_CLASSES",
    "FAIL_FIX_REQUIRED_FIELDS",
    "_bug_report_description",
    "_bug_report_value_present",
    "_bug_title",
    "_failure_class_detail",
    "bug_intake_policy",
    "fail_fix_signal_schema",
]
