"""Submit-bug application command (ARCH-MS-59).

Owns BUG intake orchestration that previously lived in ``repositories/shell``.
Policy constants live in ``domain/bug_intake``; persistence goes through the task
repository / activity helpers only (no policy ownership in storage).

REST and MCP adapters both call :func:`execute_mapping_result`. Authentication
stays at the edge.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from constants import DEFAULT_PROJECT
from db.core import _parse_jsonish, _slug_token

from switchboard.domain.bug_intake.policy import (
    BUG_FAILURE_CLASSES,
    BUG_REPORT_REQUIRED_FIELDS,
    BUG_SEVERITIES,
    bug_report_description,
    bug_report_value_present,
    bug_title,
    fail_fix_signal_schema,
    failure_class_detail,
)

CreateTaskFn = Callable[..., Optional[dict[str, Any]]]
GetTaskFn = Callable[..., Optional[dict[str, Any]]]
SetAgentStateFn = Callable[..., Any]
AppendActivityFn = Callable[..., Any]

__all__ = ["execute", "execute_mapping_result", "submit_bug"]


def _deps():
    """Lazy store façade so adapters can inject fakes in tests later."""
    import store
    return store


def execute(
        data: dict[str, Any],
        *,
        actor: str = "agent",
        project: str = DEFAULT_PROJECT,
        create_task: Optional[CreateTaskFn] = None,
        get_task: Optional[GetTaskFn] = None,
        set_agent_state: Optional[SetAgentStateFn] = None,
        append_activity: Optional[AppendActivityFn] = None) -> dict[str, Any]:
    """Validate a bug report and create one BUG triage task with activity linkage."""
    store = _deps()
    create_task = create_task or store.create_task
    get_task = get_task or store.get_task
    set_agent_state = set_agent_state or store.set_agent_state
    append_activity = append_activity or store.append_activity

    payload = dict(data or {})
    missing = [field for field in BUG_REPORT_REQUIRED_FIELDS
               if not bug_report_value_present(payload.get(field))]
    source_agent = (payload.get("source_agent") or actor or "").strip()
    if not source_agent:
        missing.append("source_agent")
    if missing:
        return {
            "error": "missing_required_fields",
            "missing": sorted(set(missing)),
            "message": "submit_bug requires a complete report; no BUG task was created.",
        }

    source_task = str(payload.get("source_task") or "").strip().upper()
    duplicate_of = str(payload.get("duplicate_of") or "").strip().upper()
    severity = str(payload.get("severity_hint") or "").strip().lower()
    if severity not in BUG_SEVERITIES:
        return {
            "error": "invalid_severity_hint",
            "allowed": sorted(BUG_SEVERITIES),
            "message": "severity_hint must be low, medium, high, or critical.",
        }
    failure_class = _slug_token(str(payload.get("failure_class") or ""))
    if failure_class and failure_class not in BUG_FAILURE_CLASSES:
        return {
            "error": "invalid_failure_class",
            "allowed": sorted(BUG_FAILURE_CLASSES),
            "schema": fail_fix_signal_schema(),
            "message": "failure_class is optional, but supplied values must match fail_fix_signal.v1.",
        }
    failure_detail = failure_class_detail(failure_class) if failure_class else None

    source = get_task(source_task, project=project)
    if not source:
        return {
            "error": "unknown_source_task",
            "source_task": source_task,
            "message": "source_task must exist on this project; no BUG task was created.",
        }
    if duplicate_of:
        dup = get_task(duplicate_of, project=project)
        if not dup:
            return {
                "error": "unknown_duplicate_of",
                "duplicate_of": duplicate_of,
                "message": "duplicate_of must name an existing BUG task; no BUG task was created.",
            }
        if (dup.get("workstream_id") or dup.get("_wsId") or "").upper() != "BUG":
            return {
                "error": "duplicate_of_not_bug",
                "duplicate_of": duplicate_of,
                "message": "duplicate_of must point at a BUG task.",
            }

    now = time.time()
    report = {
        "schema": "bug_report.v1",
        "intake_status": "new",
        "source_task": source_task,
        "source_agent": source_agent,
        "reported_by": actor,
        "reported_at": now,
        "observed_behavior": str(payload.get("observed_behavior") or "").strip(),
        "expected_behavior": str(payload.get("expected_behavior") or "").strip(),
        "repro_steps": payload.get("repro_steps"),
        "evidence": _parse_jsonish(payload.get("evidence")),
        "severity_hint": severity,
        "affected_surface": str(payload.get("affected_surface") or "").strip(),
        "failure_class": failure_class or None,
        "failure_class_detail": failure_detail,
        "fail_fix_signal": {
            "schema": "fail_fix_signal.v1",
            "source": "submit_bug",
            "failure_class": failure_class or None,
            "severity": severity,
            "affected_surface": str(payload.get("affected_surface") or "").strip(),
            "observed_behavior": str(payload.get("observed_behavior") or "").strip(),
            "expected_behavior": str(payload.get("expected_behavior") or "").strip(),
            "repro_steps": payload.get("repro_steps"),
            "evidence": _parse_jsonish(payload.get("evidence")),
            "task_id": source_task,
            "expected_signal": (
                failure_detail or {}
            ).get("expected_signal") or str(payload.get("expected_behavior") or "").strip(),
        },
        "duplicate_of": duplicate_of or None,
    }
    task = create_task({
        "workstream_id": "BUG",
        "workstream_name": "BUG",
        "title": bug_title(report["affected_surface"], report["observed_behavior"],
                           str(payload.get("title") or "")),
        "description": bug_report_description(report),
        "status": "Triage",
        "phase": "Agent Intake P0",
        "owner_org": "6th Element Labs",
        "owner_person_or_role": "Bug Intake",
        "risk_level": BUG_SEVERITIES[severity],
        "depends_on": [],
    }, actor=actor, project=project)
    if not task:
        return {"error": "bug_task_not_created", "message": "BUG task creation failed."}

    full_state = set_agent_state(task["task_id"], "bug_report", report, project=project)
    report_event = {
        "bug_task_id": task["task_id"],
        "source_task": source_task,
        "source_agent": source_agent,
        "severity_hint": severity,
        "affected_surface": report["affected_surface"],
        "failure_class": report["failure_class"],
        "duplicate_of": duplicate_of or None,
        "evidence": report["evidence"],
    }
    append_activity("bug.submitted", actor, report_event,
                    task_id=task["task_id"], project=project)
    append_activity("bug.reported_from_task", actor, report_event,
                    task_id=source_task, project=project)
    bug = get_task(task["task_id"], project=project)
    return {"submitted": True, "bug": bug, "bug_report": report,
            "agent_state": full_state}


def execute_mapping_result(
        data: dict[str, Any],
        *,
        actor: str = "agent",
        project: str = DEFAULT_PROJECT,
        create_task: Optional[CreateTaskFn] = None,
        get_task: Optional[GetTaskFn] = None,
        set_agent_state: Optional[SetAgentStateFn] = None,
        append_activity: Optional[AppendActivityFn] = None) -> dict[str, Any]:
    """Execute adapter mapping input and return the structured submit_bug result."""
    payload = dict(data or {})
    project = payload.pop("project", None) or project or DEFAULT_PROJECT
    return execute(
        payload,
        actor=actor,
        project=project,
        create_task=create_task,
        get_task=get_task,
        set_agent_state=set_agent_state,
        append_activity=append_activity,
    )


submit_bug = execute_mapping_result
