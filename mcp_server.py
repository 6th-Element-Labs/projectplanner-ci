#!/usr/bin/env python3
"""MCP server for the Project Maxwell plan (Phase 1.5 — see docs/AGENT_ROADMAP.md).

A second front door over the SAME primitives the web agent uses: read tasks/docs,
ask the plan agent, and create/update tasks — from Cursor, Claude Desktop, Claude
Code, etc. Runs as its own process (Streamable HTTP on 127.0.0.1:8111); Caddy routes
https://plan.taikunai.com/mcp here. Reuses store/rag/agent in-process and shares the
SQLite file (WAL) with the web app.

Auth: reads and writes require bearer when PM_AUTH_MODE=required (MCPAuthMiddleware).
`dev-open` passes through for local/hermetic runs. PM_MCP_TOKEN and explicit store
principals remain supported.
"""
import json
import os
import re

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import agent
import auth
import digest as digest_mod
import external_ci_mirror
import intake as intake_mod
import notify as notify_mod
import rag
import signals
import store
import deliverable_closure
import scripts.switchboard_path  # noqa: F401
from mcp_observability import MCPObservability
from mcp_dispatch import MCPToolDispatcher
from mcp_http_timing import MCPServerTimingMiddleware
from mcp_observability_http import MCPObservabilityEndpoint
from mcp_auth import MCPAuthMiddleware
from switchboard.application.commands import create_task as create_task_command
from switchboard.application.commands import update_task as update_task_command
from switchboard.application.queries import get_task as get_task_query

store.init_project_registry()
for _pid in store.project_ids():  # ensure every project's schema exists (the web app normally seeds them)
    store.init_db(_pid)
    store.seed_if_empty(_pid)

_PORT = int(os.environ.get("PM_MCP_PORT", "8111"))
_PUBLIC_HOST = (os.environ.get("PM_MCP_PUBLIC_HOST") or "plan.taikunai.com").strip()
# We sit behind Caddy (TLS), which forwards Host: <public host>. MCP's DNS-rebinding
# protection rejects unknown Hosts (421), so trust the public host + the local bind.
_SECURITY = TransportSecuritySettings(
    allowed_hosts=[_PUBLIC_HOST, f"127.0.0.1:{_PORT}", f"localhost:{_PORT}", "127.0.0.1", "localhost"],
    allowed_origins=[f"https://{_PUBLIC_HOST}", f"http://127.0.0.1:{_PORT}"],
)

mcp = FastMCP(
    "taikun-plan",
    instructions=(
        "Multi-project planning board. Every task/board tool takes a `project` arg. Built-ins are "
        "'maxwell' (default — TEEP Barnett Phase-1 pilot), 'helm' (the Helm marine-chartplotter "
        "build), and 'switchboard' (the live dogfood board for the agent coordination layer); "
        "additional boards may be created with create_project and discovered with list_projects. "
        "ALWAYS pass project='helm' to read or update Helm tasks (workstreams ENGINE/CHART/CONTRACT/"
        "OWNSHIP/ROUTE/AIS/ALARM/WX/...). ALWAYS pass project='switchboard' for Switchboard/"
        "projectplanner product work. Omit project (or use 'maxwell') only for the Maxwell plan. "
        "Writes go ONLY to the named board — they can never cross. At boot, call prepare_agent_session "
        "with your runtime plus assigned task_id/lane/project; it lists boards, validates the selected "
        "project, and returns a project-bound startup prompt plus a project-level project_contract. "
        "For lane ownership, deliverables, dependencies, and file-boundary hints, use "
        "get_project_contract/project_contract rather than assuming repo-local docs are universal. "
        "Use search_tasks/get_task to read, board_summary for the at-a-glance board, get_plan_signals "
        "for health, and create_task/update_task/add_comment to change a plan. ask_plan and "
        "doc_search also take project — each board has its own segmented corpus.\n\n"
        "SESSION-START HANDSHAKE: (0) call prepare_agent_session(...) if you were assigned a task, "
        "lane, or project and follow its selected project; (1) call get_working_agreement(project) "
        "and follow its rules for the whole session; (2) register_agent; (3) drain your inbox. "
        "DEFINITION OF DONE: follow get_working_agreement(project). Agents use "
        "complete_claim(evidence=...) to record branch/head_sha/PR/offline evidence, release the "
        "claim, and move work to In Review. Agents do not mark Done. Done is reserved for "
        "GitHub/default-branch provenance recorded by webhook/reconcile, or verifier-stamped "
        "offline_evidence for non-PR work. Push your branch before you claim progress; we "
        "squash-merge, so trust the board's recorded merged_sha over local branch state. SAFE "
        "MERGE: if you are authorized to merge, fetch origin, rebase/merge onto the intended "
        "target branch, resolve conflicts intentionally, rerun relevant tests, push the updated "
        "branch, merge through GitHub/merge queue only when checks/review are green, then fetch "
        "the target branch and record the resulting merged_sha; never set Done manually."
    ),
    host="127.0.0.1",
    port=_PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    transport_security=_SECURITY,
)

# FastMCP registers functions at decoration time.  Replacing the instance's
# decorator here instruments every tool below without duplicating timing code in
# ~150 handlers, while functools.wraps preserves the schemas FastMCP derives.
_mcp_observability = MCPObservability()
# HARDEN-63: attribute store lock-wait retries to the in-flight tool so per-tool
# contention is visible even when the retry loop transparently recovers.
try:
    store.register_lock_wait_observer(_mcp_observability.note_sqlite_lock_wait)
except Exception:
    pass
_mcp_dispatch = MCPToolDispatcher(
    # These are deliberately tiny diagnostics. Keeping them inline proves that
    # the event loop remains responsive while ordinary sync tools run in workers.
    inline_tools={"control_plane_probe", "get_mcp_observability"},
)
_register_mcp_tool = mcp.tool


def _observed_mcp_tool(*args, **kwargs):
    register = _register_mcp_tool(*args, **kwargs)

    def observed_register(fn):
        observed = _mcp_observability.wrap(fn)
        register(_mcp_dispatch.wrap(observed))
        # Keep direct Python callers and the existing hermetic tests synchronous;
        # only FastMCP's registered request handler needs worker dispatch.
        return fn

    return observed_register


mcp.tool = _observed_mcp_tool


def _dumps(obj) -> str:
    """json.dumps with sort_keys=True — deterministic serialization for prompt-cache hits.
    Stable key order means identical responses share a cache hit across agent sessions."""
    return json.dumps(obj, sort_keys=True)


def _require_write(ctx, project: str = "maxwell", scopes=("write:tasks",)):
    """Gate writes through the shared Switchboard bearer-principal path."""
    try:
        principal = auth.authenticate(project, auth.bearer_from_mcp_context(ctx),
                                      scopes, dev_actor="MCP")
    except PermissionError as e:
        raise ValueError(str(e))
    # HARDEN-63: this call took the write path — feed the write-latency histogram.
    _mcp_observability.mark_write()
    return principal


def _resolve_write_actor(principal, project: str = "maxwell", task_id: str = "",
                         agent_id: str = "", system_actor: str = "",
                         system_reason: str = ""):
    binding = store.resolve_write_actor(
        auth.actor(principal),
        project=project,
        task_id=task_id,
        agent_id=agent_id,
        system_actor=system_actor,
        system_reason=system_reason,
        principal_id=principal.get("id") or "",
    )
    if not binding.get("ok"):
        return binding
    return binding


def _write_binding_comment(task_id: str, binding, project: str = "maxwell") -> None:
    if not task_id or not isinstance(binding, dict):
        return
    if binding.get("binding") in ("principal", None):
        return
    store.append_activity(
        "principal.write_bound",
        "switchboard/identity",
        store.write_binding_activity_payload(binding),
        task_id=task_id,
        project=project,
    )


def _dep_ids(s):
    """Parse a comma/space/newline-separated list of task ids into a deduped, upper-cased list."""
    out, seen = [], set()
    for tok in (s or "").replace("\n", ",").replace(" ", ",").split(","):
        t = tok.strip().upper()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _unknown_ids(ids, project):
    """Task ids that don't exist on the project. A dependency to a non-existent task is a broken
    graph edge (invalid input) — callers REJECT it rather than write a dangling reference that would
    spread into every audit that traverses the graph."""
    return [d for d in ids if not store.get_task(d, project=project)]


def _resolve_project_input(project: str) -> str:
    """Accept either a project id or its display label, case-insensitively."""
    value = (project or "").strip()
    if not value:
        return ""
    lowered = value.lower()
    for p in store.projects():
        if lowered in (p["id"].lower(), (p.get("label") or "").lower()):
            return p["id"]
    return lowered


def _project_ids_for_task(task_id: str) -> list[str]:
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


def _project_ids_for_lane(lane: str) -> list[str]:
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


def _task_boot_brief(task):
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


def _task_contract_brief(task):
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
    }


def _dependency_contract(project: str, task) -> list[dict]:
    out = []
    for dep in (task or {}).get("depends_on") or []:
        dt = store.get_task(dep, project=project)
        out.append({
            "task_id": dep,
            "exists": bool(dt),
            "status": dt.get("status") if dt else None,
            "title": dt.get("title") if dt else None,
            "workstream": dt.get("_wsId") if dt else None,
        })
    return out


def _project_contract(project: str, lane: str = "", task_id: str = "",
                      deliverable_id: str = "", board_id: str = "",
                      mission_id: str = "", milestone_id: str = "") -> dict:
    selected = _resolve_project_input(project) or store.DEFAULT_PROJECT
    if not store.has_project(selected):
        return {
            "ok": False,
            "status": "unknown_project",
            "error": f"project '{project}' is not a routable Switchboard project",
            "projects": store.projects(),
        }
    tid = (task_id or "").strip().upper()
    task = store.get_task(tid, project=selected) if tid else None
    ws = (lane or "").strip().upper()
    if task and not ws:
        ws = task.get("_wsId") or ""
    access = store.project_access(selected)
    repo_topology = store.get_project_repo_topology(selected)
    boards_missions = store.list_project_boards(project=selected)
    repo_role_guide = store.repo_topology_role_guide(selected)
    lane_tasks = store.list_tasks_slim(workstream=ws, project=selected) if ws else []
    lane_name = None
    for lt in lane_tasks:
        if lt.get("_wsName"):
            lane_name = lt.get("_wsName")
            break
    active_agents = []
    try:
        active_agents = [
            {
                "agent_id": a.get("agent_id"),
                "runtime": a.get("runtime"),
                "lane": a.get("lane"),
                "task_id": a.get("task_id"),
                "stale": a.get("stale"),
            }
            for a in store.list_active_agents(lane=ws, project=selected)
        ] if ws else []
    except Exception:
        active_agents = []
    mission_context = None
    deliverable_scope = bool((deliverable_id or board_id or mission_id).strip())
    if deliverable_scope:
        mission_context = store.get_mission_status(
            project=selected,
            deliverable_id=deliverable_id,
            board_id=board_id,
            mission_id=mission_id,
        )
    operating_rules = [
        f'Pass project="{selected}" on every Switchboard MCP call.',
        access.get("boundary") or f"Only work belonging to project={selected} belongs here.",
        "Treat Project as the repo/trust/policy/access/CI/model/budget/Done authority boundary.",
        "Use Project Board/Mission ids for outcome cockpits under the selected Project; do not invent unknown board_id values.",
        "Boards and workstreams own execution; deliverables own outcomes, end_state, and cross-board proof rollup.",
        "Treat repo_topology.roles.canonical as the only code-truth / Done authority.",
        "Treat repo_topology.roles.public_ci as verification evidence only, even when it is a shared public repo.",
        "Read task description, deliverable, exit_criteria, dependencies, and recent activity before editing.",
        "If file ownership is unclear, check active leases/agent state and ask the board or human before writing.",
        "Do not import Helm lane/file ownership into non-Helm projects.",
    ]
    recommended_reads = [x for x in [
        f'get_task(task_id="{tid}", project="{selected}")' if tid else None,
        f'search_tasks(workstream="{ws}", project="{selected}")' if ws else None,
        f'get_agent_state(task_id="{tid}", project="{selected}")' if tid else None,
    ] if x]
    if deliverable_scope and mission_context and not mission_context.get("error"):
        did = mission_context.get("deliverable_id") or deliverable_id
        recommended_reads = [
            f'get_mission_status(project="{selected}", deliverable_id="{did}")',
            f'get_deliverable("{did}", project="{selected}")',
        ] + recommended_reads
        if (milestone_id or "").strip():
            recommended_reads.insert(1,
                f'claim_next(agent_id=..., project="{selected}", deliverable_id="{did}", '
                f'milestone_id="{milestone_id.strip()}")')
    return {
        "ok": True,
        "source_of_truth": "switchboard_project_contract",
        "project": selected,
        "project_label": _project_label(selected),
        "project_access": access,
        "project_hierarchy": repo_topology.get("project_hierarchy"),
        "boards_missions": boards_missions,
        "repo_topology": repo_topology,
        "repo_role_guide": repo_role_guide,
        "session_policy_profiles": store.get_session_policy_profiles(selected),
        "work_session_contract": store.work_session_contract(selected),
        "code_repo_gate": repo_topology.get("code_repo_gate"),
        "local_docs_policy": (
            "Do not assume repo-local docs such as docs/EPICS.md define this project. "
            "Use this Switchboard project contract, get_task, search_tasks, task activity, and active leases "
            "as the canonical lane/task boundary. Treat repo docs as project artifacts only when the selected "
            "project or task explicitly references them."
        ),
        "lane": {
            "id": ws or None,
            "name": lane_name,
            "task_count": len(lane_tasks),
            "tasks": [_task_contract_brief(t) for t in lane_tasks],
        },
        "assigned_task": _task_contract_brief(task),
        "dependency_status": _dependency_contract(selected, task),
        "active_agents_in_lane": active_agents,
        "deliverable_scope": deliverable_scope,
        "mission_context": mission_context,
        "milestone_id": (milestone_id or "").strip() or None,
        "deliverable_first_startup_doc": "docs/DELIVERABLE-FIRST-STARTUP.md",
        "operating_rules": operating_rules,
        "recommended_reads": recommended_reads,
    }


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug[:48].strip("-") or "session"


def _suggest_agent_id(runtime: str, agent_id: str, task_id: str, lane: str, task) -> str:
    if (agent_id or "").strip():
        return agent_id.strip()
    rt = (runtime or "").strip() or "<runtime>"
    if task_id:
        title = (task or {}).get("title") if task else ""
        return f"{rt}/{task_id}-{_slugify(title or lane or 'work')}"
    if lane:
        return f"{rt}/{lane}-{_slugify('work')}"
    return f"{rt}/<TASK-ID>-<slug>"


def _project_label(project: str) -> str:
    for p in store.projects():
        if p["id"] == project:
            return p.get("label") or project
    return project


