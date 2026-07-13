"""Task-focused MCP tools.

This is the transport adapter for the task application slice.  Authentication,
identity binding, and MCP serialization remain edge concerns; create/update/get
delegate to the shared application commands and query used by REST.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import agent
import auth
import store
from switchboard.application.commands import create_task as create_task_command
from switchboard.application.commands import move_task as move_task_command
from switchboard.application.commands import update_task as update_task_command
from switchboard.application.queries import get_task as get_task_query


@dataclass(frozen=True)
class TaskToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]
    resolve_write_actor: Callable[..., dict[str, Any]]
    write_binding_comment: Callable[..., None]


_SERVICES: TaskToolServices | None = None


def _services() -> TaskToolServices:
    if _SERVICES is None:
        raise RuntimeError("task MCP tools must be registered before use")
    return _SERVICES


def dep_ids(value: str) -> list[str]:
    """Parse comma/whitespace-separated task ids, preserving first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for token in (value or "").replace("\n", ",").replace(" ", ",").split(","):
        task_id = token.strip().upper()
        if task_id and task_id not in seen:
            seen.add(task_id)
            out.append(task_id)
    return out


def unknown_ids(task_ids: list[str], project: str) -> list[str]:
    """Return dependency ids that do not resolve on the selected project."""
    return [task_id for task_id in task_ids
            if not store.get_task(task_id, project=project)]


def search_tasks(workstream: str = "", status: str = "", owner_person: str = "",
                 blocking: bool = False, query: str = "", project: str = "maxwell") -> str:
    """Filter a plan's tasks. project selects the board ('maxwell' default, 'helm', or
    'switchboard'). All other args optional: workstream id (SSO/SEN/... for Maxwell;
    ENGINE/CHART/... for Helm; PROTO/ADAPTER/ENFORCE/... for Switchboard), status
    (Not Started|In Progress|In Review|Blocked|Done), owner_person substring, blocking, free-text query.
    Returns a JSON list of {task_id,title,status,owner_person_or_role,workstream,...}."""
    return _services().dumps(agent._search_tasks({
        "workstream": workstream, "status": status, "owner_person": owner_person,
        "blocking": blocking, "query": query}, project=project))


def get_task(task_id: str, project: str = "maxwell") -> str:
    """Full detail of one task: description, all fields, dependencies, and recent activity.
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    task = get_task_query.execute_for(task_id, project=project)
    return _services().dumps(agent._task_brief(task, full=True)) if task else "no such task"


def update_task(task_id: str, ctx: Context, title: str = "", description: str = "",
                status: str = "", owner_org: str = "", owner_person_or_role: str = "",
                assignee: str = "", phase: str = "", start_date: str = "",
                finish_date: str = "", risk_level: str = "", is_blocking: str = "",
                depends_on: str = "", project: str = "maxwell", agent_id: str = "",
                system_actor: str = "", system_reason: str = "") -> str:
    """Update only the fields you pass on a task. status: Not Started|In Progress|In Review|Blocked|Done;
    Done fails closed unless merge/default-branch provenance is already recorded for the task;
    dates: YYYY-MM-DD; is_blocking: 'true'/'false'. depends_on: comma/space-separated task ids that
    REPLACE this task's dependency list (e.g. 'TOOLS-7, SHELL-1'); pass 'none' to clear it (for an
    incremental edge use add_dependency/remove_dependency). Audited as the authenticated actor.
    project selects the board ('maxwell' default, 'helm', or 'switchboard') — writes go ONLY to that board."""
    services = _services()
    principal = services.require_write(ctx, project)
    binding = services.resolve_write_actor(
        principal, project=project, task_id=task_id, agent_id=agent_id,
        system_actor=system_actor, system_reason=system_reason)
    if not binding.get("ok"):
        return services.dumps(binding)
    data = {}
    for key, value in (("title", title), ("description", description), ("status", status),
                       ("owner_org", owner_org), ("owner_person_or_role", owner_person_or_role),
                       ("assignee", assignee), ("phase", phase), ("start_date", start_date),
                       ("finish_date", finish_date), ("risk_level", risk_level),
                       ("is_blocking", is_blocking), ("depends_on", depends_on)):
        if value != "":
            data[key] = value
    if not data:
        return "no fields to update"
    task = update_task_command.execute_mapping_result(
        task_id, data, actor=binding["actor"], project=project)
    if isinstance(task, dict) and task.get("error_code"):
        return services.dumps(task)
    services.write_binding_comment(task_id, binding, project)
    return services.dumps(agent._task_brief(task)) if task else "no such task"


def create_task(workstream_id: str, title: str, ctx: Context, description: str = "",
                owner_org: str = "", owner_person_or_role: str = "", status: str = "",
                phase: str = "", risk_level: str = "", depends_on: str = "",
                project: str = "maxwell", agent_id: str = "", system_actor: str = "",
                system_reason: str = "") -> str:
    """Create a task in a workstream (SSO/SEN/... for Maxwell; ENGINE/CHART/... for Helm;
    PROTO/ADAPTER/ENFORCE/... for Switchboard). depends_on:
    comma/space-separated task ids this task dependsOn (e.g. 'BOAT-1, WX-10'). Returns the created task.
    Actor 'MCP'. project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    services = _services()
    principal = services.require_write(ctx, project)
    binding = services.resolve_write_actor(
        principal, project=project, agent_id=agent_id,
        system_actor=system_actor, system_reason=system_reason)
    if not binding.get("ok"):
        return services.dumps(binding)
    task = create_task_command.execute_mapping_result(
        locals(), actor=binding["actor"], project=project)
    if task.get("error"):
        return services.dumps(task)
    if task:
        services.write_binding_comment(task.get("task_id") or "", binding, project)
    return services.dumps(agent._task_brief(task))


