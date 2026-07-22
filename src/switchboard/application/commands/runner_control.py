"""Shared runner session / control commands for REST and MCP (ARCH-MS-67).

Adapters own auth and transport serialization. Persistence lives in
``repositories/runner``.
"""
from __future__ import annotations

from typing import Any, Optional

from constants import DEFAULT_PROJECT
from switchboard.application.queries import task_session
from switchboard.storage.repositories import runner as runner_repo


def upsert_session_mapping_result(
        data: dict[str, Any], *, actor: str, principal_id: str = "") -> dict[str, Any]:
    """Register or heartbeat one supervised runner session."""
    record = dict(data or {})
    project = record.pop("project", None) or DEFAULT_PROJECT
    result = runner_repo.upsert_runner_session(
        record, principal_id=principal_id, actor=actor, project=project)
    # SIMPLIFY-9: every authenticated registration/heartbeat renews the
    # executor's short-lived outbound relay ticket. It is response-only and is
    # never persisted with the runner row.
    if not result.get("error"):
        session_id = str(
            result.get("runner_session_id")
            or record.get("runner_session_id") or record.get("id") or "")
        session = runner_repo.get_runner_session(session_id, project=project)
        if session:
            # WATCH-4: a run proven live by its attached relay tunnel renews its
            # host_url even without a scheduler claim/Work Session, so a Connect
            # PTY session's tunnel does not go dark at ticket expiry. Resolved
            # in-process (this heartbeat is served by the relay-owning process);
            # None off-process keeps the historical direct-assignment behaviour.
            from switchboard.application import runner_pty_relay as relay
            server_relay = runner_repo._server_relay_options(
                session, user_id=principal_id, project=project,
                host_attached=relay.host_attached_for(session_id))
            result["server_relay"] = server_relay
            if server_relay.get("error"):
                runner_repo.record_server_relay_failure(
                    session, server_relay, actor=actor, project=project)
    return result


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
    projection = task_session.execute_for(task_id, project=project or DEFAULT_PROJECT)
    if projection is None:
        return runner_repo.runner_bind_incomplete(["task_id"], task_id=task_id) | {
            "message": "task not found", "task_session": None,
        }
    # WATCH-5: surface the same four-state panel projection the task card uses.
    from switchboard.application.commands import task_execution as task_execution_cmd
    panel = task_execution_cmd._panel_projection(
        projection, project=project or DEFAULT_PROJECT)
    session = projection.get("active_runner")
    if session:
        # WATCH-4: the live relay attachment state is the primary liveness signal.
        # Resolved in-process here (this command is served by the web/relay process
        # that terminates the host tunnel); None on any process without the session
        # keeps the historical bind-tuple inference.
        from switchboard.application import runner_pty_relay as relay
        host_attached = relay.host_attached_for(session.get("runner_session_id"))
        verdict = runner_repo.assert_runner_watchable(
            session, host_attached=host_attached)
        return {**verdict, "sessions": [session], "enough_for_panel": True,
                "task_session": projection, "panel": panel}
    outcome = projection.get("last_dispatch_outcome") or {}
    attempt_runner = ((projection.get("active_attempt") or {}).get("runner"))
    # Prefer WATCH-5 panel detail over the generic bind-incomplete message when
    # the wake is still queued/starting — that is the operator-facing truth.
    message = (
        panel.get("detail")
        if panel.get("state") in {"queued", "starting"}
        else (outcome.get("message") or "No live runner is registered for this task")
    )
    return runner_repo.runner_bind_incomplete(
        list(runner_repo.RUNNER_BIND_FIELDS), task_id=task_id) | {
        "message": message,
        "sessions": [attempt_runner] if attempt_runner else [],
        "enough_for_panel": False, "task_session": projection,
        "panel": panel,
        **({"dispatch": outcome} if outcome else {}),
    }


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