def _agent_bootstrap_prompt(project: str, agent_id: str, task_id: str, lane: str,
                            deliverable_id: str = "", board_id: str = "",
                            mission_id: str = "", milestone_id: str = "") -> str:
    access = store.project_access(project)
    deliverable_scope = bool((deliverable_id or board_id or mission_id).strip())
    lines = [
        f'You are enlisting on Switchboard project="{project}" ({_project_label(project)}).',
        f'Every board/MCP call in this session must include project="{project}".',
        f'Project boundary: {access.get("boundary") or f"Only work belonging to project={project} belongs here."}',
        f'Project purpose: {access.get("purpose") or f"{project} work control plane"}',
        'Do not use project="helm", project="maxwell", or any other board unless prepare_agent_session selects it.',
        "Use the returned project_contract as the canonical lane/task contract. Do not assume docs/EPICS.md or other repo-local docs apply unless this selected project/task explicitly says so.",
        "Ownership: boards/workstreams own execution; deliverables own outcomes, end_state, milestones, and cross-board proof rollup.",
    ]
    if deliverable_scope:
        scope_ref = (deliverable_id or board_id or mission_id).strip()
        lines.append(
            f'Deliverable-first boot: read get_mission_status for "{scope_ref}" before editing. '
            "Inspect end_state, acceptance_criteria, policy_constraints, milestones, linked_tasks, "
            "blockers, and next_actions. See docs/DELIVERABLE-FIRST-STARTUP.md."
        )
        if (milestone_id or "").strip():
            lines.append(f'Milestone scope: {milestone_id.strip()}')
    lines.extend([
        "Boot sequence:",
        f'1. get_working_agreement(project="{project}")',
        f'2. register_agent(agent_id="{agent_id}", runtime="<your-runtime>", lane="{lane or "<lane>"}", '
        f'task_id="{task_id or "<task-id>"}", project="{project}", control_json="{{...}}", protocol_json="{{...}}")',
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
    lines.append('If a task or lane is missing, stop and call prepare_agent_session again before doing work.')
    return "\n".join(lines)


def _first_calls(project: str, agent_id: str, runtime: str, model: str,
                 task_id: str, lane: str, agreement: dict,
                 deliverable_id: str = "", board_id: str = "",
                 mission_id: str = "", milestone_id: str = "") -> list[dict]:
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
    calls = [
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
        ms_args = {"project": project}
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


# ---- read tools (open) ---------------------------------------------------
@mcp.tool()
def list_projects() -> str:
    """List all routable project boards. Returns [{id,label,pretitle}] plus the default id."""
    return _dumps({"projects": store.projects(), "default": store.DEFAULT_PROJECT})


@mcp.tool()
def get_project_contract(project: str = "maxwell", lane: str = "", task_id: str = "",
                       deliverable_id: str = "", board_id: str = "",
                       mission_id: str = "", milestone_id: str = "") -> str:
    """Return the project-level project/lane/task contract for any Switchboard project.

    This is the project-agnostic replacement for assuming repo-local files such as
    docs/EPICS.md describe the active board. It returns the selected project, lane tasks,
    assigned task deliverable/exit criteria/dependencies, active agents in the lane, and
    operating rules. When deliverable_id or board_id/mission_id is set, it also returns
    mission_context from get_mission_status. Use it at boot and whenever a repo contains
    docs for a different project.
    """
    return _dumps(_project_contract(
        project=project, lane=lane, task_id=task_id,
        deliverable_id=deliverable_id, board_id=board_id,
        mission_id=mission_id, milestone_id=milestone_id))


@mcp.tool()
def prepare_agent_session(runtime: str = "", agent_id: str = "", project: str = "",
                          task_id: str = "", lane: str = "", model: str = "",
                          intent: str = "", deliverable_id: str = "",
                          board_id: str = "", mission_id: str = "",
                          milestone_id: str = "") -> str:
    """Boot-time resolver for autonomous agents.

    Call this BEFORE get_working_agreement/register_agent/claim_next. It lists available
    project boards, resolves task_id or lane to the correct project when possible, validates
    any explicit project choice, and returns a project-bound startup prompt plus exact first
    MCP calls. This prevents agents from silently landing on the default Maxwell board or
    doing Vulkan work on Helm.

    When deliverable_id or board_id/mission_id is set, the session is deliverable-first:
    project is the mission-home project that owns the deliverable record, and first_calls
    include get_mission_status before task work begins.
    """
    tid = (task_id or "").strip().upper()
    ws = (lane or "").strip().upper()
    selected = _resolve_project_input(project)
    task_matches = _project_ids_for_task(tid)
    lane_matches = _project_ids_for_lane(ws)
    warnings: list[str] = []
    projects_payload = store.projects()

    if selected and not store.has_project(selected):
        return _dumps({
            "ok": False,
            "status": "unknown_project",
            "error": f"project '{project}' is not a routable Switchboard project",
            "projects": projects_payload,
            "selected_project": None,
            "task_matches": task_matches,
            "lane_matches": lane_matches,
            "next_step": "Pick one of projects[].id and call prepare_agent_session again.",
        })

    if selected and tid and selected not in task_matches:
        return _dumps({
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
        })

    if selected and ws and lane_matches and selected not in lane_matches:
        return _dumps({
            "ok": False,
            "status": "project_lane_mismatch",
            "error": f"lane '{ws}' is not on project '{selected}'; it exists on {', '.join(lane_matches)}",
            "projects": projects_payload,
            "selected_project": selected,
            "task_matches": task_matches,
            "lane_matches": lane_matches,
            "next_step": f"Use project='{lane_matches[0]}' for lane='{ws}'." if len(lane_matches) == 1
            else "Choose the intended project explicitly.",
        })

    if not selected:
        candidate_sets = [set(x) for x in (task_matches, lane_matches) if x]
        if candidate_sets:
            common = set.intersection(*candidate_sets)
            candidates = sorted(common or set.union(*candidate_sets))
            if len(candidates) == 1:
                selected = candidates[0]
                warnings.append(f"project inferred from {'task_id' if tid else 'lane'}")
            else:
                return _dumps({
                    "ok": False,
                    "status": "choice_required",
                    "error": "task/lane matches multiple projects" if candidates else "no project could be inferred",
                    "projects": projects_payload,
                    "selected_project": None,
                    "task_matches": task_matches,
                    "lane_matches": lane_matches,
                    "next_step": "Call prepare_agent_session again with project set to one of projects[].id.",
                })
        else:
            return _dumps({
                "ok": False,
                "status": "choice_required",
                "error": "no project, task_id, or lane selected",
                "projects": projects_payload,
                "selected_project": None,
                "task_matches": task_matches,
                "lane_matches": lane_matches,
                "next_step": "Choose a project id from projects[] before register_agent or claim_next.",
            })

    task = store.get_task(tid, project=selected) if tid else None
    if task and ws and task.get("_wsId") != ws:
        return _dumps({
            "ok": False,
            "status": "task_lane_mismatch",
            "error": f"task_id '{tid}' belongs to lane '{task.get('_wsId')}', not lane '{ws}'",
            "projects": projects_payload,
            "selected_project": selected,
            "task": _task_boot_brief(task),
            "next_step": f"Use lane='{task.get('_wsId')}' or pick the correct task.",
        })
    if task and not ws:
        ws = task.get("_wsId") or ""

    agreement = store.get_working_agreement(project=selected)
    chosen_agent_id = _suggest_agent_id(runtime, agent_id, tid, ws, task)
    contract = _project_contract(
        selected, lane=ws, task_id=tid,
        deliverable_id=deliverable_id, board_id=board_id,
        mission_id=mission_id, milestone_id=milestone_id)
    deliverable_scope = bool((deliverable_id or board_id or mission_id).strip())
    return _dumps({
        "ok": True,
        "status": "ready",
        "projects": projects_payload,
        "selected_project": selected,
        "selected_project_label": _project_label(selected),
        "task_matches": task_matches,
        "lane_matches": lane_matches,
        "task": _task_boot_brief(task),
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
        "first_calls": _first_calls(
            selected, chosen_agent_id, runtime, model, tid, ws, agreement,
            deliverable_id=deliverable_id, board_id=board_id,
            mission_id=mission_id, milestone_id=milestone_id),
        "startup_prompt": _agent_bootstrap_prompt(
            selected, chosen_agent_id, tid, ws,
            deliverable_id=deliverable_id, board_id=board_id,
            mission_id=mission_id, milestone_id=milestone_id),
    })


@mcp.tool()
def search_tasks(workstream: str = "", status: str = "", owner_person: str = "",
                 blocking: bool = False, query: str = "", project: str = "maxwell") -> str:
    """Filter a plan's tasks. project selects the board ('maxwell' default, 'helm', or
    'switchboard'). All other args optional: workstream id (SSO/SEN/... for Maxwell;
    ENGINE/CHART/... for Helm; PROTO/ADAPTER/ENFORCE/... for Switchboard), status
    (Not Started|In Progress|In Review|Blocked|Done), owner_person substring, blocking, free-text query.
    Returns a JSON list of {task_id,title,status,owner_person_or_role,workstream,...}."""
    return _dumps(agent._search_tasks({
        "workstream": workstream, "status": status, "owner_person": owner_person,
        "blocking": blocking, "query": query}, project=project))


@mcp.tool()
def get_task(task_id: str, project: str = "maxwell") -> str:
    """Full detail of one task: description, all fields, dependencies, and recent activity.
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    t = get_task_query.execute_for(task_id, project=project)
    return _dumps(agent._task_brief(t, full=True)) if t else "no such task"


@mcp.tool()
def board_summary(project: str = "maxwell") -> str:
    """Full board snapshot: project name + rollups, then one line per task.
    Use ONCE at session start for orientation. For recurring 'has anything changed?' checks
    use get_lane_delta instead — it returns only what changed and costs ~50 tokens when nothing
    did vs ~3000-5000 tokens here. project selects the board ('maxwell' default, 'helm',
    or 'switchboard')."""
    return (f"Project: {store.get_meta('project', project=project)}\n"
            f"Rollups: {_dumps(store.board_rollups(project=project))}\n\n"
            f"{agent.board_summary_text(project=project)}")


@mcp.tool()
def get_lane_delta(project: str = "maxwell", lane: str = "", since_cursor: int = 0) -> str:
    """Efficient poll replacement — returns ONLY tasks that changed since your last call.
    Use this instead of board_summary in any polling loop. Costs ~50 tokens when nothing
    changed (empty updates list) vs 3000-5000 tokens for a full board_summary.

    project: 'maxwell', 'helm', or 'switchboard'. lane: workstream id to filter (e.g. 'ENGINE',
    'CHART', 'OWNSHIP', 'ADAPTER') — leave blank for all workstreams. since_cursor: the cursor value from your
    last response; pass 0 on first call.

    Returns {cursor, updates: [{task_id, status, title, workstream_id, kinds}]}.
    Save the returned cursor and pass it on your next call. kinds lists the activity types
    that occurred (edit, comment, create). Call get_task for full detail on any changed task."""
    return _dumps(store.get_activity_delta(since_cursor=since_cursor, lane=lane, project=project))


@mcp.tool()
def control_plane_probe(project: str = "maxwell", lane: str = "",
                        include_heavy: bool = False) -> str:
    """Tiny latency probe for MCP clients. Compare your client wall time to server_elapsed_ms.
    A large gap means time is outside Switchboard's Python/SQLite path."""
    probe = store.control_plane_probe(project=project, lane=lane, include_heavy=include_heavy)
    probe["mcp_framing"] = {
        "stateless_http": True,
        "approx_tool_payload_bytes": len(_dumps(probe).encode("utf-8")),
    }
    return _dumps(probe)


@mcp.tool()
def get_mcp_observability(tool: str = "", slow_limit: int = 50) -> str:
    """Process-local MCP health: per-tool p50/p99/max latency, per-tool SQLite
    lock-wait counts, write-path latency p50/p99 (per tool and aggregate), failures,
    and a bounded slow-call log. No arguments, results, tokens, or other request
    content are retained. tool optionally filters by exact tool name; slow_limit is
    capped by PM_MCP_SLOW_LOG_LIMIT. The same snapshot is scrapeable over plain HTTP
    at GET /observability for operators/monitors that don't speak MCP."""
    return _dumps(_mcp_observability.snapshot(tool=tool, slow_limit=slow_limit))


@mcp.tool()
def get_saturation_signals(project: str = "switchboard") -> str:
    """Box saturation dashboard (PERF-7): PSI pressure, sqlite lock-waits, webhook inbox
    depth, HTTP/MCP SLO status, load-shed recommendation, and alert list."""
    import saturation_signals as sat

    def _mcp_obs():
        window_s = float(os.environ.get("PM_SQLITE_LOCK_WAIT_WINDOW_S", "60"))
        snap = _mcp_observability.snapshot()
        store_waits = store.sqlite_lock_wait_count()
        store_window = store.sqlite_lock_waits_in_window(window_s)
        snap["sqlite_lock_waits"] = max(int(snap.get("sqlite_lock_waits") or 0), store_waits)
        snap["sqlite_lock_waits_window"] = store_window
        snap["sqlite_lock_wait_window_s"] = window_s
        return snap

    return _dumps(sat.compute_saturation_signals(
        project=project,
        mcp_obs_provider=_mcp_obs,
        request_obs_provider=lambda: {"routes": {}, "dropped_webhook_deliveries": 0},
    ))


@mcp.tool()
def doc_search(query: str, project: str = "maxwell") -> str:
    """Search a project's corpus and return cited snippets: [{file, text}]. The static plan docs
    (PRD, architecture, integrations, security, the full plan) are Maxwell's, so the default
    project searches those plus its ingested artifacts; every other project searches only its own
    ingested transcripts/emails/documents. project selects the board."""
    hits = rag.search(query, top_k=5, project=project)
    return _dumps([{"file": h["file"], "text": h["text"]} for h in hits]) if hits else "no matches"


@mcp.tool()
def get_plan_signals(project: str = "maxwell") -> str:
    """Derived plan health: counts + overdue/due-soon/blocked/ready tasks, critical-path slips,
    past-due decisions, and each owner's next-best 1-2 tasks. Use for 'what's slipping?' or digests.
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    return _dumps(signals.compute_plan_signals(project=project))


@mcp.tool()
def get_working_agreement(project: str = "maxwell") -> str:
    """Connect-time policy for agents: definition of done, branch convention, merge strategy,
    canonical main SHA, and the session-start sequence. Call before register_agent."""
    return _dumps(store.get_working_agreement(project=project))


@mcp.tool()
def ask_plan(question: str, project: str = "maxwell") -> str:
    """Ask the plan-wide agent a question about a board. project selects it ('maxwell' default,
    'helm', or 'switchboard'). Helm and Switchboard answers are grounded in the live board; Maxwell
    also grounds in the plan docs via RAG. Returns a reasoned answer (+ sources) and, when relevant,
    a proposed task change (NOT applied — call update_task to apply it)."""
    r = agent.run(None, question, project=project)
    return _dumps({"answer": r.get("answer"), "sources": r.get("sources"),
                   "proposed_change": r.get("proposal")})


# ---- file lease tools (open — advisory, no token required) ---------------
@mcp.tool()
def claim_files(agent_id: str, files: str, ctx: Context, project: str = "maxwell",
                task_id: str = "", ttl_minutes: int = 30) -> str:
    """Claim file paths before editing them (advisory soft lock — prevents parallel agents
    from clobbering each other). Call before writing; call release_files when done.

    agent_id: a stable string identifying this agent session (e.g. 'claude/ENGINE-11').
    files: comma or newline-separated list of paths (relative to repo root).
    task_id: the board task you're working on (optional but recommended).
    ttl_minutes: auto-expire after this many minutes if release_files is never called (default 30).

    On success: {lease_id, files, expires_at, ttl_minutes}
    On conflict: {conflict, task_id, files, retry_after_seconds} — use Bash(sleep N) before retrying."""
    file_list = [f.strip() for f in files.replace("\n", ",").split(",") if f.strip()]
    if not file_list:
        return _dumps({"error": "no files given"})
    _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.claim_files(agent_id, file_list,
                                    task_id=task_id or None,
                                    ttl_minutes=max(1, ttl_minutes),
                                    project=project))


@mcp.tool()
def release_files(lease_id: str, ctx: Context, project: str = "maxwell") -> str:
    """Release a file lease when you are done editing. Pass the lease_id returned by
    claim_files. Idempotent — releasing an already-released lease returns an error but does
    not corrupt state. project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.release_files(lease_id, project=project))


@mcp.tool()
def check_files(files: str, project: str = "maxwell") -> str:
    """Check whether any of the given file paths are held by an active lease.
    files: comma or newline-separated list of paths.
    Returns a list of {file, held_by, task_id, expires_at} for files that ARE held.
    Empty list means all files are free — safe to edit without claiming first (though
    calling claim_files is strongly preferred to avoid races).
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    file_list = [f.strip() for f in files.replace("\n", ",").split(",") if f.strip()]
    if not file_list:
        return _dumps([])
    return _dumps(store.check_files(file_list, project=project))


@mcp.tool()
def list_active_leases(project: str = "maxwell") -> str:
    """All active file leases on the board — who holds what, and when it expires.
    Use to see which agents are currently active and which files they have claimed.
    Expired and released leases are not shown.
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    return _dumps(store.list_active_leases(project=project))


# ---- IXP-core runtime lifecycle -----------------------------------------
@mcp.tool()
def register_agent(agent_id: str, runtime: str, ctx: Context, model: str = "",
                   lane: str = "", task_id: str = "", ttl_s: int = 120,
                   control_json: str = "{}", protocol_json: str = "{}",
                   project: str = "maxwell") -> str:
    """Register a live agent session. Call at session start before claiming work.
    control_json advertises truthful control fidelity, e.g. {"mode":"advisory_poll"}.
    protocol_json advertises the adapter protocol envelope returned by get_working_agreement."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        control = json.loads(control_json or "{}")
    except Exception:
        return _dumps({"error": "control_json must be a JSON object string"})
    try:
        protocol = json.loads(protocol_json or "{}")
    except Exception:
        return _dumps({"error": "protocol_json must be a JSON object string"})
    return _dumps(store.register_agent(
        agent_id=agent_id, runtime=runtime, model=model, lane=lane, task_id=task_id,
        ttl_s=ttl_s, control=control, protocol=protocol, principal_id=principal["id"],
        actor=auth.actor(principal), project=project))


@mcp.tool()
def heartbeat(agent_id: str, ctx: Context, project: str = "maxwell") -> str:
    """Renew presence for a registered agent session."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.heartbeat(agent_id, actor=auth.actor(principal), project=project))


@mcp.tool()
def list_active_agents(project: str = "maxwell", lane: str = "") -> str:
    """List active registered agents and their advertised control fidelity."""
    return _dumps(store.list_active_agents(lane=lane, project=project))


@mcp.tool()
def register_host(host_id: str, runtimes_json: str, ctx: Context,
                  hostname: str = "", repo_root: str = "",
                  agent_host_version: str = "0.1.0",
                  limits_json: str = "{}", heartbeat_ttl_s: int = 60,
                  project: str = "maxwell") -> str:
    """Register an always-on Agent Host that can wake/start runtimes.

    runtimes_json is a JSON list, e.g. [{"runtime":"claude-code","lanes":["ADAPTER"],
    "capabilities":["python","docs"]}]. limits_json can include {"max_sessions":2}.
    """
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        runtimes = json.loads(runtimes_json or "[]")
        limits = json.loads(limits_json or "{}")
    except Exception:
        return _dumps({"error": "runtimes_json and limits_json must be valid JSON"})
    return _dumps(store.register_host(
        {"host_id": host_id, "hostname": hostname, "repo_root": repo_root,
         "agent_host_version": agent_host_version, "runtimes": runtimes,
         "limits": limits, "heartbeat_ttl_s": heartbeat_ttl_s},
        principal_id=principal["id"], actor=auth.actor(principal), project=project))


@mcp.tool()
def heartbeat_host(host_id: str, ctx: Context, active_sessions: int = -1,
                   capacity_json: str = "{}", status: str = "online",
                   last_error: str = "", project: str = "maxwell") -> str:
    """Renew liveness/capacity for an Agent Host."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        capacity = json.loads(capacity_json or "{}")
    except Exception:
        return _dumps({"error": "capacity_json must be a JSON object string"})
    return _dumps(store.heartbeat_host(
        host_id, active_sessions=(None if active_sessions < 0 else active_sessions),
        capacity=capacity, status=status, last_error=last_error,
        actor=auth.actor(principal), project=project))


@mcp.tool()
def list_agent_hosts(project: str = "maxwell", runtime: str = "", lane: str = "",
                     capability: str = "", include_stale: bool = False) -> str:
    """List registered Agent Hosts and their wake capacity."""
    return _dumps(store.list_agent_hosts(runtime=runtime, lane=lane,
                                        capability=capability,
                                        include_stale=include_stale,
                                        project=project))


@mcp.tool()
def host_status(host_id: str, project: str = "maxwell") -> str:
    """Return one Agent Host's inventory, liveness, capacity, and wake counts."""
    return _dumps(store.host_status(host_id, project=project))


@mcp.tool()
def list_runner_sessions(project: str = "maxwell", host_id: str = "", runtime: str = "",
                         task_id: str = "", status: str = "",
                         include_stale: bool = False) -> str:
    """List live runner sessions with host/runtime/task/claim/fidelity and available actions."""
    return _dumps(store.list_runner_sessions(
        host_id=host_id, runtime=runtime, task_id=task_id, status=status,
        include_stale=include_stale, project=project))


@mcp.tool()
def register_runner_session(runner_session_json: str, ctx: Context,
                            project: str = "maxwell") -> str:
    """Register or heartbeat one supervised runner session.

    runner_session_json should include runner_session_id, host_id, agent_id, runtime,
    task_id/claim_id when known, status, and control. runner_kill is accepted only for
    host-owned managed_process sessions.
    """
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        record = json.loads(runner_session_json or "{}")
    except Exception:
        return _dumps({"error": "runner_session_json must be a JSON object string"})
    return _dumps(store.upsert_runner_session(
        record, principal_id=principal["id"], actor=auth.actor(principal), project=project))


@mcp.tool()
def create_work_session(work_session_json: str, ctx: Context,
                        project: str = "maxwell") -> str:
    """Create a first-class Work Session for code work.

    The session binds agent/task work to project, repo_role, branch, worktree/clone path,
    hygiene state, and lease/env evidence. Enforcement into claim_task and complete_claim
    lands in follow-on SESSION tasks.
    """
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        payload = json.loads(work_session_json or "{}")
    except json.JSONDecodeError:
        return _dumps({"error": "work_session_json must be valid JSON"})
    return _dumps(store.create_work_session(
        payload, actor=auth.actor(principal), principal_id=principal.get("id") or "",
        project=project))


@mcp.tool()
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
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        payload = json.loads(managed_session_json or "{}")
    except json.JSONDecodeError:
        return _dumps({"error": "managed_session_json must be valid JSON"})
    return _dumps(store.create_managed_work_session(
        payload, actor=auth.actor(principal), principal_id=principal.get("id") or "",
        project=project))


@mcp.tool()
def get_work_session(work_session_id: str, project: str = "maxwell") -> str:
    """Read one Work Session by id."""
    session = store.get_work_session(work_session_id, project=project)
    return _dumps(session or {"error": "work_session_not_found",
                             "work_session_id": work_session_id})


@mcp.tool()
def get_work_session_health(work_session_id: str, project: str = "maxwell") -> str:
    """Read the computed health verdict for one Work Session."""
    health = store.get_work_session_health(work_session_id, project=project)
    return _dumps(health or {"error": "work_session_not_found",
                            "work_session_id": work_session_id})


@mcp.tool()
def list_work_sessions(project: str = "maxwell", task_id: str = "",
                       agent_id: str = "", status: str = "",
                       repo_role: str = "", include_expired: bool = True) -> str:
    """List Work Sessions for a project, task, agent, repo role, or lifecycle status."""
    return _dumps({
        "project": project,
        "contract": store.work_session_contract(project) if store.has_project(project) else None,
        "work_sessions": store.list_work_sessions(
            project=project, task_id=task_id, agent_id=agent_id, status=status,
            repo_role=repo_role, include_expired=include_expired),
    })


@mcp.tool()
def list_session_health(project: str = "maxwell", task_id: str = "",
                        agent_id: str = "", status: str = "",
                        only_unsafe: bool = False) -> str:
    """List computed Work Session health rows and the task-level session_health aggregate."""
    return _dumps(store.list_session_health(
        project=project, task_id=task_id, agent_id=agent_id,
        status=status, only_unsafe=only_unsafe))


@mcp.tool()
def update_work_session(work_session_id: str, updates_json: str, ctx: Context,
                        project: str = "maxwell") -> str:
    """Update Work Session state, hygiene, leases, SHAs, branch, or lifecycle status."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        payload = json.loads(updates_json or "{}")
    except json.JSONDecodeError:
        return _dumps({"error": "updates_json must be valid JSON"})
    return _dumps(store.update_work_session(
        work_session_id, payload, actor=auth.actor(principal), project=project))


@mcp.tool()
def archive_work_session_workspace(work_session_id: str, ctx: Context,
                                   project: str = "maxwell",
                                   remove_workspace: bool = False) -> str:
    """Archive a managed Work Session and optionally remove its owned workspace path."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.archive_work_session_workspace(
        work_session_id,
        remove_workspace=remove_workspace,
        actor=auth.actor(principal),
        project=project))


