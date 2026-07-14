"""Deliverables / mission MCP tools.

Transport adapter extracted in ARCH-MS-65. Authentication and MCP serialization
remain edge concerns; create_deliverable uses a shared application command.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth
import deliverable_closure
import store
from switchboard.application.commands import create_deliverable as create_deliverable_command


@dataclass(frozen=True)
class DeliverableToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: DeliverableToolServices | None = None


def _services() -> DeliverableToolServices:
    if _SERVICES is None:
        raise RuntimeError("deliverable MCP tools must be registered before use")
    return _SERVICES


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
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
    result = create_deliverable_command.execute_mapping_result({
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
    return services.dumps(result)


def get_deliverable(deliverable_id: str, project: str = "maxwell") -> str:
    """Fetch one deliverable with milestones, cross-project task links, progress, and board context."""
    services = _services()
    if not store.has_project(project):
        return services.dumps({"error": f"unknown project: {project}", "project": project})
    result = store.get_deliverable(deliverable_id, project=project)
    return services.dumps(result or {"error": "unknown deliverable",
                             "deliverable_id": deliverable_id, "project": project})


def list_deliverables(project: str = "maxwell", board_id: str = "") -> str:
    """List slim deliverable rows owned by one Project, optionally scoped to a Board/Mission id.
    Rows include metadata, milestones, raw task-link ids, and truthful progress counts, but omit
    linked-task snapshots. Call get_deliverable for one full deliverable when task detail is needed."""
    services = _services()
    if not store.has_project(project):
        return services.dumps({"error": f"unknown project: {project}", "project": project})
    return services.dumps({"project": project, "board_id": board_id or None,
                   "deliverables": store.list_deliverables(project=project,
                                                            board_id=board_id,
                                                            include_task_snapshots=False)})


def add_deliverable_milestone(deliverable_id: str, title: str, ctx: Context,
                              project: str = "maxwell", milestone_id: str = "",
                              description: str = "", status: str = "not_started",
                              sort_order: str = "",
                              acceptance_criteria: str = "",
                              proof_requirements_json: str = "") -> str:
    """Create/update a milestone inside a deliverable."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
    result = store.add_deliverable_milestone(deliverable_id, {
        "id": milestone_id,
        "title": title,
        "description": description,
        "status": status,
        "sort_order": sort_order,
        "acceptance_criteria": acceptance_criteria,
        "proof_requirements": proof_requirements_json,
    }, actor=auth.actor(principal), project=project)
    return services.dumps(result)


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
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
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
    return services.dumps(result)


def link_tasks_to_deliverable(deliverable_id: str, links: list[dict], ctx: Context,
                              project: str = "maxwell") -> str:
    """Link many explicitly routed tasks to one deliverable in one write transaction.

    Each links item requires task_project and task_id. Optional fields match the
    single-link tool: milestone_id, board_id/mission_id, role, blocks_deliverable,
    proof_required/proof_required_json, and metadata/metadata_json.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
    result = store.link_tasks_to_deliverable(
        deliverable_id, links, actor=auth.actor(principal), project=project)
    return services.dumps(result)


def create_board(title: str, ctx: Context, project: str = "maxwell",
                 board_id: str = "", kind: str = "board", status: str = "active",
                 purpose: str = "", end_state: str = "", description: str = "",
                 owner_org: str = "", owner_person_or_role: str = "",
                 metadata_json: str = "") -> str:
    """Alias for create_project_board with kind defaulting to board."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
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
    return services.dumps(result)


def create_mission(title: str, ctx: Context, project: str = "maxwell",
                   mission_id: str = "", board_id: str = "", status: str = "active",
                   purpose: str = "", end_state: str = "", description: str = "",
                   owner_org: str = "", owner_person_or_role: str = "",
                   metadata_json: str = "") -> str:
    """Alias for create_project_board with kind=mission."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
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
    return services.dumps(result)


def unlink_task_from_deliverable(deliverable_id: str, task_project: str, task_id: str,
                                 ctx: Context, project: str = "maxwell") -> str:
    """Remove a cross-project task link from a deliverable without mutating the task."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
    result = store.unlink_task_from_deliverable(
        deliverable_id, task_project, task_id,
        actor=auth.actor(principal), project=project)
    return services.dumps(result)


