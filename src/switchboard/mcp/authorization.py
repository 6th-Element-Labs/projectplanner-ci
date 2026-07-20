"""Central project authorization for every registered MCP tool (SEG-3).

The HTTP middleware authenticates a bearer once.  This module performs the
separate application-level decision: may that principal use this tool against
the requested project?  A frozen :class:`ProjectContext` is installed for the
duration of the tool call so command adapters can reuse the same effective
grants without reopening the principal registry.

Tool access is declared in one fail-closed census below.  Adding an MCP tool
without classifying it as a read or write, or removing a declared tool without
updating the census, prevents the MCP composition root from starting.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
import functools
import inspect
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Optional

import auth
from constants import DEFAULT_PROJECT
from switchboard.domain.projects.context import ProjectContext, ProjectGrant


class AccessClass(str, Enum):
    READ = "read"
    WRITE = "write"
    DISCOVERY = "discovery"


@dataclass(frozen=True)
class ToolAccessDeclaration:
    tool_name: str
    access_class: AccessClass
    project_argument: str
    fixed_authorization_project: str = ""


class ProjectContextUnavailable(RuntimeError):
    """Raised only when a direct/local caller has no dispatcher context."""


_transport_principal: ContextVar[Optional[Mapping[str, Any]]] = ContextVar(
    "switchboard_mcp_transport_principal", default=None)
_current_context: ContextVar[Optional[ProjectContext]] = ContextVar(
    "switchboard_mcp_project_context", default=None)
_request_cache: ContextVar[Optional[dict[tuple[Any, ...], ProjectContext]]] = ContextVar(
    "switchboard_mcp_authorization_cache", default=None)


@contextmanager
def transport_principal_scope(principal: Mapping[str, Any]) -> Iterator[None]:
    """Bind one authenticated transport principal and an empty request cache."""
    principal_token = _transport_principal.set(MappingProxyType(dict(principal)))
    cache_token = _request_cache.set({})
    context_token = _current_context.set(None)
    try:
        yield
    finally:
        _current_context.reset(context_token)
        _request_cache.reset(cache_token)
        _transport_principal.reset(principal_token)


def current_project_context() -> Optional[ProjectContext]:
    return _current_context.get()


def _principal_for_dispatch(project: str) -> Mapping[str, Any]:
    principal = _transport_principal.get()
    if principal is not None:
        return principal
    if auth.auth_mode() == auth.DEV_OPEN:
        return auth.authenticate(project or DEFAULT_PROJECT, "", (), dev_actor="MCP")
    raise PermissionError("unauthorized: authenticated MCP principal context is missing")


def _has_any_write_scope(scopes: tuple[str, ...]) -> bool:
    return "admin" in scopes or any(scope.startswith("write:") for scope in scopes)


def authorize_project_context(
        principal: Mapping[str, Any], project: str, access_class: AccessClass,
        *, requested_project: str = "", discovery: bool = False) -> ProjectContext:
    """Authorize once, then reuse the frozen result for the request."""
    selected = str(project or "").strip() or DEFAULT_PROJECT
    cache = _request_cache.get()
    cache_key = (
        str(principal.get("id") or ""), selected, access_class.value,
        str(requested_project or ""), bool(discovery),
    )
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    required = ("read",) if access_class in {AccessClass.READ, AccessClass.DISCOVERY} else ()
    authorized = auth.authorize_principal(principal, selected, required)
    scopes = tuple(sorted(str(scope) for scope in (authorized.get("effective_scopes") or [])))
    if access_class == AccessClass.WRITE and not _has_any_write_scope(scopes):
        raise PermissionError("forbidden: token is missing required write scope")
    grants = tuple(ProjectGrant.from_mapping(grant)
                   for grant in (authorized.get("project_roles") or []))
    visible = ()
    if discovery:
        visible = tuple(auth.accessible_project_ids_for_principal(principal))
    context = ProjectContext(
        project_id=selected,
        source="mcp",
        principal_id=str(authorized.get("id") or ""),
        label="",
        requested_project=str(requested_project or selected),
        principal_kind=str(authorized.get("kind") or ""),
        principal_binding=str(authorized.get("project") or ""),
        principal_display_name=str(
            authorized.get("display_name") or authorized.get("id") or "unknown"),
        access_class=access_class.value,
        effective_scopes=scopes,
        grants=grants,
        authorized_projects=visible,
        environment_operator=bool(authorized.get("environment_operator")),
        dev_open=bool(authorized.get("dev_open")),
    )
    if cache is not None:
        cache[cache_key] = context
    return context


def require_current_access(project: str, required_scopes: tuple[str, ...]) -> dict[str, Any]:
    """Reuse dispatcher authorization for legacy require_read/require_write calls."""
    principal = _transport_principal.get()
    current = _current_context.get()
    if principal is None and current is None:
        raise ProjectContextUnavailable("no MCP dispatcher ProjectContext is active")
    selected = str(project or DEFAULT_PROJECT).strip()
    if current is None or current.project != selected:
        if principal is None:
            raise ProjectContextUnavailable("no MCP transport principal is active")
        current = authorize_project_context(principal, selected, AccessClass.READ)
    scopes = set(current.effective_scopes)
    if "admin" not in scopes and not set(required_scopes).issubset(scopes):
        raise PermissionError("forbidden: token is missing required scope")
    return current.as_principal()


def filter_authorized_projects(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter project discovery using the request's already-resolved principal."""
    context = _current_context.get()
    if context is None:
        # Direct Python/dev callers retain the historical local-test behavior.
        return list(projects)
    allowed = set(context.authorized_projects or (context.project,))
    return [project for project in projects if project.get("id") in allowed]