@mcp.tool()
def repo_preflight(worktree_path: str, ctx: Context, project: str = "maxwell",
                   task_id: str = "", agent_id: str = "", repo_role: str = "canonical",
                   expected_branch: str = "", expected_base_ref: str = "",
                   scan_conflicts: bool = True) -> str:
    """Run a side-effect-free git/worktree preflight before edit/claim/complete/merge.

    Returns pass/warn/deny with typed failure classes such as dirty_worktree,
    conflict_markers, wrong_repo, wrong_branch, stale_base, and
    shared_worktree_collision.
    """
    _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.repo_preflight(
        worktree_path=worktree_path, project=project, task_id=task_id,
        agent_id=agent_id, repo_role=repo_role, expected_branch=expected_branch,
        expected_base_ref=expected_base_ref, scan_conflicts=scan_conflicts))


@mcp.tool()
def preflight_work_session(work_session_id: str, ctx: Context, project: str = "maxwell",
                           expected_branch: str = "", expected_base_ref: str = "") -> str:
    """Run repo_preflight for a Work Session path and write the result into hygiene."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.preflight_work_session(
        work_session_id, actor=auth.actor(principal), project=project,
        expected_branch=expected_branch, expected_base_ref=expected_base_ref))


@mcp.tool()
def pre_tool_check(ctx: Context, tool_name: str = "", tool_input_json: str = "",
                   action: str = "", task_id: str = "", agent_id: str = "",
                   work_session_id: str = "", claim_id: str = "",
                   control_mode: str = "", project: str = "maxwell") -> str:
    """Validate a pending side-effectful tool before an adapter executes it.

    Returns allow/warn/deny plus remediation. Hook-capable adapters should deny locally on
    `decision=deny`; advisory adapters should surface the reason and advertise reduced
    control fidelity. Denied unsafe attempts are audited as unbound_write or unsafe_session.
    """
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        tool_input = json.loads(tool_input_json or "{}")
    except json.JSONDecodeError:
        return _dumps({"error": "tool_input_json must be valid JSON"})
    return _dumps(store.pre_tool_check(
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


@mcp.tool()
def request_runner_snapshot(runner_session_id: str, ctx: Context,
                            reason: str = "", project: str = "maxwell") -> str:
    """Request a host-side snapshot for a managed runner session."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.request_runner_control(
        runner_session_id, "snapshot", reason=reason,
        actor=auth.actor(principal), principal_id=principal["id"], project=project))


@mcp.tool()
def request_runner_kill(runner_session_id: str, ctx: Context,
                        reason: str = "", grace_seconds: float = 5.0,
                        signal: str = "TERM", project: str = "maxwell") -> str:
    """Request a host-side runner kill. The request is audited and carries a pre-kill snapshot."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.request_runner_control(
        runner_session_id, "kill", reason=reason,
        options={"grace_seconds": grace_seconds, "signal": signal or "TERM"},
        actor=auth.actor(principal), principal_id=principal["id"], project=project))


@mcp.tool()
def request_runner_health(runner_session_id: str, ctx: Context,
                          reason: str = "", project: str = "maxwell") -> str:
    """Request host-side runner health from an environment that supports it.

    Unsupported runtimes return a refused control request with reason=not_supported.
    """
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.request_runner_control(
        runner_session_id, "health", reason=reason,
        actor=auth.actor(principal), principal_id=principal["id"], project=project))


@mcp.tool()
def request_runner_logs(runner_session_id: str, ctx: Context,
                        reason: str = "", project: str = "maxwell") -> str:
    """Request host-side runner logs from an environment that supports it.

    Unsupported runtimes return a refused control request with reason=not_supported.
    """
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.request_runner_control(
        runner_session_id, "logs", reason=reason,
        actor=auth.actor(principal), principal_id=principal["id"], project=project))


@mcp.tool()
def request_runner_open(runner_session_id: str, ctx: Context,
                        reason: str = "", project: str = "maxwell") -> str:
    """Request a host-side open action when the runtime explicitly advertises runner_open.

    Unsupported runtimes return a refused control request with reason=not_supported.
    """
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.request_runner_control(
        runner_session_id, "open", reason=reason,
        actor=auth.actor(principal), principal_id=principal["id"], project=project))


@mcp.tool()
def list_runner_control_requests(project: str = "maxwell", status: str = "",
                                 host_id: str = "",
                                 runner_session_id: str = "") -> str:
    """List pending/completed runner snapshot/kill/restart/health/log/open control requests."""
    return _dumps(store.list_runner_control_requests(
        status=status, host_id=host_id, runner_session_id=runner_session_id,
        project=project))


@mcp.tool()
def claim_external_effect(effect_type: str, target: str, resource: str,
                          payload_json: str, ctx: Context,
                          project: str = "maxwell", task_id: str = "",
                          claim_id: str = "", agent_id: str = "",
                          idem_key: str = "",
                          idempotency_window_seconds: int = 0) -> str:
    """Atomically claim an external side effect before touching a provider.

    Replays return the existing effect. If the existing effect is not verified, callers
    must read back provider state before issuing it again.
    """
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        payload = json.loads(payload_json or "{}")
    except Exception:
        return _dumps({"error": "payload_json must be a JSON object string"})
    return _dumps(store.claim_external_effect(
        effect_type, target, resource, payload,
        task_id=task_id or None, claim_id=claim_id, agent_id=agent_id,
        idem_key=idem_key, idempotency_window_seconds=idempotency_window_seconds,
        actor=auth.actor(principal), principal_id=principal["id"], project=project))


@mcp.tool()
def mark_external_effect_issued(effect_key: str, ctx: Context,
                                readback_json: str = "{}",
                                project: str = "maxwell") -> str:
    """Mark an already-claimed external side effect as issued to the provider."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        readback = json.loads(readback_json or "{}")
    except Exception:
        return _dumps({"error": "readback_json must be a JSON object string"})
    return _dumps(store.mark_external_effect_issued(
        effect_key, readback=readback, actor=auth.actor(principal), project=project))


