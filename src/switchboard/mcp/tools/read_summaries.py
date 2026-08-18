"""Small, stable MCP projections for status-oriented reads.

The application/query layer remains the full-fidelity source of truth.  These
helpers only shape the default MCP response; callers can request ``full=True``
from each tool when they need the complete record.
"""
from __future__ import annotations

from typing import Any, Iterable


def _pick(record: Any, fields: Iterable[str]) -> Any:
    if not isinstance(record, dict):
        return record
    return {field: record[field] for field in fields if field in record}


def task_execution(value: Any) -> Any:
    if not isinstance(value, dict) or value.get("error"):
        return value
    result = _pick(value, (
        "schema", "project", "task_id", "execution_id", "lifecycle_phase",
        "running", "starting", "resumable_review", "has_ended_session",
        "available_commands", "panel", "next_execution",
    ))
    execution = value.get("execution")
    if isinstance(execution, dict):
        compact = _pick(execution, (
            "schema", "task_id", "lifecycle_phase", "running", "starting",
            "active_claim", "active_work_session", "active_wake",
        ))
        for source, target in (
            ("active_attempt", "active_attempt"),
            ("active_runner", "active_runner"),
        ):
            row = execution.get(source)
            if isinstance(row, dict):
                runner = row.get("runner")
                compact_row = _pick(row, (
                    "attempt_id", "generation", "status", "started_at",
                    "ended_at", "failure_reason",
                ))
                if isinstance(runner, dict):
                    compact_row["runner"] = _pick(runner, (
                        "runner_session_id", "agent_id", "host_id", "runtime",
                        "status", "task_id", "heartbeat_at", "stale",
                        "execution", "work_session_id",
                    ))
                compact[target] = compact_row
        task = execution.get("task")
        if isinstance(task, dict):
            compact["task"] = _pick(task, (
                "task_id", "title", "status", "assignee", "workstream",
                "updated_at",
            ))
        result["execution"] = compact
    return result


def autopilot(value: Any) -> Any:
    if not isinstance(value, dict) or value.get("error"):
        return value
    result = _pick(value, (
        "schema", "project", "deliverable_id", "profile_id", "scope_count",
    ))
    scopes = value.get("scopes")
    if isinstance(scopes, list):
        result["scopes"] = [
            _pick(scope, (
                "scope_id", "scope_type", "status", "deliverable_id",
                "task_project", "task_id", "runtime", "profile_id",
                "generation", "reason_code", "route", "agent_id",
                "created_at", "updated_at", "paused_at", "stopped_at",
            ))
            for scope in scopes
        ]
        result.setdefault("scope_count", len(scopes))
    return result


def task_session(value: Any) -> Any:
    if not isinstance(value, dict) or value.get("error"):
        return value
    result = _pick(value, (
        "schema", "project", "project_id", "task_id", "status",
        "lifecycle_phase", "running", "starting", "reason_code", "route",
        "available_commands", "recommended_repair",
    ))
    for field in (
        "claim", "work_session", "wake", "runner", "execution",
        "session_health", "identity",
    ):
        row = value.get(field)
        if isinstance(row, dict):
            result[field] = _pick(row, (
                "schema", "claim_id", "work_session_id", "wake_id",
                "runner_session_id", "execution_id", "agent_id", "host_id",
                "runtime", "role", "status", "lifecycle_phase", "running",
                "starting", "branch", "head_sha", "base_sha", "repo_role",
                "storage_mode", "worktree_path", "expires_at", "heartbeat_at",
                "blocking", "finding_count", "recommended_repair",
            ))
    return result


def runner_sessions(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    return [
        _pick(row, (
            "runner_session_id", "task_id", "agent_id", "host_id", "runtime",
            "status", "stale", "heartbeat_at", "started_at", "ended_at",
            "work_session_id", "execution_id", "execution_generation",
            "execution_role", "source_sha", "branch",
        ))
        for row in value
    ]