def get_mission_status(project: str = "maxwell", deliverable_id: str = "",
                       board_id: str = "", mission_id: str = "") -> str:
    """Mission cockpit rollup: end state, milestones, linked tasks, proof, blockers, economics, next actions."""
    services = _services()
    return services.dumps(store.get_mission_status(
        project=project, deliverable_id=deliverable_id,
        board_id=board_id, mission_id=mission_id))


def get_deliverable_dependency_graph(project: str = "maxwell", deliverable_id: str = "",
                                     board_id: str = "", mission_id: str = "") -> str:
    """Strategic dependency map: task nodes, depends_on edges, external blockers, mermaid flowchart."""
    services = _services()
    return services.dumps(store.get_deliverable_dependency_graph(
        project=project, deliverable_id=deliverable_id,
        board_id=board_id, mission_id=mission_id))


def mission_status(project: str = "maxwell", deliverable_id: str = "",
                   board_id: str = "", mission_id: str = "") -> str:
    """Alias for get_mission_status."""
    return get_mission_status(project=project, deliverable_id=deliverable_id,
                              board_id=board_id, mission_id=mission_id)


def update_mission_narrative(deliverable_id: str, narrative: str, ctx: Context,
                             project: str = "maxwell", append: bool = False) -> str:
    """Store or append the operator-facing mission narrative on a deliverable."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
    result = store.update_mission_narrative(
        deliverable_id, narrative, actor=auth.actor(principal),
        project=project, append=append)
    return services.dumps(result)


def verify_deliverable_closure(deliverable_id: str, ctx: Context, project: str = "maxwell",
                               report_json: str = "", submitted_functional_json: str = "",
                               waivers_json: str = "", generated_by: str = "") -> str:
    """Run scope + functional closure gates for a deliverable (or accept an agent-submitted
    switchboard.deliverable_closure_report.v1), grade it, persist the report, and stamp
    deliverable.closure_verified. Pass submitted_functional_json for verifier-run script/pytest
    gate results ({gate_id: {pass, duration_s?, artifact_hash?}}) and waivers_json for
    operator task waivers; the server never executes heavy gates in-process."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
    parsed = {}
    for name, raw in (("report", report_json),
                      ("submitted_functional", submitted_functional_json),
                      ("waivers", waivers_json)):
        if not raw:
            continue
        try:
            parsed[name] = json.loads(raw)
        except json.JSONDecodeError as exc:
            return services.dumps({"error": f"invalid {name}_json: {exc}"})
    return services.dumps(deliverable_closure.verify_and_record_closure(
        deliverable_id, project, actor=auth.actor(principal),
        report=parsed.get("report"),
        submitted_functional=parsed.get("submitted_functional"),
        waivers=parsed.get("waivers"),
        generated_by=generated_by or auth.actor(principal)))


def get_deliverable_closure_report(deliverable_id: str, project: str = "maxwell",
                                   report_id: str = "") -> str:
    """Fetch the latest (or a specific report_id) persisted deliverable closure report
    plus its retained grade history."""
    services = _services()
    return services.dumps(store.get_deliverable_closure_report(
        deliverable_id, project=project, report_id=report_id))


