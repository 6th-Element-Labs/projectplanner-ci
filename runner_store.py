"""Backward-compatible shim — prefer ``switchboard.storage.repositories.runner``."""
import scripts.switchboard_path  # noqa: F401 — make src/switchboard importable

from switchboard.storage.repositories.runner import (  # noqa: E402
    RUNNER_CONTROL_ACTIONS,
    _runner_control_row,
    _runner_session_row,
    _upsert_runner_session_in,
    claim_runner_control_request,
    complete_runner_control_request,
    get_runner_session,
    list_runner_control_requests,
    list_runner_sessions,
    task_live_executions,
    task_has_live_execution,
    blocking_execution_for,
    request_runner_control,
    resolve_runner_watch,
    upsert_runner_session,
)

__all__ = [
    "RUNNER_CONTROL_ACTIONS",
    "upsert_runner_session",
    "list_runner_sessions",
    "task_live_executions",
    "task_has_live_execution",
    "blocking_execution_for",
    "resolve_runner_watch",
    "get_runner_session",
    "request_runner_control",
    "list_runner_control_requests",
    "claim_runner_control_request",
    "complete_runner_control_request",
    "_runner_session_row",
    "_upsert_runner_session_in",
    "_runner_control_row",
]