# Explicit registration census.  These names are the public MCP surface; no
# name may silently inherit an access class.
READ_TOOLS = frozenset({
    "ask_plan", "board_summary", "check_files", "check_resource",
    "control_plane_probe", "doc_search", "get_agent_state",
    "get_background_job_run", "get_decision", "get_deliverable",
    "get_deliverable_breakdown_proposal", "get_deliverable_closure_report",
    "get_deliverable_dependency_graph", "get_deliverable_tally",
    "get_execution_transcript", "get_external_ci_run", "get_kpi_tally", "get_lane_delta",
    "get_mcp_observability", "get_message_status", "get_mission_brief",
    "get_mission_status", "get_narration_health", "get_plan_signals",
    "get_preflight_calibration", "get_preflight_run",
    "get_project", "get_project_board", "get_project_contract",
    "get_project_impact_report", "get_provider_connection",
    "get_review_remediation_metrics", "get_review_verdict",
    "get_saturation_signals", "get_task", "get_task_execution", "get_task_session",
    "task_session_doctor", "get_task_tally", "get_work_session",
    "get_work_session_health", "get_working_agreement", "host_status",
    "list_active_agents", "list_active_leases", "list_active_resource_leases",
    "list_agent_hosts", "list_background_job_runs", "list_coordinator_decisions",
    "list_decisions", "list_deliverable_breakdown_proposals", "list_deliverables",
    "list_external_ci_runs", "list_external_effects", "list_monitors",
    "list_pending_acks", "list_project_boards", "list_projects",
    "list_preflight_runs",
    "list_provider_auth_capabilities", "list_provider_connections",
    "list_publication_evidence",
    "list_review_findings", "list_review_remediations",
    "list_runner_control_requests", "list_runner_sessions", "list_session_health",
    "list_unacked_messages", "list_unblock_requests", "list_wake_intents",
    "list_work_sessions", "mission_status", "prepare_agent_session",
    "resolve_runner_watch", "search_tasks",
})