def request_deliverable_closure_verification(deliverable_id: str, ctx: Context,
                                             project: str = "maxwell",
                                             agent_id: str = "",
                                             waivers_json: str = "") -> str:
    """Operator "Verify & stamp closure" dispatch: assemble the deliverable's context,
    its resolved scope+functional gate list, and a closure prompt template, then dispatch a
    verifier agent (directed inbox message + lane-less inbox wake) to run the gates and record
    a graded switchboard.deliverable_closure_report.v1 via verify_deliverable_closure. The
    verifier never sets status=done. Pass agent_id to target a specific verifier and
    waivers_json ([{task_id, reason}]) for operator task waivers. Returns {dispatched, wake_id,
    message_id, agent_id, gates, prompt, work_hosts_online, queued, …}; queues until a
    work-capable host is online (mirrors dispatch_to_claude_code for tasks)."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
    waivers = None
    if waivers_json:
        try:
            waivers = json.loads(waivers_json)
        except json.JSONDecodeError as exc:
            return services.dumps({"error": f"invalid waivers_json: {exc}"})
    return services.dumps(deliverable_closure.request_closure_verification(
        deliverable_id, project, agent_id=agent_id,
        actor=auth.actor(principal), waivers=waivers))


def generate_mission_brief(deliverable_id: str, ctx: Context, project: str = "maxwell",
                           board_id: str = "", mission_id: str = "",
                           persist: bool = True) -> str:
    """Generate a structured live mission brief from durable task/provenance events."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
    result = store.generate_mission_brief(
        project=project, deliverable_id=deliverable_id,
        board_id=board_id, mission_id=mission_id,
        actor=auth.actor(principal), persist=persist)
    return services.dumps(result)


def run_mission_coordinator(deliverable_id: str, ctx: Context, project: str = "maxwell",
                          board_id: str = "", mission_id: str = "",
                          coordinator_agent_id: str = "", worker_agent_id: str = "",
                          auto_claim: bool = True, auto_wake: bool = False,
                          auto_refresh_brief: bool = True, policy_json: str = "",
                          idem_key: str = "") -> str:
    """Run one deliverable-scoped coordinator tick: refresh brief, dispatch, monitor, or escalate."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
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
            return services.dumps({"error": f"invalid policy_json: {exc}"})
        if not isinstance(extra, dict):
            return services.dumps({"error": "policy_json must be a JSON object"})
        policy.update(extra)
    return services.dumps(store.run_mission_coordinator_tick(
        project=project,
        deliverable_id=deliverable_id,
        board_id=board_id,
        mission_id=mission_id,
        coordinator_agent_id=coordinator_agent_id,
        actor=auth.actor(principal),
        idem_key=idem_key,
        policy=policy,
    ))


def get_mission_brief(deliverable_id: str, project: str = "maxwell",
                      board_id: str = "", mission_id: str = "") -> str:
    """Return stored/generated mission brief fields from mission_status."""
    services = _services()
    if not store.has_project(project):
        return services.dumps({"error": f"unknown project: {project}", "project": project})
    status = store.get_mission_status(
        project=project, deliverable_id=deliverable_id,
        board_id=board_id, mission_id=mission_id)
    if status.get("error"):
        return services.dumps(status)
    return services.dumps({
        "schema": "switchboard.mission_brief_view.v1",
        "project_id": project,
        "deliverable_id": status.get("deliverable_id"),
        "mission_brief": status.get("mission_brief"),
        "narrative_state": status.get("narrative_state"),
        "narrative": status.get("narrative"),
        "brief_generated_at": status.get("brief_generated_at"),
        "narrative_source": status.get("narrative_source"),
    })


def propose_deliverable_breakdown(deliverable_id: str, milestones_json: str,
                                  ctx: Context, project: str = "maxwell",
                                  proposal_id: str = "", notes: str = "") -> str:
    """Propose milestones and future tasks without creating board tasks until approved."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
    try:
        milestones = json.loads(milestones_json)
    except json.JSONDecodeError:
        return services.dumps({"error": "milestones_json must be valid JSON"})
    payload = {"milestones": milestones}
    if notes:
        payload["notes"] = notes
    result = store.propose_deliverable_breakdown(
        deliverable_id, payload, actor=auth.actor(principal),
        project=project, proposal_id=proposal_id)
    return services.dumps(result)


def approve_deliverable_breakdown(proposal_id: str, ctx: Context,
                                  project: str = "maxwell") -> str:
    """Approve a breakdown proposal and materialize milestones, tasks, and links."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
    result = store.approve_deliverable_breakdown(
        proposal_id, actor=auth.actor(principal), project=project)
    return services.dumps(result)


def submit_deliverable_outcome(deliverable_id: str, outcome: str, ctx: Context,
                               project: str = "maxwell",
                               target_projects_json: str = "",
                               policy_constraints_json: str = "",
                               acceptance_criteria: str = "",
                               use_llm: bool = False) -> str:
    """Submit a coordinator outcome and generate a milestone/task breakdown draft for review."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
    result = store.submit_deliverable_outcome(
        deliverable_id, outcome, actor=auth.actor(principal), project=project,
        target_projects=target_projects_json or None,
        policy_constraints=policy_constraints_json or None,
        acceptance_criteria=acceptance_criteria or None,
        use_llm=use_llm,
    )
    return services.dumps(result)