@mcp.tool()
def verify_external_effect(effect_key: str, ctx: Context,
                           readback_json: str = "{}",
                           project: str = "maxwell") -> str:
    """Confirm an external side effect only after provider readback or explicit proof."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        readback = json.loads(readback_json or "{}")
    except Exception:
        return _dumps({"error": "readback_json must be a JSON object string"})
    return _dumps(store.verify_external_effect(
        effect_key, readback=readback, actor=auth.actor(principal), project=project))


@mcp.tool()
def fail_external_effect(effect_key: str, error: str, ctx: Context,
                         readback_json: str = "{}", dead_letter: bool = False,
                         project: str = "maxwell") -> str:
    """Record a failed or dead-lettered external side effect with visible error state."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        readback = json.loads(readback_json or "{}")
    except Exception:
        return _dumps({"error": "readback_json must be a JSON object string"})
    return _dumps(store.fail_external_effect(
        effect_key, error=error, readback=readback, dead_letter=dead_letter,
        actor=auth.actor(principal), project=project))


@mcp.tool()
def list_external_effects(project: str = "maxwell", effect_type: str = "",
                          status: str = "", task_id: str = "",
                          target: str = "") -> str:
    """List external side effects by type/status/task/target."""
    return _dumps(store.list_external_effects(
        effect_type=effect_type, status=status, task_id=task_id,
        target=target, project=project))


@mcp.tool()
def list_external_ci_runs(project: str = "maxwell", task_id: str = "",
                          source_project: str = "", source_sha: str = "",
                          status: str = "") -> str:
    """List public CI mirror runs tracked by Switchboard."""
    return _dumps(store.list_external_ci_runs(
        task_id=task_id, source_project=source_project,
        source_sha=source_sha, status=status, project=project))


@mcp.tool()
def get_external_ci_run(run_id: str, project: str = "maxwell") -> str:
    """Read one public CI mirror run by Switchboard run id."""
    run = store.get_external_ci_run(run_id, project=project)
    return _dumps(run) if run else _dumps({"error": "external_ci_run not found", "run_id": run_id})


@mcp.tool()
def list_publication_evidence(project: str = "maxwell", task_id: str = "",
                              source_project: str = "", source_sha: str = "",
                              public_repo: str = "") -> str:
    """List public mirror publication evidence tracked by Switchboard."""
    return _dumps(store.list_publication_evidence(
        task_id=task_id, source_project=source_project,
        source_sha=source_sha, public_repo=public_repo, project=project))


@mcp.tool()
def merge_gate(task_id: str, ctx: Context, project: str = "maxwell",
               agent_id: str = "", claim_id: str = "", work_session_id: str = "",
               pr_url: str = "", pr_number: int = 0, repo: str = "",
               target_branch: str = "", branch: str = "", head_sha: str = "",
               required_status_contexts: str = "", status_contexts_json: str = "{}",
               github_pr_json: str = "{}", require_work_session: bool = False) -> str:
    """Evaluate safe-merge readiness before an agent runs or requests PR merge.

    This records a merge.gate activity event and returns pass/blocked findings. It does
    not merge and cannot mark Done; GitHub webhook/reconcile provenance remains the Done
    authority."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        status_contexts = json.loads(status_contexts_json or "{}")
    except Exception:
        return _dumps({"error": "status_contexts_json must be a JSON object or array string"})
    try:
        github_pr = json.loads(github_pr_json or "{}")
    except Exception:
        return _dumps({"error": "github_pr_json must be a JSON object string"})
    payload = {
        "task_id": task_id,
        "agent_id": agent_id,
        "claim_id": claim_id,
        "work_session_id": work_session_id,
        "pr_url": pr_url,
        "pr_number": pr_number or None,
        "repo": repo,
        "target_branch": target_branch,
        "branch": branch,
        "head_sha": head_sha,
        "required_status_contexts": required_status_contexts,
        "status_contexts": status_contexts,
        "github_pr": github_pr,
        "require_work_session": bool(require_work_session),
    }
    return _dumps(store.merge_gate(
        payload, actor=auth.actor(principal), principal_id=principal["id"],
        project=project))


@mcp.tool()
def record_publication_evidence(ctx: Context, source_sha: str, public_ref: str,
                                project: str = "maxwell", source_project: str = "",
                                source_repo: str = "", public_repo: str = "",
                                public_sha: str = "", public_tag: str = "",
                                script: str = "", guard_status: str = "unknown",
                                guard_json: str = "{}", artifact_url: str = "",
                                task_id: str = "", claim_id: str = "",
                                agent_id: str = "", publication_id: str = "",
                                published_at: float = 0.0) -> str:
    """Record public mirror publication evidence for a canonical source SHA."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        guard = json.loads(guard_json or "{}")
    except Exception:
        return _dumps({"error": "guard_json must be a JSON object string"})
    if not isinstance(guard, dict):
        return _dumps({"error": "guard_json must be a JSON object string"})
    payload = {
        "publication_id": publication_id,
        "source_project": source_project or project,
        "source_repo": source_repo,
        "source_sha": source_sha,
        "public_repo": public_repo,
        "public_ref": public_ref,
        "public_sha": public_sha,
        "public_tag": public_tag,
        "script": script,
        "guard_status": guard_status,
        "guard": guard,
        "artifact_url": artifact_url,
        "task_id": task_id,
        "claim_id": claim_id,
        "agent_id": agent_id,
        "principal_id": principal["id"],
    }
    if published_at:
        payload["published_at"] = published_at
    return _dumps(store.create_publication_evidence(
        payload, actor=auth.actor(principal), project=project))


@mcp.tool()
def request_external_ci_mirror_run(source_path: str, mirror_repo: str, workflow: str,
                                   ctx: Context, source_project: str = "",
                                   source_repo: str = "", source_branch: str = "",
                                   source_sha: str = "", mirror_branch: str = "",
                                   mirror_remote_url: str = "", task_id: str = "",
                                   claim_id: str = "", agent_id: str = "",
                                   status_context: str = "",
                                   workflow_inputs_json: str = "{}",
                                   poll_interval_seconds: float = 15.0,
                                   timeout_seconds: float = 1800.0,
                                   idem_key: str = "",
                                   project: str = "maxwell") -> str:
    """Mirror an exact private source SHA to a disposable public CI branch and poll Actions.

    Agents should request this from their private/source-of-truth checkout. They must not
    edit or develop in the public CI repo. Switchboard records sync/trigger/poll/test
    failures with distinct failure_class values on the external_ci_run.
    """
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        workflow_inputs = json.loads(workflow_inputs_json or "{}")
    except Exception:
        return _dumps({"error": "workflow_inputs_json must be a JSON object string"})
    payload = {
        "source_project": source_project or project,
        "source_repo": source_repo,
        "source_branch": source_branch,
        "source_sha": source_sha,
        "mirror_repo": mirror_repo,
        "mirror_branch": mirror_branch,
        "mirror_remote_url": mirror_remote_url,
        "workflow": workflow,
        "status_context": status_context,
        "workflow_inputs": workflow_inputs,
        "task_id": task_id,
        "claim_id": claim_id,
        "agent_id": agent_id,
        "principal_id": principal["id"],
        "poll_interval_seconds": poll_interval_seconds,
        "timeout_seconds": timeout_seconds,
        "idem_key": idem_key,
        "request": {
            "workflow_inputs": workflow_inputs,
            "poll_interval_seconds": poll_interval_seconds,
            "timeout_seconds": timeout_seconds,
        },
    }
    return _dumps(external_ci_mirror.request_external_ci_mirror_run(
        payload, source_path, actor=auth.actor(principal), project=project))


@mcp.tool()
def poll_external_ci_mirror_run(run_id: str, source_path: str, ctx: Context,
                                poll_interval_seconds: float = 15.0,
                                timeout_seconds: float = 1800.0,
                                project: str = "maxwell") -> str:
    """Resume polling one external CI mirror run after trigger or agent/session interruption."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(external_ci_mirror.poll_external_ci_mirror_run(
        run_id, source_path, actor=auth.actor(principal), project=project,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds))


@mcp.tool()
def claim_runner_control(host_id: str, request_id: str, ctx: Context,
                         project: str = "maxwell") -> str:
    """Agent Host claims a pending runner control request for one of its sessions."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.claim_runner_control_request(
        host_id, request_id, actor=auth.actor(principal), project=project))


@mcp.tool()
def complete_runner_control(request_id: str, ctx: Context, result_json: str = "{}",
                            snapshot_json: str = "{}", status: str = "",
                            project: str = "maxwell") -> str:
    """Agent Host completes a runner control request after snapshot/kill execution."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        result = json.loads(result_json or "{}")
        snapshot = json.loads(snapshot_json or "{}")
    except Exception:
        return _dumps({"error": "result_json and snapshot_json must be JSON object strings"})
    return _dumps(store.complete_runner_control_request(
        request_id, result=result, snapshot=snapshot, status=status,
        actor=auth.actor(principal), project=project))


@mcp.tool()
def request_wake(selector_json: str, reason: str, ctx: Context,
                 source: str = "", policy_json: str = "{}", task_id: str = "",
                 idem_key: str = "", project: str = "maxwell") -> str:
    """Create a durable wake intent for an absent runtime/session.

    selector_json includes runtime/agent_id/lane/capabilities. Example:
    {"runtime":"claude-code","agent_id":"claude-code","lane":"ADAPTER"}.
    """
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        selector = json.loads(selector_json or "{}")
        policy = json.loads(policy_json or "{}")
    except Exception:
        return _dumps({"error": "selector_json and policy_json must be valid JSON"})
    return _dumps(store.request_wake(
        selector=selector, reason=reason, source=source or auth.actor(principal),
        policy=policy, task_id=task_id or None, principal_id=principal["id"],
        actor=auth.actor(principal), idem_key=idem_key, project=project))


@mcp.tool()
def list_wake_intents(project: str = "maxwell", status: str = "", host_id: str = "",
                      runtime: str = "") -> str:
    """List durable wake intents. status can be pending|claimed|completed|failed|cancelled."""
    return _dumps(store.list_wake_intents(status=status, host_id=host_id,
                                         runtime=runtime, project=project))


@mcp.tool()
def claim_wake(host_id: str, wake_id: str, ctx: Context,
               project: str = "maxwell") -> str:
    """Atomically assign one pending wake intent to an eligible Agent Host."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.claim_wake(host_id, wake_id, actor=auth.actor(principal),
                                  project=project))


@mcp.tool()
def complete_wake(wake_id: str, ctx: Context, runner_session_id: str = "",
                  agent_id: str = "", result_json: str = "{}",
                  project: str = "maxwell") -> str:
    """Record wake success/failure after the host daemon starts or fails to start a runtime."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        result = json.loads(result_json or "{}")
    except Exception:
        return _dumps({"error": "result_json must be a JSON object string"})
    return _dumps(store.complete_wake(
        wake_id, runner_session_id=runner_session_id, agent_id=agent_id,
        result=result, actor=auth.actor(principal), project=project))


@mcp.tool()
def cancel_wake(wake_id: str, ctx: Context, reason: str = "cancelled",
                project: str = "maxwell") -> str:
    """Cancel a pending or claimed wake intent."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.cancel_wake(wake_id, reason=reason,
                                   actor=auth.actor(principal), project=project))


@mcp.tool()
def claim_resource(agent_id: str, resource_type: str, names: str, ctx: Context,
                   task_id: str = "", ttl_seconds: int = 1800,
                   idem_key: str = "", project: str = "maxwell") -> str:
    """Generic IXP resource claim. resource_type can be file, port, build_dir, worktree,
    binary, branch, task, etc. names is comma/newline-separated."""
    principal = _require_write(ctx, project, ("write:ixp",))
    name_list = [n.strip() for n in names.replace("\n", ",").split(",") if n.strip()]
    return _dumps(store.claim_resources(
        agent_id=agent_id, resource_type=resource_type, names=name_list,
        task_id=task_id or None, ttl_seconds=ttl_seconds, principal_id=principal["id"],
        actor=auth.actor(principal), idem_key=idem_key, project=project))


@mcp.tool()
def check_resource(resource_type: str, names: str, project: str = "maxwell") -> str:
    """Check whether generic IXP resources are held by active leases."""
    name_list = [n.strip() for n in names.replace("\n", ",").split(",") if n.strip()]
    return _dumps(store.check_resources(resource_type, name_list, project=project))


@mcp.tool()
def release_resource(lease_id: str, ctx: Context, project: str = "maxwell") -> str:
    """Release a generic IXP resource lease."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.release_resource_lease(
        lease_id, actor=auth.actor(principal), project=project))


@mcp.tool()
def list_active_resource_leases(project: str = "maxwell") -> str:
    """All active generic IXP resource leases."""
    return _dumps(store.list_active_resource_leases(project=project))


@mcp.tool()
def claim_next(agent_id: str, ctx: Context, lanes: str = "", capabilities: str = "",
               max_risk: str = "", max_budget_usd: float = 0.0,
               ttl_seconds: int = 1800, idem_key: str = "",
               override_identity_risk: bool = False,
               work_session_id: str = "", work_session_json: str = "",
               session_policy_profile: str = "",
               require_work_session: bool = False,
               project: str = "maxwell", deliverable_id: str = "",
               board_id: str = "", mission_id: str = "",
               milestone_id: str = "") -> str:
    """Atomically claim the next unblocked task for this agent. This is the first +TXP
    scheduler primitive: dependency-aware, idempotent, constraint-scored, and returns
    dispatch_reason plus budget/model guidance.

    When deliverable_id or board_id/mission_id is set, only tasks linked to that mission
    deliverable are eligible. milestone_id optionally narrows to one milestone."""
    principal = _require_write(ctx, project, ("write:ixp",))
    lane_list = [x.strip().upper() for x in lanes.replace("\n", ",").split(",") if x.strip()]
    cap_list = [x.strip() for x in capabilities.replace("\n", ",").split(",") if x.strip()]
    work_session = {}
    if work_session_json.strip():
        try:
            work_session = json.loads(work_session_json)
        except json.JSONDecodeError:
            return _dumps({"error": "work_session_json must be valid JSON"})
    return _dumps(store.claim_next(
        agent_id=agent_id, lanes=lane_list, capabilities=cap_list,
        max_risk=max_risk, max_budget_usd=max_budget_usd or None,
        principal_id=principal["id"], actor=auth.actor(principal),
        ttl_seconds=ttl_seconds, idem_key=idem_key,
        override_identity_risk=override_identity_risk,
        work_session_id=work_session_id, work_session=work_session,
        session_policy_profile=session_policy_profile,
        require_work_session=require_work_session,
        project=project, deliverable_id=deliverable_id,
        board_id=board_id, mission_id=mission_id, milestone_id=milestone_id))


@mcp.tool()
def claim_task(task_id: str, agent_id: str, ctx: Context,
               ttl_seconds: int = 1800, idem_key: str = "",
               override_identity_risk: bool = False,
               work_session_id: str = "", work_session_json: str = "",
               session_policy_profile: str = "",
               require_work_session: bool = False,
               project: str = "maxwell") -> str:
    """Atomically claim one exact ready, unblocked task.

    Use this when a human/operator has selected a specific task. Unlike claim_next,
    this never substitutes a different scheduler-preferred task.
    """
    principal = _require_write(ctx, project, ("write:ixp",))
    work_session = {}
    if work_session_json.strip():
        try:
            work_session = json.loads(work_session_json)
        except json.JSONDecodeError:
            return _dumps({"error": "work_session_json must be valid JSON"})
    return _dumps(store.claim_task(
        task_id=task_id, agent_id=agent_id,
        principal_id=principal["id"], actor=auth.actor(principal),
        ttl_seconds=ttl_seconds, idem_key=idem_key,
        override_identity_risk=override_identity_risk,
        work_session_id=work_session_id, work_session=work_session,
        session_policy_profile=session_policy_profile,
        require_work_session=require_work_session,
        project=project))


@mcp.tool()
def complete_claim(claim_id: str, ctx: Context, evidence: str = "", final_status: str = "",
                   project: str = "maxwell", agent_id: str = "",
                   system_actor: str = "", system_reason: str = "",
                   mission_project: str = "") -> str:
    """Mark a task claim completed, release its task lease, and record completion evidence.

    This moves the task to In Review. Done is reserved for GitHub/default-branch merge
    provenance; if final_status='Done' is passed, Switchboard records the request but keeps
    the task In Review until merged_sha/default-branch SHA is stamped. evidence should be
    a JSON object string with branch, head_sha, pr_url/pr_number, or a verification note.
    Optional deliverable_id, milestone_id, and mission_project in evidence refresh mission
    progress without counting agent completion as Done."""
    principal = _require_write(ctx, project, ("write:ixp",))
    target = store.claim_binding_target(claim_id, project=project)
    binding = _resolve_write_actor(
        principal,
        project=project,
        task_id=target.get("task_id") or "",
        agent_id=agent_id or target.get("agent_id") or "",
        system_actor=system_actor,
        system_reason=system_reason,
    )
    if not binding.get("ok"):
        return _dumps(binding)
    _write_binding_comment(target.get("task_id") or "", binding, project)
    return _dumps(store.complete_claim(claim_id, evidence=evidence, final_status=final_status,
                                      actor=binding["actor"], project=project,
                                      mission_project=mission_project))


@mcp.tool()
def verify_offline_completion(task_id: str, ctx: Context, evidence: str = "",
                              artifact_url: str = "", evidence_hash: str = "",
                              verifier: str = "", reviewed_at: float = 0,
                              project: str = "maxwell") -> str:
    """Mark an In Review non-PR/offline task Done with verifier-stamped evidence.

    Agents still use complete_claim(...) to move work to In Review. This tool is the
    privileged verifier/operator path for work that has no code PR: it records
    provenance_type=offline_evidence, evidence/artifact/hash/verifier/reviewed_at, and
    then marks Done. It fails closed unless the task is already In Review and evidence
    is supplied.
    """
    principal = _require_write(ctx, project)
    return _dumps(store.mark_task_offline_done(
        task_id, evidence=evidence, artifact_url=artifact_url,
        evidence_hash=evidence_hash, verifier=verifier or auth.actor(principal),
        reviewed_at=reviewed_at or None, actor=auth.actor(principal), project=project))


@mcp.tool()
def abandon_claim(claim_id: str, reason: str, ctx: Context,
                  project: str = "maxwell") -> str:
    """Abandon a task claim, release its task lease, and return the task to the ready queue."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.abandon_claim(claim_id, reason=reason,
                                     actor=auth.actor(principal), project=project))