WRITE_TOOLS = frozenset({
    "abandon_claim", "ack_message", "acquire_provider_credential_lease",
    "add_comment", "add_deliverable_milestone", "add_dependency", "apply_cleanup",
    "apply_project_consolidation", "approve_deliverable_breakdown", "archive_project",
    "archive_task", "archive_work_session_workspace", "cancel_monitor", "cancel_wake",
    "begin_agent_host_enrollment", "bind_host_native_provider_connection",
    "claim_external_effect", "claim_files", "claim_next", "claim_resource",
    "claim_runner_control", "claim_task", "claim_wake", "complete_claim",
    "complete_runner_control", "complete_wake", "create_board", "create_deliverable",
    "create_kpi", "create_managed_work_session", "create_mission", "create_project",
    "create_project_board", "create_project_purge_intent", "create_scoped_token",
    "create_task", "create_work_session", "defer_deliverable_breakdown",
    "delete_provider_connection", "dispatch_to_claude_code", "dispatch_to_co_fleet",
    "dispatch_to_codex_cloud", "enroll_provider_connection", "execute_project_purge",
    "fail_external_effect", "generate_digest", "generate_mission_brief",
    "get_audit_export", "heartbeat", "heartbeat_host", "ingest_and_triage",
    "link_outcome_to_kpi", "link_task_to_deliverable", "link_tasks_to_deliverable",
    "list_agent_host_enrollments", "open_session",
    "list_cleanup_candidates", "list_scoped_tokens", "mark_external_effect_issued",
    "merge_gate", "move_task", "narrate_now", "notify", "plan_project_consolidation",
    "poll_external_ci_mirror_run", "pre_tool_check", "preflight_work_session",
    "propose_deliverable_breakdown", "reactivate_narration", "reconcile",
    "reconcile_alerts",
    "record_coordinator_decision", "record_decision", "record_outcome",
    "record_project_cleanup_review", "record_publication_evidence",
    "record_review_verdict", "register_agent", "register_host",
    "register_runner_session", "reject_deliverable_breakdown", "reject_outcome",
    "release_files", "release_provider_credential_lease", "release_resource",
    "remove_dependency", "repo_preflight", "report_usage",
    "request_deliverable_closure_verification", "request_external_ci_mirror_run",
    "request_runner_health", "request_runner_inject", "request_runner_kill",
    "mint_runner_pty_ticket",
    "request_runner_logs", "request_runner_open", "request_runner_snapshot",
    "request_unblock", "request_wake", "resolve_monitor", "resolve_review_finding",
    "restore_project", "retry_task", "revoke_claim", "revoke_provider_connection",
    "revoke_scoped_token", "rollback_project_consolidation",
    "rotate_provider_connection", "run_background_job", "run_mission_coordinator",
    "send_agent_message", "send_message", "set_agent_state", "set_project_github_repo",
    "set_project_repo_topology", "start_task", "stop_task", "submit_bug",
    "submit_deliverable_outcome",
    "sweep_monitors", "unlink_task_from_deliverable",
    "update_deliverable_breakdown_proposal", "update_kpi_value",
    "update_deliverable", "update_mission_narrative", "update_project", "update_task",
    "update_work_session",
    "update_agent_host_execution_policy",
    "verify_deliverable_closure", "verify_external_effect", "verify_offline_completion",
    "verify_outcome", "verify_project_consolidation", "verify_project_purge_intent",
    "verify_provider_connection",
})

if READ_TOOLS & WRITE_TOOLS:
    raise RuntimeError("MCP authorization census contains conflicting access declarations")

SPECIAL_PROJECT_ARGUMENTS = {
    "create_project": "project_id",
    "move_task": "project_from",
}
FIXED_AUTHORIZATION_PROJECTS = {
    "create_project": "switchboard",
    "move_task": "switchboard",
}
DISCOVERY_TOOLS = frozenset({"list_projects"})
WORK_SESSION_BOOT_TOOLS = frozenset({
    "prepare_agent_session", "get_working_agreement", "get_project_contract", "get_task",
})
DIRECT_SESSION_WRITE_TOOLS = frozenset({
    "ack_message", "add_comment", "check_files", "claim_files", "claim_resource",
    "claim_task", "complete_claim", "create_work_session", "heartbeat",
    "merge_gate", "pre_tool_check", "preflight_work_session", "reconcile",
    "record_review_verdict", "register_agent", "release_files", "release_resource",
    "request_unblock", "send_agent_message", "set_agent_state", "update_task",
    "update_work_session",
})


def declaration_for(tool_name: str) -> ToolAccessDeclaration:
    if tool_name in DISCOVERY_TOOLS:
        access_class = AccessClass.DISCOVERY
    elif tool_name in READ_TOOLS:
        access_class = AccessClass.READ
    elif tool_name in WRITE_TOOLS:
        access_class = AccessClass.WRITE
    else:
        raise RuntimeError(f"unguarded MCP tool registration: {tool_name}")
    return ToolAccessDeclaration(
        tool_name=tool_name,
        access_class=access_class,
        project_argument=SPECIAL_PROJECT_ARGUMENTS.get(tool_name, "project"),
        fixed_authorization_project=FIXED_AUTHORIZATION_PROJECTS.get(tool_name, ""),
    )