def get_deliverable_breakdown_proposal(proposal_id: str,
                                       project: str = "maxwell") -> str:
    """Fetch one deliverable breakdown proposal with deliverable context."""
    services = _services()
    if not store.has_project(project):
        return services.dumps({"error": f"unknown project: {project}", "project": project})
    result = store.get_deliverable_breakdown_proposal(proposal_id, project=project)
    return services.dumps(result or {"error": "unknown proposal", "proposal_id": proposal_id})


def list_deliverable_breakdown_proposals(deliverable_id: str = "",
                                         project: str = "maxwell",
                                         status: str = "") -> str:
    """List breakdown proposals for one deliverable, optionally filtered by status."""
    services = _services()
    if not store.has_project(project):
        return services.dumps({"error": f"unknown project: {project}", "project": project})
    return services.dumps({
        "project": project,
        "deliverable_id": deliverable_id or None,
        "proposals": store.list_deliverable_breakdown_proposals(
            deliverable_id=deliverable_id, project=project, status=status),
    })


def update_deliverable_breakdown_proposal(proposal_id: str, milestones_json: str,
                                          ctx: Context, project: str = "maxwell",
                                          outcome: str = "", notes: str = "") -> str:
    """Edit a pending breakdown proposal before approval."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
    try:
        milestones = json.loads(milestones_json)
    except json.JSONDecodeError:
        return services.dumps({"error": "milestones_json must be valid JSON"})
    payload = {"milestones": milestones}
    if notes:
        payload["notes"] = notes
    if outcome:
        payload["outcome"] = outcome
    result = store.update_deliverable_breakdown_proposal(
        proposal_id, payload, actor=auth.actor(principal), project=project,
        outcome_text=outcome)
    return services.dumps(result)


def reject_deliverable_breakdown(proposal_id: str, reason: str, ctx: Context,
                                 project: str = "maxwell") -> str:
    """Reject a pending breakdown proposal with an audited reason."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
    result = store.reject_deliverable_breakdown(
        proposal_id, reason, actor=auth.actor(principal), project=project)
    return services.dumps(result)


def defer_deliverable_breakdown(proposal_id: str, reason: str, ctx: Context,
                                project: str = "maxwell",
                                defer_until: str = "") -> str:
    """Defer a pending breakdown proposal with an audited reason."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
    until = None
    if defer_until.strip():
        try:
            until = float(defer_until)
        except ValueError:
            return services.dumps({"error": "defer_until must be a unix timestamp"})
    result = store.defer_deliverable_breakdown(
        proposal_id, reason, actor=auth.actor(principal), project=project,
        defer_until=until)
    return services.dumps(result)


DELIVERABLE_TOOL_NAMES = (
    "create_deliverable",
    "get_deliverable",
    "list_deliverables",
    "add_deliverable_milestone",
    "link_task_to_deliverable",
    "link_tasks_to_deliverable",
    "create_board",
    "create_mission",
    "unlink_task_from_deliverable",
    "get_mission_status",
    "get_deliverable_dependency_graph",
    "mission_status",
    "update_mission_narrative",
    "verify_deliverable_closure",
    "get_deliverable_closure_report",
    "request_deliverable_closure_verification",
    "generate_mission_brief",
    "run_mission_coordinator",
    "get_mission_brief",
    "propose_deliverable_breakdown",
    "approve_deliverable_breakdown",
    "submit_deliverable_outcome",
    "get_deliverable_breakdown_proposal",
    "list_deliverable_breakdown_proposals",
    "update_deliverable_breakdown_proposal",
    "reject_deliverable_breakdown",
    "defer_deliverable_breakdown",
)


def register_deliverable_tools(
        mcp: Any, services: DeliverableToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in DELIVERABLE_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
