"""Shared typed executed-test evidence command for REST and MCP (COORD-62).

The pydantic contract is the tool boundary: a malformed call (missing hash, empty
commands, bad hash format, caller-stamped completed_at) fails validation here and
never reaches storage. A valid call lands on both evidence read surfaces in one
transaction and the response carries the executed-test gate's verdict for the
session immediately — plus what merge authorization still lacks from the local
view, so the runner gets the desktop-agent feedback loop at machine speed.
"""
from __future__ import annotations

from typing import Any, Mapping

from pydantic import ValidationError

from switchboard.contracts import validation_error_message
from switchboard.contracts.evidence import RecordExecutedTestRunCommand
from switchboard.storage.repositories import work_sessions as work_sessions_repo


def _merge_authorization_hint(result: dict[str, Any], project: str) -> list[str]:
    """Local-view blockers still standing after this evidence write.

    Only what the coordinator can see without touching GitHub: PR state and CI
    contexts are computed off-box by the publisher and deliberately not guessed at
    here.
    """
    lacks: list[str] = []
    session = result.get("work_session") or {}
    hygiene = session.get("hygiene") or {}
    if not (hygiene.get("repo_preflight") or {}):
        lacks.append("missing_work_session_preflight")
    task_id = str(result.get("task_id") or "").strip()
    head_sha = str((result.get("run") or {}).get("head_sha") or "").strip()
    try:
        from switchboard.application.queries import review_verdicts as review_queries
        verdict = review_queries.get_for(task_id, project=project, head_sha=head_sha)
    except Exception:  # the hint must never break the recording it annotates
        verdict = None
    if not verdict or str(verdict.get("status") or "").lower() != "pass":
        lacks.append("review_required")
    return lacks


def _result_message(result: dict[str, Any], project: str) -> str:
    gate = result.get("executed_test_gate") or {}
    session_id = result.get("work_session_id") or ""
    if not gate.get("ok"):
        reason = gate.get("reason") or "invalid_executed_test_run"
        return (
            f"Recorded, but the executed-test gate is NOT green for {session_id}: "
            f"{reason}. See executed_test_gate.problems."
        )
    lacks = _merge_authorization_hint(result, project)
    if lacks:
        return (
            f"Executed-test evidence accepted; the executed-test gate is green for "
            f"{session_id}. Merge authorization still lacks (local view): "
            + ", ".join(lacks)
            + ". PR state and CI contexts are evaluated off-box."
        )
    return (
        f"Executed-test evidence accepted; the executed-test gate is green for "
        f"{session_id}. No further local blockers are visible; PR state and CI "
        "contexts are evaluated off-box."
    )


def execute_mapping(
        data: Mapping[str, Any], *, actor: str, principal_id: str = "",
        project: str,
        raise_errors: bool = False) -> dict[str, Any]:
    """Validate at the boundary, then record onto both evidence surfaces."""
    try:
        command = RecordExecutedTestRunCommand.from_mapping(data)
    except ValidationError as exc:
        if raise_errors:
            raise
        return {
            "error": "invalid_executed_test_run",
            "error_code": "invalid_executed_test_run",
            "message": validation_error_message(exc),
            "repair_tool": "record_executed_test_run",
        }
    result = work_sessions_repo.record_executed_test_run(
        command.to_repository_data(), actor=actor, principal_id=principal_id,
        project=project,
    )
    if result.get("recorded"):
        result["merge_authorization_lacks"] = _merge_authorization_hint(result, project)
        result["message"] = _result_message(result, project)
    return result
