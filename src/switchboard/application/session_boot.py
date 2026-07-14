"""Agent session boot queries — prepare_agent_session + project contract reads.

Transport-neutral builders for startup prompts, first MCP calls, and project
resolution. MCP/REST adapters stay thin; unit tests exercise these helpers
without FastMCP.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

import project_contract as project_contract_service
import store


def resolve_project_input(project: str) -> str:
    """Accept either a project id or its display label, case-insensitively."""
    return project_contract_service.resolve_project_input(project)


def project_ids_for_task(task_id: str) -> list[str]:
    tid = (task_id or "").strip().upper()
    if not tid:
        return []
    matches = []
    for pid in store.project_ids():
        try:
            if store.get_task(tid, project=pid):
                matches.append(pid)
        except Exception:
            continue
    return matches


def project_ids_for_lane(lane: str) -> list[str]:
    ws = (lane or "").strip().upper()
    if not ws:
        return []
    matches = []
    for pid in store.project_ids():
        try:
            if store.list_tasks_slim(workstream=ws, project=pid):
                matches.append(pid)
        except Exception:
            continue
    return matches


def task_boot_brief(task: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not task:
        return None
    return {
        "task_id": task.get("task_id"),
        "title": task.get("title"),
        "status": task.get("status"),
        "workstream": task.get("_wsId"),
        "workstream_name": task.get("_wsName"),
        "owner_person_or_role": task.get("owner_person_or_role"),
        "depends_on": task.get("depends_on") or [],
    }


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug[:48].strip("-") or "session"


def suggest_agent_id(
    runtime: str,
    agent_id: str,
    task_id: str,
    lane: str,
    task: Optional[dict[str, Any]],
) -> str:
    if (agent_id or "").strip():
        return agent_id.strip()
    rt = (runtime or "").strip() or "<runtime>"
    if task_id:
        title = (task or {}).get("title") if task else ""
        return f"{rt}/{task_id}-{slugify(title or lane or 'work')}"
    if lane:
        return f"{rt}/{lane}-{slugify('work')}"
    return f"{rt}/<TASK-ID>-<slug>"


def build_startup_prompt(
    project: str,
    agent_id: str,
    task_id: str,
    lane: str,
    deliverable_id: str = "",
    board_id: str = "",
    mission_id: str = "",
    milestone_id: str = "",
) -> str:
    """Build the project-bound boot prompt (no FastMCP dependency)."""
    access = store.project_access(project)
    deliverable_scope = bool((deliverable_id or board_id or mission_id).strip())
    lines = [
        f'You are enlisting on Switchboard project="{project}" '
        f"({project_contract_service.project_label(project)}).",
        f'Every board/MCP call in this session must include project="{project}".',
        f'Project boundary: {access.get("boundary") or f"Only work belonging to project={project} belongs here."}',
        f'Project purpose: {access.get("purpose") or f"{project} work control plane"}',
        'Do not use project="helm", project="maxwell", or any other board unless '
        "prepare_agent_session selects it.",
        "Use the returned project_contract as the canonical lane/task contract. "
        "Do not assume docs/EPICS.md or other repo-local docs apply unless this "
        "selected project/task explicitly says so.",
        "Ownership: boards/workstreams own execution; deliverables own outcomes, "
        "end_state, milestones, and cross-board proof rollup.",
    ]
    if deliverable_scope:
        scope_ref = (deliverable_id or board_id or mission_id).strip()
        lines.append(
            f'Deliverable-first boot: read get_mission_status for "{scope_ref}" before editing. '
            "Inspect end_state, acceptance_criteria, policy_constraints, milestones, linked_tasks, "
            "blockers, and next_actions. See docs/DELIVERABLE-FIRST-STARTUP.md."
        )
        if (milestone_id or "").strip():
            lines.append(f"Milestone scope: {milestone_id.strip()}")
    lines.extend([
        "Boot sequence:",
        f'1. get_working_agreement(project="{project}")',
        f'2. register_agent(agent_id="{agent_id}", runtime="<your-runtime>", '
        f'lane="{lane or "<lane>"}", '
        f'task_id="{task_id or "<task-id>"}", project="{project}", '
        f'control_json="{{...}}", protocol_json="{{...}}")',
        f'3. list_unacked_messages(to_agent="{agent_id}", project="{project}")',
        f'4. list_unblock_requests(owner_agent="{agent_id}", project="{project}")',
    ])
    step = 5
    if deliverable_scope:
        did = deliverable_id or board_id or mission_id
        lines.append(
            f'{step}. get_mission_status(project="{project}", deliverable_id="{did.strip()}")'
        )
        step += 1
    if task_id:
        lines.append(f'{step}. get_task(task_id="{task_id}", project="{project}")')
    elif lane:
        lines.append(f'{step}. search_tasks(workstream="{lane}", project="{project}")')
    else:
        lines.append(f'{step}. board_summary(project="{project}")')
    lines.append(
        "If a task or lane is missing, stop and call prepare_agent_session again before doing work."
    )
    return "\n".join(lines)


def build_first_calls(
    project: str,
    agent_id: str,
    runtime: str,
    model: str,
    task_id: str,
    lane: str,
    agreement: dict,
    deliverable_id: str = "",
    board_id: str = "",
    mission_id: str = "",
    milestone_id: str = "",
) -> list[dict[str, Any]]:
    """Exact first MCP calls for the boot handshake (no FastMCP dependency)."""
    register_args = {
        "agent_id": agent_id,
        "runtime": runtime or "<runtime>",
        "model": model or "",
        "lane": lane or "",
        "task_id": task_id or "",
        "control_json": json.dumps({"mode": "advisory_poll"}, sort_keys=True),
        "protocol_json": json.dumps(agreement.get("protocol") or {}, sort_keys=True),
        "project": project,
    }
    calls: list[dict[str, Any]] = [
        {"tool": "get_working_agreement", "args": {"project": project}},
        {"tool": "register_agent", "args": register_args},
        {"tool": "list_unacked_messages", "args": {"to_agent": agent_id, "project": project}},
        {"tool": "list_unblock_requests", "args": {"owner_agent": agent_id, "project": project}},
        {"tool": "get_project_contract", "args": {
            "project": project, "task_id": task_id or "", "lane": lane or "",
            "deliverable_id": deliverable_id or "",
            "board_id": board_id or "",
            "mission_id": mission_id or "",
            "milestone_id": milestone_id or "",
        }},
    ]
    if (deliverable_id or board_id or mission_id).strip():
        ms_args: dict[str, str] = {"project": project}
        if deliverable_id:
            ms_args["deliverable_id"] = deliverable_id
        if board_id:
            ms_args["board_id"] = board_id
        if mission_id:
            ms_args["mission_id"] = mission_id
        calls.append({"tool": "get_mission_status", "args": ms_args})
    if task_id:
        calls.append({"tool": "get_task", "args": {"task_id": task_id, "project": project}})
    elif lane:
        calls.append({"tool": "search_tasks", "args": {"workstream": lane, "project": project}})
    else:
        calls.append({"tool": "board_summary", "args": {"project": project}})
    return calls


def get_project_contract(
    project: str = "maxwell",
    lane: str = "",
    task_id: str = "",
    deliverable_id: str = "",
    board_id: str = "",
    mission_id: str = "",
    milestone_id: str = "",
) -> dict[str, Any]:
    """Project/lane/task contract — delegates to root project_contract.build."""
    return project_contract_service.build(
        project=project,
        lane=lane,
        task_id=task_id,
        deliverable_id=deliverable_id,
        board_id=board_id,
        mission_id=mission_id,
        milestone_id=milestone_id,
    )


def prepare_agent_session(
    runtime: str = "",
    agent_id: str = "",
    project: str = "",
    task_id: str = "",
    lane: str = "",
    model: str = "",
    intent: str = "",
    deliverable_id: str = "",
    board_id: str = "",
    mission_id: str = "",
    milestone_id: str = "",
) -> dict[str, Any]:
    """Resolve project/task/lane and return the boot payload (dict, not JSON)."""
    tid = (task_id or "").strip().upper()
    ws = (lane or "").strip().upper()
    selected = resolve_project_input(project)
    task_matches = project_ids_for_task(tid)
    lane_matches = project_ids_for_lane(ws)
    warnings: list[str] = []
    projects_payload = store.projects()

    if selected and not store.has_project(selected):
        return {
            "ok": False,
            "status": "unknown_project",
            "error": f"project '{project}' is not a routable Switchboard project",
            "projects": projects_payload,
            "selected_project": None,
            "task_matches": task_matches,
            "lane_matches": lane_matches,
            "next_step": "Pick one of projects[].id and call prepare_agent_session again.",
        }

    if selected and tid and selected not in task_matches:
        return {
            "ok": False,
            "status": "project_task_mismatch" if task_matches else "task_not_found",
            "error": (
                f"task_id '{tid}' is not on project '{selected}'"
                + (f"; it exists on {', '.join(task_matches)}" if task_matches else "")
            ),
            "projects": projects_payload,
            "selected_project": selected,
            "task_matches": task_matches,
            "lane_matches": lane_matches,
            "next_step": (
                f"Use project='{task_matches[0]}' for task_id='{tid}'."
                if len(task_matches) == 1 else
                "Choose the intended project explicitly, or create the missing task on that project."
            ),
        }

    if selected and ws and lane_matches and selected not in lane_matches:
        return {
            "ok": False,
            "status": "project_lane_mismatch",
            "error": (
                f"lane '{ws}' is not on project '{selected}'; "
                f"it exists on {', '.join(lane_matches)}"
            ),
            "projects": projects_payload,
            "selected_project": selected,
            "task_matches": task_matches,
            "lane_matches": lane_matches,
            "next_step": (
                f"Use project='{lane_matches[0]}' for lane='{ws}'."
                if len(lane_matches) == 1
                else "Choose the intended project explicitly."
            ),
        }

    if not selected:
        candidate_sets = [set(x) for x in (task_matches, lane_matches) if x]
        if candidate_sets:
            common = set.intersection(*candidate_sets)
            candidates = sorted(common or set.union(*candidate_sets))
            if len(candidates) == 1:
                selected = candidates[0]
                warnings.append(f"project inferred from {'task_id' if tid else 'lane'}")
            else:
                return {
                    "ok": False,
                    "status": "choice_required",
                    "error": (
                        "task/lane matches multiple projects"
                        if candidates
                        else "no project could be inferred"
                    ),
                    "projects": projects_payload,
                    "selected_project": None,
                    "task_matches": task_matches,
                    "lane_matches": lane_matches,
                    "next_step": (
                        "Call prepare_agent_session again with project set to one of projects[].id."
                    ),
                }
        else:
            return {
                "ok": False,
                "status": "choice_required",
                "error": "no project, task_id, or lane selected",
                "projects": projects_payload,
                "selected_project": None,
                "task_matches": task_matches,
                "lane_matches": lane_matches,
                "next_step": (
                    "Choose a project id from projects[] before register_agent or claim_next."
                ),
            }

    task = store.get_task(tid, project=selected) if tid else None
    if task and ws and task.get("_wsId") != ws:
        return {
            "ok": False,
            "status": "task_lane_mismatch",
            "error": (
                f"task_id '{tid}' belongs to lane '{task.get('_wsId')}', not lane '{ws}'"
            ),
            "projects": projects_payload,
            "selected_project": selected,
            "task": task_boot_brief(task),
            "next_step": f"Use lane='{task.get('_wsId')}' or pick the correct task.",
        }
    if task and not ws:
        ws = task.get("_wsId") or ""

    agreement = store.get_working_agreement(project=selected)
    chosen_agent_id = suggest_agent_id(runtime, agent_id, tid, ws, task)
    contract = get_project_contract(
        selected,
        lane=ws,
        task_id=tid,
        deliverable_id=deliverable_id,
        board_id=board_id,
        mission_id=mission_id,
        milestone_id=milestone_id,
    )
    deliverable_scope = bool((deliverable_id or board_id or mission_id).strip())
    return {
        "ok": True,
        "status": "ready",
        "projects": projects_payload,
        "selected_project": selected,
        "selected_project_label": project_contract_service.project_label(selected),
        "task_matches": task_matches,
        "lane_matches": lane_matches,
        "task": task_boot_brief(task),
        "lane": ws,
        "agent_id": chosen_agent_id,
        "intent": intent,
        "deliverable_scope": deliverable_scope,
        "deliverable_id": (deliverable_id or "").strip() or None,
        "board_id": (board_id or "").strip() or None,
        "mission_id": (mission_id or "").strip() or None,
        "milestone_id": (milestone_id or "").strip() or None,
        "warnings": warnings,
        "working_agreement": agreement,
        "project_contract": contract,
        "first_calls": build_first_calls(
            selected, chosen_agent_id, runtime, model, tid, ws, agreement,
            deliverable_id=deliverable_id, board_id=board_id,
            mission_id=mission_id, milestone_id=milestone_id),
        "startup_prompt": build_startup_prompt(
            selected, chosen_agent_id, tid, ws,
            deliverable_id=deliverable_id, board_id=board_id,
            mission_id=mission_id, milestone_id=milestone_id),
    }