def add_comment(task_id: str, text: str, ctx: Context, project: str = "maxwell",
                agent_id: str = "", system_actor: str = "",
                system_reason: str = "") -> str:
    """Add a note to a task's activity log (audited as actor 'MCP').
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    services = _services()
    principal = services.require_write(ctx, project)
    binding = services.resolve_write_actor(
        principal, project=project, task_id=task_id, agent_id=agent_id,
        system_actor=system_actor, system_reason=system_reason)
    if not binding.get("ok"):
        return services.dumps(binding)
    services.write_binding_comment(task_id, binding, project)
    task = store.add_comment(
        task_id, binding["actor"], text, project=project, hydrate_task=False)
    return "ok" if task else "no such task"


def archive_task(task_id: str, ctx: Context, project: str = "maxwell",
                 reason: str = "") -> str:
    """Archive a task instead of raw-deleting it.

    Requires the system write scope because this removes the active task row. The archived
    snapshot preserves task fields, activity, git/provenance, Tally rows, claims/leases, and
    related decision records where possible. Fails if the task has active claims or leases.
    project selects the board ('maxwell' default, 'helm', 'switchboard', or dynamic projects).
    """
    services = _services()
    principal = services.require_write(ctx, "switchboard", ("write:system",))
    return services.dumps(store.archive_task(
        task_id, reason=reason, actor=auth.actor(principal), project=project))


def move_task(task_id: str, project_from: str, project_to: str, ctx: Context,
              reason: str = "", new_task_id: str = "",
              dependency_policy: str = "fail") -> str:
    """Move one task between isolated project boards with an audit trail.

    This is for cleanup of project-boundary mistakes. It fails closed on unknown projects,
    refuses active claims/leases, and refuses destination task-id conflicts. By default it
    also refuses dangling dependencies in the destination; pass dependency_policy='clear'
    only when intentionally cleaning up leaked tasks and the missing dependency edges should
    be removed during the move.
    """
    services = _services()
    principal = services.require_write(ctx, "switchboard", ("write:system",))
    return services.dumps(move_task_command.execute_mapping_result(
        task_id,
        {
            "project_from": project_from,
            "project_to": project_to,
            "reason": reason,
            "new_task_id": new_task_id,
            "dependency_policy": dependency_policy,
        },
        actor=auth.actor(principal),
    ))


def add_dependency(task_id: str, depends_on: str, ctx: Context,
                   project: str = "maxwell") -> str:
    """Add one or more dependency EDGES to a task (task_id dependsOn each id in depends_on,
    comma/space-separated, e.g. 'TOOLS-7, SHELL-1'). APPENDS without clobbering existing deps
    (idempotent, deduped) — use this to wire cross-epic edges. FAIL-FAST: if ANY id is not a real
    task the whole call is REJECTED with an error and nothing is written (a dependency to a
    non-existent task is a broken graph edge) — fix the id or create the target first, then retry.
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    services = _services()
    principal = services.require_write(ctx, project)
    additions = dep_ids(depends_on)
    if not additions:
        return "no dependency ids given"
    task = store.get_task(task_id, project=project)
    if not task:
        return "no such task: " + task_id
    unknown = unknown_ids(additions, project)
    if unknown:
        return services.dumps({
            "error": "unknown task id(s) on project '%s': %s — NO edge added. "
                     "Create the target task(s) first or fix the id."
                     % (project, ", ".join(unknown))})
    merged = list(task.get("depends_on") or [])
    for dependency in additions:
        if dependency not in merged:
            merged.append(dependency)
    store.update_task(task_id, {"depends_on": merged},
                      actor=auth.actor(principal), project=project)
    return services.dumps({"task_id": task_id, "depends_on": merged})


def remove_dependency(task_id: str, depends_on: str, ctx: Context,
                      project: str = "maxwell") -> str:
    """Remove one or more dependency edges from a task (comma/space-separated ids). Reports which ids
    were actually removed vs not present — a no-op removal is SURFACED, not silently swallowed.
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    services = _services()
    principal = services.require_write(ctx, project)
    removals = dep_ids(depends_on)
    if not removals:
        return "no dependency ids given"
    task = store.get_task(task_id, project=project)
    if not task:
        return "no such task: " + task_id
    current = list(task.get("depends_on") or [])
    removal_set = set(removals)
    merged = [dependency for dependency in current if dependency not in removal_set]
    store.update_task(task_id, {"depends_on": merged},
                      actor=auth.actor(principal), project=project)
    result = {"task_id": task_id, "depends_on": merged,
              "removed": [dependency for dependency in current if dependency in removal_set]}
    not_present = [dependency for dependency in removals if dependency not in current]
    if not_present:
        result["note"] = "not present (nothing to remove): " + ", ".join(not_present)
    return services.dumps(result)


TASK_TOOL_NAMES = (
    "search_tasks", "get_task", "update_task", "create_task", "add_comment",
    "archive_task", "move_task", "add_dependency", "remove_dependency",
)


def register_task_tools(mcp: Any, services: TaskToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the task tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in TASK_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
