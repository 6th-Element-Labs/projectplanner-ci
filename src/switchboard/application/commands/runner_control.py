"""Shared runner session / control commands for REST and MCP (ARCH-MS-67).

Adapters own auth and transport serialization. Persistence lives in
``repositories/runner``.
"""
from __future__ import annotations

from typing import Any, Optional

from constants import DEFAULT_PROJECT
from switchboard.storage.repositories import runner as runner_repo


def upsert_session_mapping_result(
        data: dict[str, Any], *, actor: str, principal_id: str = "") -> dict[str, Any]:
    """Register or heartbeat one supervised runner session."""
    record = dict(data or {})
    project = record.pop("project", None) or DEFAULT_PROJECT
    return runner_repo.upsert_runner_session(
        record, principal_id=principal_id, actor=actor, project=project)


def request_mapping_result(
        data: dict[str, Any], *, actor: str, principal_id: str = "") -> dict[str, Any]:
    """Request a host-side runner control action (snapshot/kill/restart/…)."""
    action = (data.get("action") or "").strip()
    options = data.get("options")
    if options is None:
        options = {}
    if not isinstance(options, dict):
        return {"error": "options must be a JSON object", "error_code": "invalid_options"}
    return runner_repo.request_runner_control(
        data.get("runner_session_id") or data.get("id") or "",
        action,
        reason=data.get("reason") or "",
        options=options,
        actor=actor,
        principal_id=principal_id,
        project=data.get("project") or DEFAULT_PROJECT,
    )


def claim_mapping_result(
        data: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """Agent Host claims a pending runner control request."""
    return runner_repo.claim_runner_control_request(
        (data.get("host_id") or "").strip(),
        (data.get("request_id") or data.get("id") or "").strip(),
        actor=actor,
        project=data.get("project") or DEFAULT_PROJECT,
    )


def complete_mapping_result(
        data: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """Agent Host completes a runner control request after execution."""
    result = data.get("result")
    snapshot = data.get("snapshot")
    if result is None:
        result = {}
    if snapshot is None:
        snapshot = {}
    if not isinstance(result, dict) or not isinstance(snapshot, dict):
        return {
            "error": "result and snapshot must be JSON objects",
            "error_code": "invalid_runner_control_completion",
        }
    return runner_repo.complete_runner_control_request(
        (data.get("request_id") or data.get("id") or "").strip(),
        result=result,
        snapshot=snapshot,
        status=data.get("status") or "",
        host_id=(data.get("host_id") or "").strip(),
        actor=actor,
        project=data.get("project") or DEFAULT_PROJECT,
    )


def list_sessions(
        *,
        host_id: str = "",
        runtime: str = "",
        task_id: str = "",
        status: str = "",
        include_stale: bool = False,
        project: Optional[str] = None) -> list[dict[str, Any]]:
    return runner_repo.list_runner_sessions(
        host_id=host_id, runtime=runtime, task_id=task_id, status=status,
        include_stale=include_stale,
        project=project or DEFAULT_PROJECT,
    )


def resolve_watch(
        *,
        task_id: str,
        include_stale: bool = False,
        project: Optional[str] = None) -> dict[str, Any]:
    """Fail-closed Watch/Chat gate (COORD-34 / UI-17)."""
    return runner_repo.resolve_runner_watch(
        task_id, include_stale=include_stale,
        project=project or DEFAULT_PROJECT)


def list_control_requests(
        *,
        status: str = "",
        host_id: str = "",
        runner_session_id: str = "",
        project: Optional[str] = None) -> list[dict[str, Any]]:
    return runner_repo.list_runner_control_requests(
        status=status, host_id=host_id, runner_session_id=runner_session_id,
        project=project or DEFAULT_PROJECT,
    )