@mcp.tool()
def revoke_claim(claim_id: str, reason: str, ctx: Context,
                 project: str = "maxwell", reassign_to: str = "",
                 sort_order: int = 0, partial_evidence: str = "",
                 notify: bool = True, ack_deadline_minutes: float = 5) -> str:
    """Operator override for a live claim.

    Revokes the active task claim, releases its task lease, requeues the task,
    optionally redirects/reprioritizes it, preserves partial evidence, and sends
    the displaced agent an ack-required claim_revoked message.
    """
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.revoke_claim(
        claim_id,
        reason=reason,
        reassign_to=reassign_to,
        sort_order=sort_order if sort_order > 0 else None,
        partial_evidence=partial_evidence,
        notify=notify,
        ack_deadline_minutes=ack_deadline_minutes,
        actor=auth.actor(principal),
        project=project,
    ))


@mcp.tool()
def report_usage(ctx: Context, source: str = "agent_report", confidence: str = "reported",
                 task_id: str = "", claim_id: str = "", agent_id: str = "",
                 outcome_id: str = "",
                 runtime: str = "", provider: str = "", model: str = "",
                 prompt_tokens: int = 0, completion_tokens: int = 0,
                 total_tokens: int = 0, cost_usd: float = 0.0,
                 call_site: str = "coding", request_id: str = "",
                 metadata_json: str = "{}", project: str = "maxwell") -> str:
    """Report usage/cost into Tally. Gateway-measured calls should use source='gateway';
    external coding agents should use source='agent_report' and honest confidence."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        metadata = json.loads(metadata_json or "{}")
    except Exception:
        return _dumps({"error": "metadata_json must be a JSON object string"})
    return _dumps(store.report_usage(
        source=source, confidence=confidence, task_id=task_id or None,
        claim_id=claim_id or None, outcome_id=outcome_id or None,
        agent_id=agent_id or None,
        principal_id=principal["id"], runtime=runtime, call_site=call_site,
        provider=provider, model=model, prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens or None, cost_usd=cost_usd,
        metadata=metadata, request_id=request_id or None, project=project))


@mcp.tool()
def record_outcome(ctx: Context, outcome_type: str, title: str,
                   task_id: str = "", claim_id: str = "", epic_id: str = "",
                   status: str = "proposed", verifier: str = "",
                   verification: str = "", evidence_json: str = "{}",
                   value_json: str = "{}", project: str = "maxwell") -> str:
    """Record an OXP outcome. Proposed outcomes are pending value; only verified outcomes
    count in cost-per-outcome denominators."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        evidence = json.loads(evidence_json or "{}")
        value = json.loads(value_json or "{}")
    except Exception:
        return _dumps({"error": "evidence_json and value_json must be JSON object strings"})
    return _dumps(store.record_outcome(
        outcome_type=outcome_type, title=title, task_id=task_id or None,
        claim_id=claim_id or None, epic_id=epic_id or None, status=status,
        verifier=verifier, verification=verification, evidence=evidence,
        value=value, actor=auth.actor(principal), project=project))


@mcp.tool()
def verify_outcome(outcome_id: str, ctx: Context, verifier: str = "",
                   verification: str = "", evidence_json: str = "{}",
                   project: str = "maxwell") -> str:
    """Mark an outcome verified so it enters Tally's denominator."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        evidence = json.loads(evidence_json or "{}")
    except Exception:
        return _dumps({"error": "evidence_json must be a JSON object string"})
    return _dumps(store.verify_outcome(
        outcome_id, verifier=verifier or auth.actor(principal),
        verification=verification, evidence=evidence,
        actor=auth.actor(principal), project=project))


@mcp.tool()
def reject_outcome(outcome_id: str, reason: str, ctx: Context,
                   verifier: str = "", project: str = "maxwell") -> str:
    """Reject a proposed outcome. Rejected outcomes remain auditable but never count."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.reject_outcome(
        outcome_id, verifier=verifier or auth.actor(principal), reason=reason,
        actor=auth.actor(principal), project=project))


@mcp.tool()
def create_kpi(ctx: Context, name: str, unit: str, direction: str,
               owner: str = "", baseline_value: float = 0.0,
               current_value: float = 0.0, target_value: float = 0.0,
               period: str = "", project: str = "maxwell") -> str:
    """Create a KPI that outcomes can move. direction: increase|decrease|maintain."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.create_kpi(
        name=name, unit=unit, direction=direction, owner=owner,
        baseline_value=baseline_value if baseline_value else None,
        current_value=current_value if current_value else None,
        target_value=target_value if target_value else None,
        period=period, actor=auth.actor(principal), project=project))


@mcp.tool()
def update_kpi_value(kpi_id: str, current_value: float, ctx: Context,
                     evidence_json: str = "{}", project: str = "maxwell") -> str:
    """Update the current value for a KPI."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        evidence = json.loads(evidence_json or "{}")
    except Exception:
        return _dumps({"error": "evidence_json must be a JSON object string"})
    return _dumps(store.update_kpi_value(
        kpi_id, current_value=current_value, evidence=evidence,
        actor=auth.actor(principal), project=project))


@mcp.tool()
def link_outcome_to_kpi(ctx: Context, outcome_id: str, kpi_id: str,
                        contribution: float = 0.0, contribution_unit: str = "",
                        confidence: str = "directional", rationale: str = "",
                        project: str = "maxwell") -> str:
    """Link a verified or proposed outcome to a KPI with measured|estimated|directional
    confidence. Only verified outcome links count in cost-per-KPI movement."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.link_outcome_to_kpi(
        outcome_id=outcome_id, kpi_id=kpi_id,
        contribution=contribution if contribution else None,
        contribution_unit=contribution_unit, confidence=confidence,
        rationale=rationale, actor=auth.actor(principal), project=project))


@mcp.tool()
def get_task_tally(task_id: str, project: str = "maxwell") -> str:
    """Tally rollup for one task: spend by source, total tokens/cost, and outcome denominator."""
    return _dumps(store.task_tally(task_id, project=project))


@mcp.tool()
def get_kpi_tally(kpi_id: str, project: str = "maxwell") -> str:
    """KPI rollup: linked outcomes, spend, verified contribution, and cost per movement unit."""
    return _dumps(store.kpi_tally(kpi_id, project=project))


@mcp.tool()
def get_deliverable_tally(deliverable_id: str, project: str = "maxwell") -> str:
    """Deliverable/mission economics: spend, verified outcomes, KPI movement, and proven vs in-review split."""
    return _dumps(store.deliverable_tally(deliverable_id, project=project))


@mcp.tool()
def run_background_job(ctx: Context, job_name: str, project: str = "maxwell",
                       run_id: str = "", resume: bool = True,
                       params_json: str = "{}") -> str:
    """Run or resume a checkpointed background job (replay, audit export, receipts, reconcile)."""
    project = _resolve_project_input(project)
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        params = json.loads(params_json or "{}")
    except json.JSONDecodeError as exc:
        return _dumps({"error": "invalid params_json", "detail": str(exc)})
    if not isinstance(params, dict):
        return _dumps({"error": "params_json must decode to an object"})
    try:
        import background_jobs
        return _dumps(store.run_background_job(
            project=project,
            job_name=job_name,
            run_id=run_id,
            resume=resume,
            params=params,
            actor=auth.actor(principal),
        ))
    except background_jobs.JobBoundaryError as exc:
        return _dumps({"error": "job_boundary", "detail": str(exc)})


@mcp.tool()
def get_background_job_run(ctx: Context, run_id: str, project: str = "maxwell") -> str:
    """Fetch one persisted background job run manifest by run_id."""
    project = _resolve_project_input(project)
    return _dumps(store.get_background_job_run(project=project, run_id=run_id))


@mcp.tool()
def list_background_job_runs(ctx: Context, project: str = "maxwell",
                             job_name: str = "", limit: int = 20) -> str:
    """List recent checkpointed background job runs."""
    project = _resolve_project_input(project)
    return _dumps(store.list_background_job_runs(
        project=project, job_name=job_name, limit=limit))


@mcp.tool()
def reconcile(project: str = "maxwell") -> str:
    """Run the local board/git-provenance drift report. This first pass catches board-internal
    contradictions such as Done without merged_sha or In Review without PR/branch evidence."""
    return _dumps(store.reconcile(project=project))


@mcp.tool()
def reconcile_alerts(ctx: Context, project: str = "maxwell",
                     alert_to: str = "switchboard/operator",
                     min_severity: str = "medium") -> str:
    """Run the scheduled reconcile alert path now: reconcile, filter actionable findings,
    dedupe inside the configured window, and emit a directed agent message when needed."""
    _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.run_reconcile_alerts(
        project=project, alert_to=alert_to, min_severity=min_severity))


# ---- directed agent IM (IXP write-authenticated) -----------------------
@mcp.tool()
def send_agent_message(from_agent: str, to_agent: str, message: str,
                       ctx: Context, project: str = "maxwell", task_id: str = "",
                       requires_ack: bool = False,
                       ack_deadline_minutes: int = 0,
                       ack_timeout_seconds: float = 0,
                       on_ack_timeout: str = "notify_sender",
                       signal: str = "", priority: int = 0,
                       idem_key: str = "") -> str:
    """Send a directed message to another agent session. Unlike add_comment (bulletin
    board, fire-and-forget), this has an ack/read-receipt so the sender can confirm
    the message landed before acting on the assumption it was received.

    from_agent / to_agent: stable agent-session identifiers (e.g. 'claude/ENGINE-11').
    task_id: the task this message is about (optional).
    requires_ack: if true, the receiving agent should call ack_message to confirm receipt.
    ack_deadline_minutes: how long the sender will wait for an ack (0 = no deadline).
    ack_timeout_seconds: equivalent seconds-based alias; used when minutes is 0.

    Returns the message record including its id. Pass the id to get_message_status to
    check whether the recipient has acked."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.send_agent_message(
        from_agent, to_agent, message,
        task_id=task_id or None,
        requires_ack=requires_ack,
        ack_deadline_minutes=ack_deadline_minutes or None,
        ack_timeout_seconds=ack_timeout_seconds or None,
        on_ack_timeout=on_ack_timeout,
        signal=signal or None,
        priority=priority,
        principal_id=principal["id"],
        idem_key=idem_key,
        project=project,
    ))


@mcp.tool()
def ack_message(message_id: int, ctx: Context, project: str = "maxwell", response: str = "") -> str:
    """Acknowledge a directed message. Call this when you have received and understood a
    message that has requires_ack=true. response is optional — include it to give the
    sender a one-line confirmation (e.g. 'seen — will rebase before merging').
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.ack_message(message_id, response=response,
                                    actor=auth.actor(principal), project=project))


@mcp.tool()
def list_unacked_messages(to_agent: str, project: str = "maxwell") -> str:
    """Your incoming message inbox — messages directed to you that have not been acked.
    Call at session start and after completing a task to check for coordination messages
    from other agents. to_agent: your agent-session id (e.g. 'claude/CHART-8').
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    return _dumps(store.list_unacked_messages(to_agent, project=project))


@mcp.tool()
def get_message_status(message_id: int, project: str = "maxwell") -> str:
    """Check whether a message you sent has been acked. Returns the full message record
    including acked_at and ack_response if the recipient has responded.
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    r = store.get_message_status(message_id, project=project)
    return _dumps(r) if r else "message not found"


@mcp.tool()
def list_pending_acks(project: str = "maxwell", agent_id: str = "") -> str:
    """List unacked requires_ack messages plus durable monitor state. agent_id filters to
    messages either sent by or addressed to that agent. This is the protocol-native way to see
    what is still waiting on another agent."""
    return _dumps(store.list_pending_acks(agent_id=agent_id, project=project))


@mcp.tool()
def list_monitors(project: str = "maxwell", status: str = "", kind: str = "",
                  task_id: str = "") -> str:
    """List durable Switchboard monitors. status can be pending|fired|resolved|cancelled;
    kind can be ack_deadline. task_id narrows the result to one task."""
    return _dumps(store.list_coordination_monitors(
        status=status, kind=kind, task_id=task_id, project=project))


@mcp.tool()
def sweep_monitors(ctx: Context, project: str = "maxwell") -> str:
    """Evaluate durable monitors now: resolve acked messages and fire timed-out ack monitors.
    This is also what the Switchboard-owned systemd timer calls."""
    _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.sweep_coordination_monitors(project=project))


@mcp.tool()
def resolve_monitor(monitor_id: str, ctx: Context, project: str = "maxwell",
                    reason: str = "manual") -> str:
    """Manually resolve a durable monitor after an operator handles it outside the normal path."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.resolve_monitor(monitor_id, reason=reason,
                                       actor=auth.actor(principal), project=project))