class MCPAuthorizationGuard:
    """Dispatcher wrapper plus startup-time registration census."""

    def __init__(self) -> None:
        self._registered: set[str] = set()

    @property
    def registered_tools(self) -> frozenset[str]:
        return frozenset(self._registered)

    def wrap(self, function: Callable[..., Any]) -> Callable[..., Any]:
        declaration = declaration_for(function.__name__)
        signature = inspect.signature(function)
        if declaration.project_argument not in signature.parameters:
            raise RuntimeError(
                f"MCP tool {function.__name__} must declare project argument "
                f"{declaration.project_argument!r}")
        self._registered.add(function.__name__)

        @functools.wraps(function)
        def authorized(*args, **kwargs):
            bound = signature.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            requested = str(bound.arguments.get(declaration.project_argument) or "").strip()
            principal_hint = _transport_principal.get()
            work_session_principal = (principal_hint or {}).get("kind") == "work_session"
            direct_session_principal = (principal_hint or {}).get("kind") == "direct_session"
            if (work_session_principal
                    and function.__name__ not in WORK_SESSION_BOOT_TOOLS):
                raise ValueError(
                    "forbidden: Work Session token is limited to MCP session boot")
            if work_session_principal and "task_id" in bound.arguments:
                requested_task = str(bound.arguments.get("task_id") or "").strip().upper()
                bound_task = str(
                    (principal_hint or {}).get("bound_task_id") or "").strip().upper()
                if requested_task and requested_task != bound_task:
                    raise ValueError(
                        "forbidden: Work Session token is bound to another task")
            if (direct_session_principal
                    and declaration.access_class == AccessClass.WRITE
                    and function.__name__ not in DIRECT_SESSION_WRITE_TOOLS):
                raise ValueError(
                    "forbidden: direct CLI token cannot perform that write")
            if direct_session_principal and "task_id" in bound.arguments:
                requested_task = str(bound.arguments.get("task_id") or "").strip().upper()
                bound_task = str(
                    (principal_hint or {}).get("bound_task_id") or "").strip().upper()
                if requested_task and requested_task != bound_task:
                    raise ValueError(
                        "forbidden: direct CLI token is bound to another task")
            if direct_session_principal and "agent_id" in bound.arguments:
                requested_agent = str(bound.arguments.get("agent_id") or "").strip()
                bound_agent = str(
                    (principal_hint or {}).get("bound_agent_id") or "").strip()
                if requested_agent and bound_agent and requested_agent != bound_agent:
                    raise ValueError(
                        "forbidden: direct CLI token is bound to another agent")
            binding = str((principal_hint or {}).get("project") or "").strip()
            selected = requested
            if declaration.fixed_authorization_project:
                selected = declaration.fixed_authorization_project
            elif requested == "*" and function.__name__ == "create_scoped_token":
                selected = "switchboard"
            elif not selected:
                if (function.__name__ == "prepare_agent_session" and
                        bool((principal_hint or {}).get("environment_operator"))):
                    # The explicitly configured deployment operator retains the
                    # cross-board boot resolver. Customer/global grant principals
                    # must select one of their granted projects explicitly.
                    selected = "switchboard"
                elif function.__name__ == "list_projects" and binding == "*":
                    visible = auth.accessible_project_ids_for_principal(
                        dict(principal_hint or {}))
                    selected = visible[0] if visible else DEFAULT_PROJECT
                    bound.arguments[declaration.project_argument] = selected
                else:
                    selected = binding if binding and binding != "*" else DEFAULT_PROJECT
                    bound.arguments[declaration.project_argument] = selected
            try:
                principal = _principal_for_dispatch(selected)
                context = authorize_project_context(
                    principal,
                    selected,
                    declaration.access_class,
                    requested_project=requested or selected,
                    discovery=declaration.access_class == AccessClass.DISCOVERY,
                )
            except PermissionError as exc:
                raise ValueError(str(exc)) from exc
            token = _current_context.set(context)
            try:
                return function(*bound.args, **bound.kwargs)
            except PermissionError as exc:
                raise ValueError(str(exc)) from exc
            finally:
                _current_context.reset(token)

        return authorized

    def assert_complete(self) -> None:
        declared = READ_TOOLS | WRITE_TOOLS
        missing = sorted(declared - self._registered)
        extra = sorted(self._registered - declared)
        if missing or extra:
            raise RuntimeError(
                "MCP authorization census mismatch: "
                f"unregistered_declarations={missing}, undeclared_registrations={extra}")
