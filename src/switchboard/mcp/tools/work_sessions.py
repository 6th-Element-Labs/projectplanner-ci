"""Work Session / preflight MCP tools.

Transport adapter finished in ARCH-MS-66. Authentication and MCP serialization
remain edge concerns; mutating create/update/preflight/archive paths call
application commands. Persistence stays behind store /
repositories/work_sessions.py. ``repo_preflight`` stays a thin store wrapper;
``pre_tool_check`` goes through the ARCH-MS-60 application command.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth
import store
from switchboard.application.commands import pre_tool_check as pre_tool_check_command
from switchboard.application.commands import work_sessions as work_session_commands


@dataclass(frozen=True)
class WorkSessionToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: WorkSessionToolServices | None = None


def _services() -> WorkSessionToolServices:
    if _SERVICES is None:
        raise RuntimeError("work session MCP tools must be registered before use")
    return _SERVICES


def create_work_session(work_session_json: str, ctx: Context,
                        project: str = "maxwell") -> str:
    """Create a first-class Work Session for code work.

    The session binds agent/task work to project, repo_role, branch, worktree/clone path,
    hygiene state, and lease/env evidence. Enforcement into claim_task and complete_claim
    lands in follow-on SESSION tasks.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        payload = json.loads(work_session_json or "{}")
    except json.JSONDecodeError:
        return services.dumps({"error": "work_session_json must be valid JSON"})
    return services.dumps(work_session_commands.create(
        payload, actor=auth.actor(principal),
        principal_id=principal.get("id") or "", project=project))


def create_managed_work_session(managed_session_json: str, ctx: Context,
                                project: str = "maxwell") -> str:
    """Create an isolated git worktree/clone and persist it as a Work Session.

    The payload should include task_id, agent_id, source_path or repo_path, optional
    workspace_root/workspace_path, storage_mode=worktree|clone, and policy_profile.
    The tool allocates branch/path/base/env namespace/session token from repo topology,
    runs git, preflights the workspace, claims a worktree lease, and returns a ready
    Work Session. It fails closed for disallowed modes, existing paths, wrong repos,
    and git/preflight failures.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        payload = json.loads(managed_session_json or "{}")
    except json.JSONDecodeError:
        return services.dumps({"error": "managed_session_json must be valid JSON"})
    return services.dumps(work_session_commands.create_managed(
        payload, actor=auth.actor(principal),
        principal_id=principal.get("id") or "", project=project))


def get_work_session(work_session_id: str, project: str = "maxwell") -> str:
    """Read one Work Session by id."""
    services = _services()
    session = store.get_work_session(work_session_id, project=project)
    return services.dumps(session or {"error": "work_session_not_found",
                             "work_session_id": work_session_id})


def get_work_session_health(work_session_id: str, project: str = "maxwell") -> str:
    """Read the computed health verdict for one Work Session."""
    services = _services()
    health = store.get_work_session_health(work_session_id, project=project)
    return services.dumps(health or {"error": "work_session_not_found",
                            "work_session_id": work_session_id})


def list_work_sessions(project: str = "maxwell", task_id: str = "",
                       agent_id: str = "", status: str = "",
                       repo_role: str = "", include_expired: bool = True) -> str:
    """List Work Sessions for a project, task, agent, repo role, or lifecycle status."""
    services = _services()
    return services.dumps({
        "project": project,
        "contract": store.work_session_contract(project) if store.has_project(project) else None,
        "work_sessions": store.list_work_sessions(
            project=project, task_id=task_id, agent_id=agent_id, status=status,
            repo_role=repo_role, include_expired=include_expired),
    })


def list_session_health(project: str = "maxwell", task_id: str = "",
                        agent_id: str = "", status: str = "",
                        only_unsafe: bool = False) -> str:
    """List computed Work Session health rows and the task-level session_health aggregate."""
    services = _services()
    return services.dumps(store.list_session_health(
        project=project, task_id=task_id, agent_id=agent_id,
        status=status, only_unsafe=only_unsafe))


def update_work_session(work_session_id: str, updates_json: str, ctx: Context,
                        project: str = "maxwell") -> str:
    """Update Work Session state, hygiene, leases, SHAs, branch, or lifecycle status."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        payload = json.loads(updates_json or "{}")
    except json.JSONDecodeError:
        return services.dumps({"error": "updates_json must be valid JSON"})
    return services.dumps(work_session_commands.update(
        work_session_id, payload, actor=auth.actor(principal), project=project))