@mcp.tool()
def cancel_monitor(monitor_id: str, ctx: Context, project: str = "maxwell",
                   reason: str = "cancelled") -> str:
    """Cancel a durable monitor that should no longer fire."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.cancel_monitor(monitor_id, reason=reason,
                                      actor=auth.actor(principal), project=project))


# ---- blocking dep requests (§2.2 — requires_ack IM with dep semantics) ------
@mcp.tool()
def request_unblock(requesting_agent: str, owner_agent: str,
                    blocking_task_id: str, blocked_task_id: str,
                    message: str, ctx: Context, project: str = "maxwell",
                    ack_deadline_minutes: int = 60) -> str:
    """Ask the agent working on a blocking task to unblock your work. Use this when
    your task has a direct dependency that hasn't been resolved and you need the
    owning agent to act — more urgent and structured than add_comment.

    How it works:
    - Sends a directed, ack-required message to owner_agent.
    - Records the request as 'dep_request' activity on BOTH tasks for the board trail.
    - Returns {request_id, ...}. Poll get_message_status(request_id) to see when the
      owning agent has acked (i.e., picked up and acknowledged the request).

    Fields:
      requesting_agent: your agent-session id ('claude/ROUTE-3')
      owner_agent:      the agent working on the blocker ('claude/ENGINE-11')
      blocking_task_id: the task that is blocking you ('ENGINE-11')
      blocked_task_id:  your task ('ROUTE-3')
      message:          what you need / why it's urgent (1-3 sentences)
      ack_deadline_minutes: how long you'll wait (default 60)
    project: 'maxwell' (default), 'helm', or 'switchboard'."""
    _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.request_unblock(
        requesting_agent=requesting_agent, blocking_task_id=blocking_task_id,
        blocked_task_id=blocked_task_id, message=message,
        owner_agent=owner_agent, ack_deadline_minutes=ack_deadline_minutes,
        project=project,
    ))


@mcp.tool()
def list_unblock_requests(owner_agent: str, project: str = "maxwell") -> str:
    """Check your queue of unacked blocking dep requests — tasks whose owners are
    waiting on you. Call at session start alongside list_unacked_messages.
    Returns the same structure as list_unacked_messages but filtered to DEP REQUEST
    messages. Ack each with ack_message(request_id, response='unblocked') when done.
    project: 'maxwell' (default), 'helm', or 'switchboard'."""
    return _dumps(store.list_unblock_requests(owner_agent, project=project))


# ---- agent state (open — lightweight working-state scratchpad per agent) ----
@mcp.tool()
def set_agent_state(task_id: str, agent_id: str, state: str,
                    ctx: Context, project: str = "maxwell") -> str:
    """Write your working state for a task — a small JSON object (max ~500 chars)
    capturing what you're doing, where you are, and what you plan next. Stored inside
    the task and visible to all agents via get_agent_state. Good keys to include:
      "files_open": which files you have staged or modified
      "next_step": what you're about to do next
      "blocked_on": what you're waiting for (or null)
      "progress": e.g. "3/7 tests passing"
    state: JSON-string object. agent_id: your stable session id (e.g. 'claude/ENGINE-11').
    project: 'maxwell' (default), 'helm', or 'switchboard'."""
    try:
        state_obj = json.loads(state)
    except Exception:
        return _dumps({"error": "state must be a valid JSON object string"})
    _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.set_agent_state(task_id, agent_id, state_obj, project=project))


@mcp.tool()
def get_agent_state(task_id: str, project: str = "maxwell") -> str:
    """Read the working-state blobs for all agents currently on a task.
    Returns {agent_id: {state fields}, ...}. Call this before starting work on a
    task to see if another agent is already active, what files it has open, and what
    it plans next — complements list_unacked_messages for live coordination.
    project: 'maxwell' (default), 'helm', or 'switchboard'."""
    return _dumps(store.get_agent_state(task_id, project=project))


# ---- decisions log (open — append-only ADR-lite for multi-agent alignment) --
@mcp.tool()
def record_decision(title: str, context: str, decision: str, rationale: str,
                    author: str, ctx: Context, project: str = "maxwell",
                    task_id: str = "", supersedes: int = 0) -> str:
    """Append an immutable architectural decision record so all agents share settled
    conclusions without re-litigating them. Use this when you've just chosen an
    approach — especially a non-obvious one that another agent might reverse.
    Examples: choosing a library, fixing an interface contract, deciding NOT to do X.

    Fields:
      title:     short (≤80 chars) — 'Use advisory leases, not hard file locks'
      context:   why the decision was needed (1-3 sentences)
      decision:  exactly what was decided (1-3 sentences, present tense)
      rationale: why this option was chosen over the alternatives
      author:    your agent-session id (e.g. 'claude/ENGINE-11')
      task_id:   related task (optional)
      supersedes: id of an earlier decision this replaces (optional, set to 0 if none)

    Decisions are append-only. To reverse one, record a new decision with the old id
    in 'supersedes' — the old record is marked 'superseded' automatically.
    project: 'maxwell' (default), 'helm', or 'switchboard'."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.record_decision(
        task_id=task_id or None, author=author or auth.actor(principal), title=title,
        context=context, decision=decision, rationale=rationale,
        supersedes=supersedes or None, project=project,
    ))


@mcp.tool()
def list_decisions(project: str = "maxwell", task_id: str = "",
                   status: str = "") -> str:
    """List architectural decisions recorded by any agent.
    Filter by task_id (decisions about that task) and/or status ('accepted',
    'superseded', 'proposed'). Returns newest-first.
    Check this at session start to know what's already been decided before
    choosing an approach. project: 'maxwell' (default), 'helm', or 'switchboard'."""
    return _dumps(store.list_decisions(task_id=task_id or None,
                                      status=status, project=project))


@mcp.tool()
def get_decision(decision_id: int, project: str = "maxwell") -> str:
    """Fetch a single decision record by id. Use when list_decisions refers to a
    decision you want to read in full (context + rationale).
    project: 'maxwell' (default), 'helm', or 'switchboard'."""
    r = store.get_decision(decision_id, project=project)
    return _dumps(r) if r else "decision not found"


# ---- task write tools (Switchboard bearer-principal authenticated) -------
@mcp.tool()
def create_project(name: str, ctx: Context, project_id: str = "", label: str = "",
                   pretitle: str = "", github_repo: str = "",
                   purpose: str = "", boundary: str = "",
                   org_id: str = "", visibility: str = "private") -> str:
    """Create a new isolated project board and make it routable by all board tools.

    Authenticates against project='switchboard' with write:projects (contributors and up).
    `name` is the human name; `project_id` is optional and defaults to a lowercase slug, e.g.
    name='Vulkan' creates project='vulkan'. `github_repo` is optional owner/repo provenance
    config, e.g. github_repo='StevenRidder/Helm'. `visibility` is 'private' (default — only
    the creator, invitees, and org admins see it) or 'org' (all org members). Returns the
    created/existing project record.
    """
    principal = _require_write(ctx, "switchboard", ("write:projects",))
    result = store.create_project(name=name, project_id=project_id, label=label,
                                  pretitle=pretitle, github_repo=github_repo,
                                  owner_principal_id=principal["id"],
                                  org_id=org_id or store.DEFAULT_ORG_ID,
                                  purpose=purpose, boundary=boundary,
                                  visibility=(visibility or "private").strip().lower(),
                                  actor=auth.actor(principal))
    return _dumps(result)


@mcp.tool()
def set_project_github_repo(repo: str, ctx: Context, project: str = "maxwell") -> str:
    """Set the GitHub owner/repo used by reconcile to verify PR merge provenance for a board.

    Use this when a project board maps to a different repository than Switchboard itself, e.g.
    project='helm' -> repo='StevenRidder/Helm'. Requires system write scope because it changes
    the board's trust boundary for Done stamping.
    """
    principal = _require_write(ctx, "switchboard", ("write:system",))
    result = store.set_project_github_repo(repo=repo, project=project)
    if not result.get("error"):
        store.append_activity("project.github_repo_configured", auth.actor(principal),
                              {"project": project, "github_repo": repo},
                              task_id=None, project=project)
    return _dumps(result)


@mcp.tool()
def set_project_repo_topology(ctx: Context, project: str = "maxwell",
                              canonical_repo: str = "", public_ci_repo: str = "",
                              public_repo: str = "", release_repo: str = "",
                              topology_type: str = "",
                              canonical_default_branch: str = "",
                              public_ci_required_status_contexts: str = "",
                              public_ci_sync_scripts: str = "",
                              public_publish_scripts: str = "",
                              release_publish_scripts: str = "",
                              ci_repo: str = "", ci_required_status_contexts: str = "",
                              ci_sync_scripts: str = "") -> str:
    """Configure first-class repository roles for a project.

    canonical_repo is the only code-truth / Done authority. public_ci_repo is a
    shared public CI sandbox for verification evidence only. public_repo and
    release_repo are publication/release evidence roles only. ci_* arguments are
    accepted as aliases for public_ci_* during migration.
    """
    principal = _require_write(ctx, "switchboard", ("write:system",))
    result = store.set_project_repo_topology(
        project=project,
        canonical_repo=canonical_repo,
        public_ci_repo=public_ci_repo,
        public_repo=public_repo,
        release_repo=release_repo,
        topology_type=topology_type,
        canonical_default_branch=canonical_default_branch,
        public_ci_required_status_contexts=public_ci_required_status_contexts,
        public_ci_sync_scripts=public_ci_sync_scripts,
        public_publish_scripts=public_publish_scripts,
        release_publish_scripts=release_publish_scripts,
        ci_repo=ci_repo,
        ci_required_status_contexts=ci_required_status_contexts,
        ci_sync_scripts=ci_sync_scripts,
    )
    if not result.get("error"):
        store.append_activity("project.repo_topology_configured", auth.actor(principal),
                              {"project": project, "repo_topology": result.get("repo_topology")},
                              task_id=None, project=project)
    return _dumps(result)


@mcp.tool()
def create_project_board(title: str, ctx: Context, project: str = "maxwell",
                         board_id: str = "", mission_id: str = "",
                         kind: str = "mission", status: str = "active",
                         purpose: str = "", end_state: str = "",
                         description: str = "", owner_org: str = "",
                         owner_person_or_role: str = "",
                         metadata_json: str = "") -> str:
    """Create/update a first-class Board/Mission child under one Project.

    Project remains the repo/trust/policy/access/CI/model/budget/Done boundary.
    Boards/Missions are live outcome cockpits under that Project. Unknown projects fail closed.
    """
    principal = _require_write(ctx, project, ("write:tasks",))
    result = store.create_project_board({
        "id": board_id or mission_id,
        "title": title,
        "kind": kind,
        "status": status,
        "purpose": purpose,
        "end_state": end_state,
        "description": description,
        "owner_org": owner_org,
        "owner_person_or_role": owner_person_or_role,
        "metadata": metadata_json,
    }, actor=auth.actor(principal), project=project)
    return _dumps(result)


@mcp.tool()
def get_project_board(board_id: str, project: str = "maxwell") -> str:
    """Fetch one Board/Mission child by id from one Project."""
    if not store.has_project(project):
        return _dumps({"error": f"unknown project: {project}", "project": project})
    result = store.get_project_board(board_id, project=project)
    return _dumps(result or {"error": "unknown board", "board_id": board_id, "project": project})


@mcp.tool()
def list_project_boards(project: str = "maxwell", kind: str = "",
                        status: str = "") -> str:
    """List Board/Mission children under one Project, optionally filtered by kind/status."""
    if not store.has_project(project):
        return _dumps({"error": f"unknown project: {project}", "project": project})
    return _dumps({"project": project, "boards": store.list_project_boards(
        project=project, kind=kind, status=status)})


@mcp.tool()
def create_deliverable(title: str, ctx: Context, project: str = "maxwell",
                       deliverable_id: str = "", board_id: str = "",
                       mission_id: str = "", status: str = "proposed",
                       owner_org: str = "", owner_person_or_role: str = "",
                       end_state: str = "", why_it_matters: str = "",
                       confidence: str = "",
                       acceptance_criteria: str = "",
                       policy_constraints_json: str = "",
                       proof_requirements_json: str = "",
                       kpi_links: str = "", metadata_json: str = "") -> str:
    """Create/update a deliverable under one Project and optional Board/Mission.

    If board_id/mission_id is supplied it must already exist in the owning project.
    When PM_ENFORCE_DELIVERABLE_INTAKE is on, moving a deliverable into status=in_progress
    requires end_state + acceptance_criteria + a well-formed proof_requirements
    (schema switchboard.deliverable_proof_requirements.v1); see docs/DELIVERABLE-CLOSURE-GATE.md.
    """
    principal = _require_write(ctx, project, ("write:tasks",))
    result = store.create_deliverable({
        "id": deliverable_id,
        "board_id": board_id or mission_id,
        "title": title,
        "status": status,
        "owner_org": owner_org,
        "owner_person_or_role": owner_person_or_role,
        "end_state": end_state,
        "why_it_matters": why_it_matters,
        "confidence": confidence,
        "acceptance_criteria": acceptance_criteria,
        "policy_constraints": policy_constraints_json,
        "proof_requirements": proof_requirements_json,
        "kpi_links": kpi_links,
        "metadata": metadata_json,
    }, actor=auth.actor(principal), project=project)
    return _dumps(result)


@mcp.tool()
def get_deliverable(deliverable_id: str, project: str = "maxwell") -> str:
    """Fetch one deliverable with milestones, cross-project task links, progress, and board context."""
    if not store.has_project(project):
        return _dumps({"error": f"unknown project: {project}", "project": project})
    result = store.get_deliverable(deliverable_id, project=project)
    return _dumps(result or {"error": "unknown deliverable",
                             "deliverable_id": deliverable_id, "project": project})


@mcp.tool()
def list_deliverables(project: str = "maxwell", board_id: str = "") -> str:
    """List slim deliverable rows owned by one Project, optionally scoped to a Board/Mission id.
    Rows include metadata, milestones, raw task-link ids, and truthful progress counts, but omit
    linked-task snapshots. Call get_deliverable for one full deliverable when task detail is needed."""
    if not store.has_project(project):
        return _dumps({"error": f"unknown project: {project}", "project": project})
    return _dumps({"project": project, "board_id": board_id or None,
                   "deliverables": store.list_deliverables(project=project,
                                                            board_id=board_id,
                                                            include_task_snapshots=False)})


@mcp.tool()
def add_deliverable_milestone(deliverable_id: str, title: str, ctx: Context,
                              project: str = "maxwell", milestone_id: str = "",
                              description: str = "", status: str = "not_started",
                              sort_order: str = "",
                              acceptance_criteria: str = "",
                              proof_requirements_json: str = "") -> str:
    """Create/update a milestone inside a deliverable."""
    principal = _require_write(ctx, project, ("write:tasks",))
    result = store.add_deliverable_milestone(deliverable_id, {
        "id": milestone_id,
        "title": title,
        "description": description,
        "status": status,
        "sort_order": sort_order,
        "acceptance_criteria": acceptance_criteria,
        "proof_requirements": proof_requirements_json,
    }, actor=auth.actor(principal), project=project)
    return _dumps(result)


