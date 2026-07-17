"""Project-native context shared by MCP and the embedded planning agent."""

from __future__ import annotations

from typing import Any, Dict, Optional

import store
from switchboard.domain.validation_policy import project_validation_policy, task_validation


def resolve_project_input(project: str) -> str:
    """Accept either a project id or its display label, case-insensitively."""
    value = (project or "").strip()
    if not value:
        return ""
    lowered = value.lower()
    for item in store.projects():
        if lowered in (item["id"].lower(), (item.get("label") or "").lower()):
            return item["id"]
    return lowered


def project_label(project: str) -> str:
    for item in store.projects():
        if item["id"] == project:
            return item.get("label") or project
    return project


def _task_brief(task: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not task:
        return None
    return {
        "task_id": task.get("task_id"),
        "title": task.get("title"),
        "status": task.get("status"),
        "workstream": task.get("_wsId"),
        "workstream_name": task.get("_wsName"),
        "owner_org": task.get("owner_org"),
        "owner_person_or_role": task.get("owner_person_or_role"),
        "assignee": task.get("assignee"),
        "depends_on": task.get("depends_on") or [],
        "description": task.get("description"),
        "entry_criteria": task.get("entry_criteria"),
        "exit_criteria": task.get("exit_criteria"),
        "deliverable": task.get("deliverable"),
        "risk_level": task.get("risk_level"),
        "is_blocking": task.get("is_blocking"),
        "ui_impact": task.get("ui_impact"),
        "ui_validation": task.get("ui_validation") or {"required": False},
    }


def _dependencies(project: str, task: Optional[Dict[str, Any]]) -> list[dict]:
    out = []
    for dependency_id in (task or {}).get("depends_on") or []:
        dependency = store.get_task(dependency_id, project=project)
        out.append(
            {
                "task_id": dependency_id,
                "exists": bool(dependency),
                "status": dependency.get("status") if dependency else None,
                "title": dependency.get("title") if dependency else None,
                "workstream": dependency.get("_wsId") if dependency else None,
            }
        )
    return out


def build(
    project: str,
    lane: str = "",
    task_id: str = "",
    deliverable_id: str = "",
    board_id: str = "",
    mission_id: str = "",
    milestone_id: str = "",
) -> Dict[str, Any]:
    """Build the canonical project/lane/task contract for any project."""
    selected = resolve_project_input(project) or store.DEFAULT_PROJECT
    if not store.has_project(selected):
        return {
            "ok": False,
            "status": "unknown_project",
            "error": f"project '{project}' is not a routable Switchboard project",
            "projects": store.projects(),
        }
    tid = (task_id or "").strip().upper()
    task = store.get_task(tid, project=selected) if tid else None
    workstream = (lane or "").strip().upper()
    if task and not workstream:
        workstream = task.get("_wsId") or ""
    access = store.project_access(selected)
    repo_topology = store.get_project_repo_topology(selected)
    lane_tasks = (
        store.list_tasks_slim(workstream=workstream, project=selected)
        if workstream
        else []
    )
    lane_name = next(
        (item.get("_wsName") for item in lane_tasks if item.get("_wsName")), None
    )
    try:
        active_agents = (
            [
                {
                    "agent_id": item.get("agent_id"),
                    "runtime": item.get("runtime"),
                    "lane": item.get("lane"),
                    "task_id": item.get("task_id"),
                    "stale": item.get("stale"),
                }
                for item in store.list_active_agents(lane=workstream, project=selected)
            ]
            if workstream
            else []
        )
    except Exception:
        active_agents = []
    deliverable_scope = bool((deliverable_id or board_id or mission_id).strip())
    mission_context = (
        store.get_mission_status(
            project=selected,
            deliverable_id=deliverable_id,
            board_id=board_id,
            mission_id=mission_id,
        )
        if deliverable_scope
        else None
    )
    operating_rules = [
        f'Pass project="{selected}" on every Switchboard MCP call.',
        access.get("boundary")
        or f"Only work belonging to project={selected} belongs here.",
        "Treat Project as the repo, trust, policy, access, CI, model, budget, and Done authority boundary.",
        "Boards and workstreams own execution; deliverables own outcomes and cross-board proof rollup.",
        "Treat repo_topology.roles.canonical as the only code-truth and Done authority.",
        "Read task description, deliverable, exit criteria, dependencies, and recent activity before editing.",
        "Do not import another project's lane or file ownership into this project.",
    ]
    recommended_reads = [
        item
        for item in [
            f'get_task(task_id="{tid}", project="{selected}")' if tid else None,
            f'search_tasks(workstream="{workstream}", project="{selected}")'
            if workstream
            else None,
        ]
        if item
    ]
    return {
        "ok": True,
        "source_of_truth": "switchboard_project_contract",
        "project": selected,
        "project_label": project_label(selected),
        "project_access": access,
        "project_hierarchy": repo_topology.get("project_hierarchy"),
        "boards_missions": store.list_project_boards(project=selected),
        "repo_topology": repo_topology,
        "repo_role_guide": store.repo_topology_role_guide(selected),
        "session_policy_profiles": store.get_session_policy_profiles(selected),
        "work_session_contract": store.work_session_contract(selected),
        "code_repo_gate": repo_topology.get("code_repo_gate"),
        "validation_policy": project_validation_policy(selected),
        "effective_task_validation": task_validation(task, selected) if task else None,
        "local_docs_policy": (
            "Do not assume repo-local docs define this project. Use this project contract, the "
            "selected project's corpus, tasks, task activity, and active leases as the canonical "
            "boundary. Treat repo docs as project artifacts only when this project or task "
            "explicitly references them."
        ),
        "lane": {
            "id": workstream or None,
            "name": lane_name,
            "task_count": len(lane_tasks),
            "tasks": [_task_brief(item) for item in lane_tasks],
        },
        "assigned_task": _task_brief(task),
        "dependency_status": _dependencies(selected, task),
        "active_agents_in_lane": active_agents,
        "deliverable_scope": deliverable_scope,
        "mission_context": mission_context,
        "milestone_id": (milestone_id or "").strip() or None,
        "deliverable_first_startup_doc": "docs/DELIVERABLE-FIRST-STARTUP.md",
        "operating_rules": operating_rules,
        "recommended_reads": recommended_reads,
    }