def archive_work_session_workspace(work_session_id: str, ctx: Context,
                                   project: str = "maxwell",
                                   remove_workspace: bool = False) -> str:
    """Archive a managed Work Session and optionally remove its owned workspace path."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(work_session_commands.archive(
        work_session_id,
        remove_workspace=remove_workspace,
        actor=auth.actor(principal),
        project=project))


def repo_preflight(worktree_path: str, ctx: Context, project: str = "maxwell",
                   task_id: str = "", agent_id: str = "", repo_role: str = "canonical",
                   expected_branch: str = "", expected_base_ref: str = "",
                   scan_conflicts: bool = True) -> str:
    """Run a side-effect-free git/worktree preflight before edit/claim/complete/merge.

    Returns pass/warn/deny with typed failure classes such as dirty_worktree,
    conflict_markers, wrong_repo, wrong_branch, stale_base, and
    shared_worktree_collision.
    """
    services = _services()
    services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(store.repo_preflight(
        worktree_path=worktree_path, project=project, task_id=task_id,
        agent_id=agent_id, repo_role=repo_role, expected_branch=expected_branch,
        expected_base_ref=expected_base_ref, scan_conflicts=scan_conflicts))


def preflight_work_session(work_session_id: str, ctx: Context, project: str = "maxwell",
                           expected_branch: str = "", expected_base_ref: str = "") -> str:
    """Run repo_preflight for a Work Session path and write the result into hygiene."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(work_session_commands.preflight(
        work_session_id, actor=auth.actor(principal), project=project,
        expected_branch=expected_branch, expected_base_ref=expected_base_ref))


def pre_tool_check(ctx: Context, tool_name: str = "", tool_input_json: str = "",
                   action: str = "", task_id: str = "", agent_id: str = "",
                   work_session_id: str = "", claim_id: str = "",
                   control_mode: str = "", project: str = "maxwell") -> str:
    """Validate a pending side-effectful tool before an adapter executes it.

    Returns allow/warn/deny plus remediation. Hook-capable adapters should deny locally on
    `decision=deny`; advisory adapters should surface the reason and advertise reduced
    control fidelity. Denied unsafe attempts are audited as unbound_write or unsafe_session.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        tool_input = json.loads(tool_input_json or "{}")
    except json.JSONDecodeError:
        return services.dumps({"error": "tool_input_json must be valid JSON"})
    return services.dumps(pre_tool_check_command.execute_mapping_result(
        {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "action": action,
            "task_id": task_id,
            "agent_id": agent_id,
            "work_session_id": work_session_id,
            "claim_id": claim_id,
            "control_mode": control_mode,
        },
        actor=auth.actor(principal),
        principal_id=principal.get("id") or "",
        project=project,
    ))


def get_preflight_calibration(code: str = "", since: float = 0.0,
                              min_outcomes: int = 3,
                              project: str = "maxwell") -> str:
    """SESSION-15: join preflight predictions to merge/CI/merge-gate outcomes per finding code."""
    from switchboard.application.queries import preflight_calibration as calibration_q
    services = _services()
    return services.dumps(calibration_q.calibration(
        project=project, code=code, since=since, min_outcomes=min_outcomes))


def list_preflight_runs(task_id: str = "", head_sha: str = "",
                        work_session_id: str = "", limit: int = 50,
                        project: str = "maxwell") -> str:
    """SESSION-15: list durable preflight prediction runs."""
    from switchboard.application.queries import preflight_calibration as calibration_q
    services = _services()
    rows = calibration_q.list_runs(
        project=project, task_id=task_id, head_sha=head_sha,
        work_session_id=work_session_id, limit=limit)
    return services.dumps({"run_count": len(rows), "runs": rows})


def get_preflight_run(run_id: str, project: str = "maxwell") -> str:
    """SESSION-15: fetch one durable preflight run with findings."""
    from switchboard.application.queries import preflight_calibration as calibration_q
    services = _services()
    row = calibration_q.get_run(run_id, project=project)
    if not row:
        return services.dumps({"error": "preflight_run_not_found", "run_id": run_id})
    return services.dumps(row)


WORK_SESSION_TOOL_NAMES = (
    'create_work_session', 'create_managed_work_session', 'get_work_session',
    'get_work_session_health', 'list_work_sessions', 'list_session_health',
    'update_work_session', 'archive_work_session_workspace', 'repo_preflight',
    'preflight_work_session', 'pre_tool_check',
    'get_preflight_calibration', 'list_preflight_runs', 'get_preflight_run',
)


def register_work_session_tools(mcp: Any, services: WorkSessionToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in WORK_SESSION_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