@mcp.tool()
def link_task_to_deliverable(deliverable_id: str, task_project: str, task_id: str,
                             ctx: Context, project: str = "maxwell",
                             milestone_id: str = "", board_id: str = "",
                             mission_id: str = "", role: str = "",
                             blocks_deliverable: bool = False,
                             proof_required_json: str = "",
                             metadata_json: str = "",
                             include_task_snapshots: bool = False) -> str:
    """Link an existing task from an explicit project into a deliverable/mission rollup.

    The target task is validated in task_project. The operation does not move or mutate it.

    role controls the mission dependency map: 'contributes' / 'implementation' /
    'acceptance' are execution flow (drawn in the DAG); 'foundation' (shipped
    groundwork) and 'parked' (frozen tracks) are context — listed under the map,
    drawn only if a flow task depends_on them. Leave empty for auto: a task
    already Done at link time becomes 'foundation', anything else 'contributes'.

    By default this write returns a compact link acknowledgement and progress count.
    Set include_task_snapshots=true only when the full decorated deliverable is needed;
    get_deliverable remains the normal full-read tool.
    """
    principal = _require_write(ctx, project, ("write:tasks",))
    result = store.link_task_to_deliverable(
        deliverable_id, task_project, task_id, milestone_id=milestone_id,
        data={
            "board_id": board_id or mission_id,
            "role": role,
            "blocks_deliverable": blocks_deliverable,
            "proof_required": proof_required_json,
            "metadata": metadata_json,
        },
        actor=auth.actor(principal),
        project=project,
        include_task_snapshots=include_task_snapshots,
    )
    return _dumps(result)


@mcp.tool()
def link_tasks_to_deliverable(deliverable_id: str, links: list[dict], ctx: Context,
                              project: str = "maxwell") -> str:
    """Link many explicitly routed tasks to one deliverable in one write transaction.

    Each links item requires task_project and task_id. Optional fields match the
    single-link tool: milestone_id, board_id/mission_id, role, blocks_deliverable,
    proof_required/proof_required_json, and metadata/metadata_json.
    """
    principal = _require_write(ctx, project, ("write:tasks",))
    result = store.link_tasks_to_deliverable(
        deliverable_id, links, actor=auth.actor(principal), project=project)
    return _dumps(result)


@mcp.tool()
def create_board(title: str, ctx: Context, project: str = "maxwell",
                 board_id: str = "", kind: str = "board", status: str = "active",
                 purpose: str = "", end_state: str = "", description: str = "",
                 owner_org: str = "", owner_person_or_role: str = "",
                 metadata_json: str = "") -> str:
    """Alias for create_project_board with kind defaulting to board."""
    principal = _require_write(ctx, project, ("write:tasks",))
    result = store.create_project_board({
        "id": board_id,
        "title": title,
        "kind": kind or "board",
        "status": status,
        "purpose": purpose,
        "end_state": end_state,
        "description": description,
        "owner_org": owner_org,
        "owner_person_or_role": owner_person_or_role,
        "metadata": metadata_json,
    }, actor=auth.actor(principal), project=project)
    return _dumps(result)


@mcp.tool()
def create_mission(title: str, ctx: Context, project: str = "maxwell",
                   mission_id: str = "", board_id: str = "", status: str = "active",
                   purpose: str = "", end_state: str = "", description: str = "",
                   owner_org: str = "", owner_person_or_role: str = "",
                   metadata_json: str = "") -> str:
    """Alias for create_project_board with kind=mission."""
    principal = _require_write(ctx, project, ("write:tasks",))
    result = store.create_project_board({
        "id": mission_id or board_id,
        "title": title,
        "kind": "mission",
        "status": status,
        "purpose": purpose,
        "end_state": end_state,
        "description": description,
        "owner_org": owner_org,
        "owner_person_or_role": owner_person_or_role,
        "metadata": metadata_json,
    }, actor=auth.actor(principal), project=project)
    return _dumps(result)


@mcp.tool()
def unlink_task_from_deliverable(deliverable_id: str, task_project: str, task_id: str,
                                 ctx: Context, project: str = "maxwell") -> str:
    """Remove a cross-project task link from a deliverable without mutating the task."""
    principal = _require_write(ctx, project, ("write:tasks",))
    result = store.unlink_task_from_deliverable(
        deliverable_id, task_project, task_id,
        actor=auth.actor(principal), project=project)
    return _dumps(result)


@mcp.tool()
def get_mission_status(project: str = "maxwell", deliverable_id: str = "",
                       board_id: str = "", mission_id: str = "") -> str:
    """Mission cockpit rollup: end state, milestones, linked tasks, proof, blockers, economics, next actions."""
    return _dumps(store.get_mission_status(
        project=project, deliverable_id=deliverable_id,
        board_id=board_id, mission_id=mission_id))


@mcp.tool()
def get_deliverable_dependency_graph(project: str = "maxwell", deliverable_id: str = "",
                                     board_id: str = "", mission_id: str = "") -> str:
    """Strategic dependency map: task nodes, depends_on edges, external blockers, mermaid flowchart."""
    return _dumps(store.get_deliverable_dependency_graph(
        project=project, deliverable_id=deliverable_id,
        board_id=board_id, mission_id=mission_id))


@mcp.tool()
def mission_status(project: str = "maxwell", deliverable_id: str = "",
                   board_id: str = "", mission_id: str = "") -> str:
    """Alias for get_mission_status."""
    return get_mission_status(project=project, deliverable_id=deliverable_id,
                              board_id=board_id, mission_id=mission_id)


@mcp.tool()
def update_mission_narrative(deliverable_id: str, narrative: str, ctx: Context,
                             project: str = "maxwell", append: bool = False) -> str:
    """Store or append the operator-facing mission narrative on a deliverable."""
    principal = _require_write(ctx, project, ("write:tasks",))
    result = store.update_mission_narrative(
        deliverable_id, narrative, actor=auth.actor(principal),
        project=project, append=append)
    return _dumps(result)


@mcp.tool()
def verify_deliverable_closure(deliverable_id: str, ctx: Context, project: str = "maxwell",
                               report_json: str = "", submitted_functional_json: str = "",
                               waivers_json: str = "", generated_by: str = "") -> str:
    """Run scope + functional closure gates for a deliverable (or accept an agent-submitted
    switchboard.deliverable_closure_report.v1), grade it, persist the report, and stamp
    deliverable.closure_verified. Pass submitted_functional_json for verifier-run script/pytest
    gate results ({gate_id: {pass, duration_s?, artifact_hash?}}) and waivers_json for
    operator task waivers; the server never executes heavy gates in-process."""
    principal = _require_write(ctx, project, ("write:tasks",))
    parsed = {}
    for name, raw in (("report", report_json),
                      ("submitted_functional", submitted_functional_json),
                      ("waivers", waivers_json)):
        if not raw:
            continue
        try:
            parsed[name] = json.loads(raw)
        except json.JSONDecodeError as exc:
            return _dumps({"error": f"invalid {name}_json: {exc}"})
    return _dumps(deliverable_closure.verify_and_record_closure(
        deliverable_id, project, actor=auth.actor(principal),
        report=parsed.get("report"),
        submitted_functional=parsed.get("submitted_functional"),
        waivers=parsed.get("waivers"),
        generated_by=generated_by or auth.actor(principal)))


@mcp.tool()
def get_deliverable_closure_report(deliverable_id: str, project: str = "maxwell",
                                   report_id: str = "") -> str:
    """Fetch the latest (or a specific report_id) persisted deliverable closure report
    plus its retained grade history."""
    return _dumps(store.get_deliverable_closure_report(
        deliverable_id, project=project, report_id=report_id))


@mcp.tool()
def generate_mission_brief(deliverable_id: str, ctx: Context, project: str = "maxwell",
                           board_id: str = "", mission_id: str = "",
                           persist: bool = True) -> str:
    """Generate a structured live mission brief from durable task/provenance events."""
    principal = _require_write(ctx, project, ("write:tasks",))
    result = store.generate_mission_brief(
        project=project, deliverable_id=deliverable_id,
        board_id=board_id, mission_id=mission_id,
        actor=auth.actor(principal), persist=persist)
    return _dumps(result)


@mcp.tool()
def run_mission_coordinator(deliverable_id: str, ctx: Context, project: str = "maxwell",
                          board_id: str = "", mission_id: str = "",
                          coordinator_agent_id: str = "", worker_agent_id: str = "",
                          auto_claim: bool = True, auto_wake: bool = False,
                          auto_refresh_brief: bool = True, policy_json: str = "",
                          idem_key: str = "") -> str:
    """Run one deliverable-scoped coordinator tick: refresh brief, dispatch, monitor, or escalate."""
    principal = _require_write(ctx, project, ("write:tasks",))
    policy = {
        "auto_claim": auto_claim,
        "auto_wake": auto_wake,
        "auto_refresh_brief": auto_refresh_brief,
        "worker_agent_id": worker_agent_id,
    }
    if policy_json:
        try:
            extra = json.loads(policy_json)
        except json.JSONDecodeError as exc:
            return _dumps({"error": f"invalid policy_json: {exc}"})
        if not isinstance(extra, dict):
            return _dumps({"error": "policy_json must be a JSON object"})
        policy.update(extra)
    return _dumps(store.run_mission_coordinator_tick(
        project=project,
        deliverable_id=deliverable_id,
        board_id=board_id,
        mission_id=mission_id,
        coordinator_agent_id=coordinator_agent_id,
        actor=auth.actor(principal),
        idem_key=idem_key,
        policy=policy,
    ))


@mcp.tool()
def get_mission_brief(deliverable_id: str, project: str = "maxwell",
                      board_id: str = "", mission_id: str = "") -> str:
    """Return stored/generated mission brief fields from mission_status."""
    if not store.has_project(project):
        return _dumps({"error": f"unknown project: {project}", "project": project})
    status = store.get_mission_status(
        project=project, deliverable_id=deliverable_id,
        board_id=board_id, mission_id=mission_id)
    if status.get("error"):
        return _dumps(status)
    return _dumps({
        "schema": "switchboard.mission_brief_view.v1",
        "project_id": project,
        "deliverable_id": status.get("deliverable_id"),
        "mission_brief": status.get("mission_brief"),
        "narrative_state": status.get("narrative_state"),
        "narrative": status.get("narrative"),
        "brief_generated_at": status.get("brief_generated_at"),
        "narrative_source": status.get("narrative_source"),
    })


@mcp.tool()
def propose_deliverable_breakdown(deliverable_id: str, milestones_json: str,
                                  ctx: Context, project: str = "maxwell",
                                  proposal_id: str = "", notes: str = "") -> str:
    """Propose milestones and future tasks without creating board tasks until approved."""
    principal = _require_write(ctx, project, ("write:tasks",))
    try:
        milestones = json.loads(milestones_json)
    except json.JSONDecodeError:
        return _dumps({"error": "milestones_json must be valid JSON"})
    payload = {"milestones": milestones}
    if notes:
        payload["notes"] = notes
    result = store.propose_deliverable_breakdown(
        deliverable_id, payload, actor=auth.actor(principal),
        project=project, proposal_id=proposal_id)
    return _dumps(result)


@mcp.tool()
def approve_deliverable_breakdown(proposal_id: str, ctx: Context,
                                  project: str = "maxwell") -> str:
    """Approve a breakdown proposal and materialize milestones, tasks, and links."""
    principal = _require_write(ctx, project, ("write:tasks",))
    result = store.approve_deliverable_breakdown(
        proposal_id, actor=auth.actor(principal), project=project)
    return _dumps(result)


@mcp.tool()
def submit_deliverable_outcome(deliverable_id: str, outcome: str, ctx: Context,
                               project: str = "maxwell",
                               target_projects_json: str = "",
                               policy_constraints_json: str = "",
                               acceptance_criteria: str = "",
                               use_llm: bool = False) -> str:
    """Submit a coordinator outcome and generate a milestone/task breakdown draft for review."""
    principal = _require_write(ctx, project, ("write:tasks",))
    result = store.submit_deliverable_outcome(
        deliverable_id, outcome, actor=auth.actor(principal), project=project,
        target_projects=target_projects_json or None,
        policy_constraints=policy_constraints_json or None,
        acceptance_criteria=acceptance_criteria or None,
        use_llm=use_llm,
    )
    return _dumps(result)


@mcp.tool()
def get_deliverable_breakdown_proposal(proposal_id: str,
                                       project: str = "maxwell") -> str:
    """Fetch one deliverable breakdown proposal with deliverable context."""
    if not store.has_project(project):
        return _dumps({"error": f"unknown project: {project}", "project": project})
    result = store.get_deliverable_breakdown_proposal(proposal_id, project=project)
    return _dumps(result or {"error": "unknown proposal", "proposal_id": proposal_id})


@mcp.tool()
def list_deliverable_breakdown_proposals(deliverable_id: str = "",
                                         project: str = "maxwell",
                                         status: str = "") -> str:
    """List breakdown proposals for one deliverable, optionally filtered by status."""
    if not store.has_project(project):
        return _dumps({"error": f"unknown project: {project}", "project": project})
    return _dumps({
        "project": project,
        "deliverable_id": deliverable_id or None,
        "proposals": store.list_deliverable_breakdown_proposals(
            deliverable_id=deliverable_id, project=project, status=status),
    })


@mcp.tool()
def update_deliverable_breakdown_proposal(proposal_id: str, milestones_json: str,
                                          ctx: Context, project: str = "maxwell",
                                          outcome: str = "", notes: str = "") -> str:
    """Edit a pending breakdown proposal before approval."""
    principal = _require_write(ctx, project, ("write:tasks",))
    try:
        milestones = json.loads(milestones_json)
    except json.JSONDecodeError:
        return _dumps({"error": "milestones_json must be valid JSON"})
    payload = {"milestones": milestones}
    if notes:
        payload["notes"] = notes
    if outcome:
        payload["outcome"] = outcome
    result = store.update_deliverable_breakdown_proposal(
        proposal_id, payload, actor=auth.actor(principal), project=project,
        outcome_text=outcome)
    return _dumps(result)


@mcp.tool()
def reject_deliverable_breakdown(proposal_id: str, reason: str, ctx: Context,
                                 project: str = "maxwell") -> str:
    """Reject a pending breakdown proposal with an audited reason."""
    principal = _require_write(ctx, project, ("write:tasks",))
    result = store.reject_deliverable_breakdown(
        proposal_id, reason, actor=auth.actor(principal), project=project)
    return _dumps(result)


@mcp.tool()
def defer_deliverable_breakdown(proposal_id: str, reason: str, ctx: Context,
                                project: str = "maxwell",
                                defer_until: str = "") -> str:
    """Defer a pending breakdown proposal with an audited reason."""
    principal = _require_write(ctx, project, ("write:tasks",))
    until = None
    if defer_until.strip():
        try:
            until = float(defer_until)
        except ValueError:
            return _dumps({"error": "defer_until must be a unix timestamp"})
    result = store.defer_deliverable_breakdown(
        proposal_id, reason, actor=auth.actor(principal), project=project,
        defer_until=until)
    return _dumps(result)


@mcp.tool()
def list_scoped_tokens(ctx: Context, project: str = "maxwell",
                       include_revoked: bool = False, kind: str = "") -> str:
    """List bearer principals for one project without exposing token hashes or raw tokens.

    Requires write:system on the target project. Use this to audit which humans, agents,
    hosts, or system actors can call Switchboard over REST/MCP.
    """
    _require_write(ctx, project, ("write:system",))
    return _dumps({
        "project": project,
        "tokens": store.list_principals(project=project, include_revoked=include_revoked, kind=kind),
        "scope_definitions": store.principal_scope_definitions(),
        "valid_kinds": sorted(store.VALID_PRINCIPAL_KINDS),
    })


@mcp.tool()
def get_audit_export(ctx: Context, project: str = "maxwell") -> str:
    """Return the redacted enterprise audit evidence bundle for one project.

    Requires write:system. The bundle includes task/activity history, claims, messages,
    runner/session/control evidence, Git/offline provenance, Tally economics, and access
    principal/role history without exposing token hashes or raw secrets.
    """
    _require_write(ctx, project, ("write:system",))
    return _dumps(store.audit_export(project=project))


@mcp.tool()
def list_cleanup_candidates(ctx: Context, project: str = "maxwell",
                            kinds: str = "", proof_task_age_days: float = 14) -> str:
    """List stale lifecycle cleanup candidates without changing board state.

    Requires write:system. Candidates cover stale agent presence, expired runner sessions,
    orphan/expired claims and leases, old wake intents, fired/orphan monitors, and old terminal
    proof/sentinel tasks that can be archived without deleting provenance.
    """
    _require_write(ctx, project, ("write:system",))
    return _dumps(store.cleanup_candidates(
        project=project,
        proof_task_age_days=proof_task_age_days,
        include_kinds=store.coerce_csv_list(kinds),
    ))


@mcp.tool()
def apply_cleanup(ctx: Context, project: str = "maxwell", candidate_ids: str = "",
                  dry_run: bool = True, reason: str = "operator lifecycle cleanup",
                  kinds: str = "", proof_task_age_days: float = 14) -> str:
    """Dry-run or apply safe lifecycle cleanup with an audit trail.

    Pass comma/newline-separated candidate_ids to limit scope. With dry_run=false, each mutation
    writes cleanup activity and uses archive/resolve paths rather than raw deletion.
    """
    principal = _require_write(ctx, project, ("write:system",))
    return _dumps(store.apply_cleanup(
        project=project,
        candidate_ids=store.coerce_csv_list(candidate_ids),
        dry_run=dry_run,
        actor=auth.actor(principal),
        reason=reason,
        proof_task_age_days=proof_task_age_days,
        include_kinds=store.coerce_csv_list(kinds),
    ))


def _scoped_token_auth_project(project: str) -> str:
    binding = (project or "maxwell").strip()
    if store.is_global_project_binding(binding):
        return "switchboard"
    return binding


@mcp.tool()
def create_scoped_token(ctx: Context, project: str = "maxwell", kind: str = "agent",
                        display_name: str = "", scopes: str = "", role: str = "",
                        principal_id: str = "") -> str:
    """Create one project-scoped bearer token for REST/MCP callers.

    Requires write:system on the target project. Pass project='*' for a global agent token that
    can read/write every current and future board. `role` is a preset such as viewer,
    contributor, operator, or admin; `scopes` can also be a comma/newline list. The raw token is
    returned once and is never stored, so capture it immediately.
    """
    binding = (project or "maxwell").strip()
    auth_project = _scoped_token_auth_project(binding)
    if not store.is_global_project_binding(binding) and not store.has_project(binding):
        return _dumps({"error": f"unknown project: {binding}"})
    principal = _require_write(ctx, auth_project, ("write:system",))
    resolved = store.resolve_principal_scopes(scopes, role=role)
    if resolved.get("error"):
        return _dumps(resolved)
    normalized_kind = store.validate_principal_kind(kind or "agent")
    if not normalized_kind:
        return _dumps({"error": "kind must be one of: " + ", ".join(sorted(store.VALID_PRINCIPAL_KINDS))})
    raw_token = auth.new_secret_token()
    created = store.create_principal(
        kind=normalized_kind,
        display_name=display_name or normalized_kind,
        token=raw_token,
        scopes=resolved["scopes"],
        principal_id=principal_id or None,
        project=binding,
    )
    if created.get("error"):
        return _dumps(created)
    public = store.public_principal_record(created, project=auth_project)
    store.append_activity(
        "access.token_created",
        auth.actor(principal),
        {"principal": public, "role": resolved.get("role"), "token_returned_once": True},
        task_id=None,
        project=auth_project,
    )
    return _dumps({"project": binding, "principal": public, "token": raw_token,
                   "token_returned_once": True})


@mcp.tool()
def revoke_scoped_token(principal_id: str, ctx: Context, project: str = "maxwell") -> str:
    """Revoke one project-scoped bearer principal and any live sessions for that principal."""
    principal = _require_write(ctx, project, ("write:system",))
    result = store.revoke_principal_token(principal_id, project=project, actor=auth.actor(principal))
    return _dumps(result)


@mcp.tool()
def update_task(task_id: str, ctx: Context, title: str = "", description: str = "", status: str = "",
                owner_org: str = "", owner_person_or_role: str = "", assignee: str = "",
                phase: str = "", start_date: str = "", finish_date: str = "",
                risk_level: str = "", is_blocking: str = "", depends_on: str = "",
                project: str = "maxwell", agent_id: str = "",
                system_actor: str = "", system_reason: str = "") -> str:
    """Update only the fields you pass on a task. status: Not Started|In Progress|In Review|Blocked|Done;
    Done fails closed unless merge/default-branch provenance is already recorded for the task;
    dates: YYYY-MM-DD; is_blocking: 'true'/'false'. depends_on: comma/space-separated task ids that
    REPLACE this task's dependency list (e.g. 'TOOLS-7, SHELL-1'); pass 'none' to clear it (for an
    incremental edge use add_dependency/remove_dependency). Audited as the authenticated actor.
    project selects the board ('maxwell' default, 'helm', or 'switchboard') — writes go ONLY to that board."""
    principal = _require_write(ctx, project)
    binding = _resolve_write_actor(
        principal, project=project, task_id=task_id, agent_id=agent_id,
        system_actor=system_actor, system_reason=system_reason)
    if not binding.get("ok"):
        return _dumps(binding)
    actor_name = binding["actor"]
    data = {}
    for k, v in (("title", title), ("description", description), ("status", status),
                 ("owner_org", owner_org), ("owner_person_or_role", owner_person_or_role),
                 ("assignee", assignee), ("phase", phase), ("start_date", start_date),
                 ("finish_date", finish_date), ("risk_level", risk_level),
                 ("is_blocking", is_blocking), ("depends_on", depends_on)):
        if v != "":
            data[k] = v
    if not data:
        return "no fields to update"
    t = update_task_command.execute_mapping_result(task_id, data, actor=actor_name, project=project)
    if isinstance(t, dict) and t.get("error_code"):
        return _dumps(t)   # command validation failed (e.g. unknown deps): fail loud, no binding comment
    _write_binding_comment(task_id, binding, project)
    return _dumps(agent._task_brief(t)) if t else "no such task"


@mcp.tool()
def create_task(workstream_id: str, title: str, ctx: Context, description: str = "",
                owner_org: str = "", owner_person_or_role: str = "", status: str = "",
                phase: str = "", risk_level: str = "", depends_on: str = "",
                project: str = "maxwell", agent_id: str = "",
                system_actor: str = "", system_reason: str = "") -> str:
    """Create a task in a workstream (SSO/SEN/... for Maxwell; ENGINE/CHART/... for Helm;
    PROTO/ADAPTER/ENFORCE/... for Switchboard). depends_on:
    comma/space-separated task ids this task dependsOn (e.g. 'BOAT-1, WX-10'). Returns the created task.
    Actor 'MCP'. project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    principal = _require_write(ctx, project)
    binding = _resolve_write_actor(
        principal, project=project, agent_id=agent_id,
        system_actor=system_actor, system_reason=system_reason)
    if not binding.get("ok"):
        return _dumps(binding)
    actor_name = binding["actor"]
    t = create_task_command.execute_mapping_result(locals(), actor=actor_name, project=project)
    if t.get("error"):
        return _dumps(t)
    if t:
        _write_binding_comment(t.get("task_id") or "", binding, project)
    return _dumps(agent._task_brief(t))


@mcp.tool()
def submit_bug(source_task: str, observed_behavior: str, expected_behavior: str,
               repro_steps: str, evidence: str, severity_hint: str,
               affected_surface: str, ctx: Context, project: str = "maxwell",
               source_agent: str = "", failure_class: str = "",
               duplicate_of: str = "", title: str = "") -> str:
    """Submit an agent-discovered bug through the dedicated BUG intake path.

    Requires write:bug_intake. Creates exactly one BUG triage task with structured
    bug_report state and source task/agent linkage. It does not create implementation
    work, mark work Ready, dispatch agents, or bypass the human gate.
    """
    principal = _require_write(ctx, project, ("write:bug_intake",))
    actor_name = auth.actor(principal)
    result = store.submit_bug({
        "source_task": source_task,
        "source_agent": source_agent or actor_name,
        "observed_behavior": observed_behavior,
        "expected_behavior": expected_behavior,
        "repro_steps": repro_steps,
        "evidence": evidence,
        "severity_hint": severity_hint,
        "affected_surface": affected_surface,
        "failure_class": failure_class,
        "duplicate_of": duplicate_of,
        "title": title,
    }, actor=actor_name, project=project)
    return _dumps(result)


@mcp.tool()
def add_comment(task_id: str, text: str, ctx: Context, project: str = "maxwell",
                agent_id: str = "", system_actor: str = "",
                system_reason: str = "") -> str:
    """Add a note to a task's activity log (audited as actor 'MCP').
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    principal = _require_write(ctx, project)
    binding = _resolve_write_actor(
        principal, project=project, task_id=task_id, agent_id=agent_id,
        system_actor=system_actor, system_reason=system_reason)
    if not binding.get("ok"):
        return _dumps(binding)
    _write_binding_comment(task_id, binding, project)
    t = store.add_comment(
        task_id, binding["actor"], text, project=project, hydrate_task=False)
    return "ok" if t else "no such task"


@mcp.tool()
def archive_task(task_id: str, ctx: Context, project: str = "maxwell",
                 reason: str = "") -> str:
    """Archive a task instead of raw-deleting it.

    Requires the system write scope because this removes the active task row. The archived
    snapshot preserves task fields, activity, git/provenance, Tally rows, claims/leases, and
    related decision records where possible. Fails if the task has active claims or leases.
    project selects the board ('maxwell' default, 'helm', 'switchboard', or dynamic projects).
    """
    principal = _require_write(ctx, "switchboard", ("write:system",))
    result = store.archive_task(task_id, reason=reason, actor=auth.actor(principal),
                                project=project)
    return _dumps(result)


@mcp.tool()
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
    principal = _require_write(ctx, "switchboard", ("write:system",))
    result = store.move_task(
        task_id, project_from=project_from, project_to=project_to,
        reason=reason, actor=auth.actor(principal), new_task_id=new_task_id,
        dependency_policy=dependency_policy)
    return _dumps(result)


@mcp.tool()
def add_dependency(task_id: str, depends_on: str, ctx: Context, project: str = "maxwell") -> str:
    """Add one or more dependency EDGES to a task (task_id dependsOn each id in depends_on,
    comma/space-separated, e.g. 'TOOLS-7, SHELL-1'). APPENDS without clobbering existing deps
    (idempotent, deduped) — use this to wire cross-epic edges. FAIL-FAST: if ANY id is not a real
    task the whole call is REJECTED with an error and nothing is written (a dependency to a
    non-existent task is a broken graph edge) — fix the id or create the target first, then retry.
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    principal = _require_write(ctx, project)
    actor_name = auth.actor(principal)
    add = _dep_ids(depends_on)
    if not add:
        return "no dependency ids given"
    t = store.get_task(task_id, project=project)
    if not t:
        return "no such task: " + task_id
    unknown = _unknown_ids(add, project)
    if unknown:   # FAIL LOUD: reject the whole batch — never write a dangling edge
        return _dumps({"error": "unknown task id(s) on project '%s': %s — NO edge added. "
                       "Create the target task(s) first or fix the id." % (project, ", ".join(unknown))})
    merged = list(t.get("depends_on") or [])
    for d in add:
        if d not in merged:
            merged.append(d)
    store.update_task(task_id, {"depends_on": merged}, actor=actor_name, project=project)
    return _dumps({"task_id": task_id, "depends_on": merged})


@mcp.tool()
def remove_dependency(task_id: str, depends_on: str, ctx: Context, project: str = "maxwell") -> str:
    """Remove one or more dependency edges from a task (comma/space-separated ids). Reports which ids
    were actually removed vs not present — a no-op removal is SURFACED, not silently swallowed.
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    principal = _require_write(ctx, project)
    actor_name = auth.actor(principal)
    rm = _dep_ids(depends_on)
    if not rm:
        return "no dependency ids given"
    t = store.get_task(task_id, project=project)
    if not t:
        return "no such task: " + task_id
    cur = list(t.get("depends_on") or [])
    rmset = set(rm)
    merged = [d for d in cur if d not in rmset]
    store.update_task(task_id, {"depends_on": merged}, actor=actor_name, project=project)
    res = {"task_id": task_id, "depends_on": merged, "removed": [d for d in cur if d in rmset]}
    not_present = [d for d in rm if d not in cur]
    if not_present:   # surface the no-op rather than pretend it did something
        res["note"] = "not present (nothing to remove): " + ", ".join(not_present)
    return _dumps(res)


@mcp.tool()
def generate_digest(ctx: Context) -> str:
    """Generate + post the weekly chief-of-staff brief (plan signals + activity deltas since the
    last digest). Returns the brief text. Creates a digest record."""
    _require_write(ctx)
    return digest_mod.generate_digest().get("content", "")


@mcp.tool()
def notify(subject: str, text: str, ctx: Context) -> str:
    """Send a message to the wired channels (Slack + Email). Unconfigured channels are dry-run."""
    _require_write(ctx)
    return _dumps(notify_mod.send(subject, text))


@mcp.tool()
def dispatch_to_claude_code(task_id: str, ctx: Context, project: str = "maxwell") -> str:
    """Queue a task for autonomous development via the fleet. Enqueues a lane-scoped claim_next
    wake intent that a work-capable Agent Host claims and runs in an isolated worktree, opening a
    PR on a `claude/<task>` branch — never main. Returns {dispatched, wake_id, work_hosts_online, …}.
    If no work-capable host is online for the task's project/lane, the wake queues until one is
    (deploy/switchboard-agent-host-work.service.example). project selects the board."""
    principal = _require_write(ctx, project)
    import dispatch as dispatch_mod
    return _dumps(dispatch_mod.dispatch(task_id, actor=auth.actor(principal), project=project))


@mcp.tool()
def ingest_and_triage(kind: str, title: str, text: str, ctx: Context, project: str = "maxwell") -> str:
    """Ingest an artifact (email / transcript / document / note) into `project`'s RAG corpus AND
    triage it against that board. Returns {summary, proposals, new_tasks, sources} — proposals are
    NOT applied (use update_task / create_task to apply). kind: email|transcript|document|note.
    project selects the board — the corpus is segmented per project."""
    _require_write(ctx, project)
    return _dumps(intake_mod.ingest_and_triage(kind, title, text, project=project))


if __name__ == "__main__":
    import uvicorn

    # FastMCP.run() builds this same ASGI app internally. Running it explicitly
    # lets Switchboard attach timing/reconnect headers to success and error paths.
    # Timing wraps auth so even rejected (401) requests carry timing headers; auth wraps
    # the tool app so anonymous callers are turned away before any tool runs (BUG-46).
    from concurrency_limiter import ConcurrencyLimitASGIMiddleware

    # The observability endpoint sits between timing and auth (HARDEN-63): operators
    # scrape GET /observability without a token (read-only, no request content), and the
    # response still carries the standard server-timing header.
    app = MCPServerTimingMiddleware(
        MCPObservabilityEndpoint(
            MCPAuthMiddleware(
                ConcurrencyLimitASGIMiddleware(mcp.streamable_http_app()),
            ),
            _mcp_observability.snapshot,
        )
    )
    uvicorn.run(
        app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )
