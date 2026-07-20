"""Deliverables / mission REST routes.

Extracted in ARCH-MS-65. Composition root supplies project, principal, and
etag boundaries; create_deliverable uses a shared application command.
"""
from __future__ import annotations

from typing import Any, Callable, Literal

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

import auth
import deliverable_closure
import store
from switchboard.application.commands import create_deliverable as create_deliverable_command
from switchboard.application.commands import update_deliverable as update_deliverable_command


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
EtagJson = Callable[..., Response]


class AutopilotControlBody(BaseModel):
    """Typed operator intent for one durable task or deliverable scope."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["start", "pause", "resume", "stop"] = "start"
    profile_id: str = Field(default="autopilot-default", min_length=1, max_length=128)
    runtime: str = Field(default="codex", min_length=1, max_length=64)
    task_project: str = Field(default="", max_length=128)


class UpdateDeliverableBody(BaseModel):
    """Typed partial update for one deliverable contract."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    status: str | None = None
    end_state: str | None = None
    purpose: str | None = None
    metadata: dict[str, Any] | None = None
    replacement_deliverable_id: str | None = None
    scope_transition_reason: str | None = None


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  etag_json: EtagJson,
                  sibling_bc_only: bool = False) -> APIRouter:
    """Build the deliverables/mission router against shared trust boundaries."""
    router = APIRouter()

    @router.get("/api/deliverables")
    def list_deliverables(project: str = Query(...), board_id: str = "",
                          view: str = ""):
        # def (not async): run the SQLite/deliverable work in the threadpool so a slow
        # deliverable read can't block the single worker's event loop (same as /api/board).
        project = resolve_project(project)
        if view == "picker":
            return {"project": project, "board_id": board_id or None, "view": "picker",
                    "deliverables": store.list_deliverable_summaries(
                        project=project, board_id=board_id)}
        if view:
            raise HTTPException(400, "unknown deliverable list view")
        return {"project": project, "board_id": board_id or None,
                "deliverables": store.list_deliverables(project=project, board_id=board_id)}

    @router.post("/api/deliverables")
    async def create_deliverable(request: Request, body: dict = Body(...),
                                 project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        result = create_deliverable_command.execute_mapping_result(
            body or {}, actor=auth.actor(principal), project=project)
        if result.get("error"):
            # DELIVERABLES-13: surface the full error object (error + per-field details) when the
            # intake gate rejects a move into in_progress, so the operator sees which fields are
            # missing rather than a bare message. Falls back to the string for simple errors.
            raise HTTPException(400, result if result.get("details") else result["error"])
        return result

    # These literal-path reads MUST be registered before the `/{deliverable_id}` route
    # below — otherwise Starlette matches `/api/deliverables/breakdown_proposals` against
    # `{deliverable_id}` (first-registered wins) and the list 404s as an "unknown
    # deliverable". Keep them here; do not move them back down with the other breakdown
    # routes (UI-1).
    @router.get("/api/deliverables/breakdown_proposals")
    async def list_deliverable_breakdown_proposals(deliverable_id: str = "",
                                                   project: str = Query(...),
                                                   status: str = ""):
        project = resolve_project(project)
        return {
            "project": project,
            "deliverable_id": deliverable_id or None,
            "proposals": store.list_deliverable_breakdown_proposals(
                deliverable_id=deliverable_id, project=project, status=status),
        }

    @router.get("/api/deliverables/breakdown_proposals/{proposal_id}")
    async def get_deliverable_breakdown_proposal(proposal_id: str,
                                                 project: str = Query(...)):
        project = resolve_project(project)
        result = store.get_deliverable_breakdown_proposal(proposal_id, project=project)
        if not result:
            raise HTTPException(404, "proposal not found")
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.get("/api/deliverables/{deliverable_id}")
    def get_deliverable(deliverable_id: str, project: str = Query(...)):
        # def (not async): threadpool the deliverable read so it can't block the event loop.
        project = resolve_project(project)
        result = store.get_deliverable(deliverable_id, project=project)
        if not result:
            raise HTTPException(404, "deliverable not found")
        return result

    @router.patch("/api/deliverables/{deliverable_id}")
    async def update_deliverable(request: Request, deliverable_id: str,
                                 body: UpdateDeliverableBody = Body(...),
                                 project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        result = update_deliverable_command.execute_mapping_result(
            deliverable_id, body.model_dump(exclude_unset=True),
            actor=auth.actor(principal), project=project)
        if result.get("error"):
            status_code = 404 if result["error"] == "unknown deliverable" else 400
            raise HTTPException(status_code, result)
        return result

    @router.post("/api/deliverables/{deliverable_id}/milestones")
    async def add_deliverable_milestone(request: Request, deliverable_id: str,
                                        body: dict = Body(...),
                                        project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        result = store.add_deliverable_milestone(
            deliverable_id, body or {}, actor=auth.actor(principal), project=project)
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/api/deliverables/{deliverable_id}/task_links")
    async def link_task_to_deliverable(request: Request, deliverable_id: str,
                                       body: dict = Body(...),
                                       project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        payload = body or {}
        result = store.link_task_to_deliverable(
            deliverable_id,
            payload.get("task_project") or payload.get("project_id") or "",
            payload.get("task_id") or "",
            milestone_id=payload.get("milestone_id") or "",
            data=payload,
            actor=auth.actor(principal),
            project=project,
        )
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.delete("/api/deliverables/{deliverable_id}/task_links")
    async def unlink_task_from_deliverable(request: Request, deliverable_id: str,
                                           task_project: str = Query(...),
                                           task_id: str = Query(...),
                                           project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        result = store.unlink_task_from_deliverable(
            deliverable_id, task_project, task_id,
            actor=auth.actor(principal), project=project)
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.get("/api/mission_status")
    def mission_status_query(request: Request, project: str = Query(...), deliverable_id: str = "",
                             board_id: str = "", mission_id: str = ""):
        project = resolve_project(project)
        result = store.get_mission_status(
            project=project, deliverable_id=deliverable_id,
            board_id=board_id, mission_id=mission_id)
        if result.get("error"):
            code = 404 if "unknown" in result["error"] or "no deliverable" in result["error"] else 400
            raise HTTPException(code, result["error"])
        # CONSOL-8: the live cockpit polls this on a 5s timer. The store already serves a
        # short-TTL cached copy (HARDEN-36); the ETag/304 gives the mission pollers the same
        # wire-level parity /api/board has — an unchanged tick returns a bodyless 304.
        return etag_json(request, result, max_age=5)

    @router.get("/api/deliverables/{deliverable_id}/mission_status")
    def deliverable_mission_status(request: Request, deliverable_id: str, project: str = Query(...)):
        project = resolve_project(project)
        result = store.get_mission_status(project=project, deliverable_id=deliverable_id)
        if result.get("error"):
            code = 404 if "unknown" in result["error"] else 400
            raise HTTPException(code, result["error"])
        return etag_json(request, result, max_age=5)  # CONSOL-8: TTL+ETag poll parity

    @router.get("/api/deliverables/{deliverable_id}/dependency_graph")
    def deliverable_dependency_graph(request: Request, deliverable_id: str, project: str = Query(...)):
        # def (not async): threadpool the graph build so it can't block the event loop.
        project = resolve_project(project)
        result = store.get_deliverable_dependency_graph(project=project, deliverable_id=deliverable_id)
        if result.get("error"):
            code = 404 if "unknown" in result["error"] else 400
            raise HTTPException(code, result["error"])
        return etag_json(request, result, max_age=5)  # CONSOL-8: TTL+ETag poll parity

    @router.get("/api/deliverables/{deliverable_id}/autopilot")
    def deliverable_autopilot_status(deliverable_id: str, project: str = Query(...)):
        """Return durable task/deliverable scopes for one mission cockpit."""
        project = resolve_project(project)
        if not store.get_deliverable(deliverable_id, project=project):
            raise HTTPException(404, "unknown deliverable")
        scopes = store.list_autopilot_scopes(
            project=project, deliverable_id=deliverable_id,
            status="active,paused", limit=500)
        return {
            "schema": "switchboard.autopilot_scope_list.v1",
            "project_id": project,
            "deliverable_id": deliverable_id,
            "scopes": scopes,
        }

    @router.post("/api/deliverables/{deliverable_id}/autopilot")
    async def control_deliverable_autopilot(request: Request, deliverable_id: str,
                                            body: AutopilotControlBody,
                                            project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        action = body.action
        common = {
            "project": project,
            "profile_id": body.profile_id,
            "deliverable_id": deliverable_id,
            "scope_type": "deliverable",
            "actor": auth.actor(principal),
        }
        if action == "start":
            result = store.start_autopilot_scope(
                **common, runtime=body.runtime)
        else:
            result = store.control_autopilot_scope(**common, action=action)
        if result.get("error"):
            code = 404 if "unknown" in result["error"] or "not found" in result["error"] else 400
            raise HTTPException(code, result["error"])
        return result

    @router.post("/api/deliverables/{deliverable_id}/tasks/{task_id}/autopilot")
    async def control_task_autopilot(request: Request, deliverable_id: str, task_id: str,
                                     body: AutopilotControlBody,
                                     project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        action = body.action
        common = {
            "project": project,
            "profile_id": body.profile_id,
            "deliverable_id": deliverable_id,
            "scope_type": "task",
            "task_project": body.task_project or project,
            "task_id": task_id,
            "actor": auth.actor(principal),
        }
        if action == "start":
            result = store.start_autopilot_scope(
                **common, runtime=body.runtime)
        else:
            result = store.control_autopilot_scope(**common, action=action)
        if result.get("error"):
            code = 404 if "unknown" in result["error"] or "not found" in result["error"] else 400
            raise HTTPException(code, result["error"])
        return result

    @router.post("/api/deliverables/{deliverable_id}/closure_verify")
    async def verify_deliverable_closure_route(request: Request, deliverable_id: str,
                                               body: dict = Body(...), project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        payload = body or {}
        result = deliverable_closure.verify_and_record_closure(
            deliverable_id, project, actor=auth.actor(principal),
            report=payload.get("report"),
            submitted_functional=payload.get("submitted_functional"),
            waivers=payload.get("waivers"),
            generated_by=payload.get("generated_by") or auth.actor(principal))
        if isinstance(result, dict) and result.get("error"):
            code = 404 if "unknown deliverable" in result["error"] else 400
            raise HTTPException(code, result["error"])
        return result

    @router.get("/api/deliverables/{deliverable_id}/closure_report")
    def get_deliverable_closure_report_route(deliverable_id: str, report_id: str = "",
                                             project: str = Query(...)):
        project = resolve_project(project)
        result = store.get_deliverable_closure_report(
            deliverable_id, project=project, report_id=report_id)
        if result.get("error"):
            code = 404 if ("unknown" in result["error"] or "not found" in result["error"]) else 400
            raise HTTPException(code, result["error"])
        return result

    @router.post("/api/deliverables/{deliverable_id}/closure_request")
    async def request_deliverable_closure_verification_route(request: Request, deliverable_id: str,
                                                             body: dict = Body(default={}),
                                                             project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        payload = body or {}
        result = deliverable_closure.request_closure_verification(
            deliverable_id, project, agent_id=payload.get("agent_id") or "",
            actor=auth.actor(principal), waivers=payload.get("waivers"))
        if isinstance(result, dict) and result.get("error"):
            code = 404 if "not found" in result["error"] else 400
            raise HTTPException(code, result["error"])
        return result

    @router.post("/api/deliverables/{deliverable_id}/coordinator_tick")
    async def run_mission_coordinator_tick(request: Request, deliverable_id: str,
                                           project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        body = body or {}
        result = store.run_mission_coordinator_tick(
            project=project,
            deliverable_id=deliverable_id,
            board_id=body.get("board_id") or "",
            mission_id=body.get("mission_id") or "",
            coordinator_agent_id=body.get("coordinator_agent_id") or "",
            actor=auth.actor(principal),
            idem_key=body.get("idem_key") or "",
            policy=body.get("policy"),
        )
        if result.get("error"):
            code = 404 if "unknown" in result["error"] else 400
            raise HTTPException(code, result["error"])
        return result

    @router.post("/api/deliverables/{deliverable_id}/mission_brief")
    async def generate_mission_brief(request: Request, deliverable_id: str,
                                     project: str = Query(...),
                                     persist: bool = Query(True)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        result = store.generate_mission_brief(
            project=project, deliverable_id=deliverable_id,
            actor=auth.actor(principal), persist=persist)
        if result.get("error"):
            code = 404 if "unknown" in result["error"] else 400
            raise HTTPException(code, result["error"])
        return result

    @router.patch("/api/deliverables/{deliverable_id}/narrative")
    async def update_mission_narrative(request: Request, deliverable_id: str,
                                       body: dict = Body(...),
                                       project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        payload = body or {}
        result = store.update_mission_narrative(
            deliverable_id, payload.get("narrative") or "",
            actor=auth.actor(principal), project=project,
            append=bool(payload.get("append")))
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/api/deliverables/{deliverable_id}/breakdown_proposals")
    async def propose_deliverable_breakdown(request: Request, deliverable_id: str,
                                            body: dict = Body(...),
                                            project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        payload = body or {}
        result = store.propose_deliverable_breakdown(
            deliverable_id, payload, actor=auth.actor(principal), project=project,
            proposal_id=payload.get("proposal_id") or "")
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/api/deliverables/breakdown_proposals/{proposal_id}/approve")
    async def approve_deliverable_breakdown(request: Request, proposal_id: str,
                                            project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        result = store.approve_deliverable_breakdown(
            proposal_id, actor=auth.actor(principal), project=project)
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/api/deliverables/{deliverable_id}/outcome")
    async def submit_deliverable_outcome(request: Request, deliverable_id: str,
                                         body: dict = Body(...),
                                         project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        payload = body or {}
        result = store.submit_deliverable_outcome(
            deliverable_id, payload.get("outcome") or "",
            actor=auth.actor(principal), project=project,
            target_projects=payload.get("target_projects"),
            policy_constraints=payload.get("policy_constraints"),
            acceptance_criteria=payload.get("acceptance_criteria"),
            use_llm=bool(payload.get("use_llm")),
        )
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/api/deliverables/{deliverable_id}/archive")
    async def archive_deliverable_route(request: Request, deliverable_id: str,
                                        body: dict = Body(default={}),
                                        project: str = Query(...)):
        """Archive/restore, atomically transferring or stopping live Autopilot scope."""
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        archived = True if not body else bool(body.get("archived", True))
        result = store.archive_deliverable(
            deliverable_id, project=project, actor=auth.actor(principal), archived=archived,
            replacement_deliverable_id=str(
                body.get("replacement_deliverable_id") or "") if body else "",
            scope_transition_reason=str(
                body.get("scope_transition_reason") or "") if body else "",
        )
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.patch("/api/deliverables/breakdown_proposals/{proposal_id}")
    async def update_deliverable_breakdown_proposal(request: Request, proposal_id: str,
                                                    body: dict = Body(...),
                                                    project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        payload = body or {}
        result = store.update_deliverable_breakdown_proposal(
            proposal_id, payload, actor=auth.actor(principal), project=project,
            outcome_text=payload.get("outcome") or "")
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/api/deliverables/breakdown_proposals/{proposal_id}/reject")
    async def reject_deliverable_breakdown(request: Request, proposal_id: str,
                                           body: dict = Body(...),
                                           project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        payload = body or {}
        result = store.reject_deliverable_breakdown(
            proposal_id, payload.get("reason") or "",
            actor=auth.actor(principal), project=project)
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/api/deliverables/breakdown_proposals/{proposal_id}/defer")
    async def defer_deliverable_breakdown(request: Request, proposal_id: str,
                                          body: dict = Body(...),
                                          project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        payload = body or {}
        defer_until = payload.get("defer_until")
        if defer_until not in (None, ""):
            try:
                defer_until = float(defer_until)
            except (TypeError, ValueError):
                raise HTTPException(400, "defer_until must be a unix timestamp")
        result = store.defer_deliverable_breakdown(
            proposal_id, payload.get("reason") or "",
            actor=auth.actor(principal), project=project,
            defer_until=defer_until)
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    if sibling_bc_only:
        # ARCH-MS-111: the standalone service owns exactly these ADR-0014 GETs.
        # Register normally above so the source-level route inventory remains the
        # architecture contract, then selectively strip only the production mount.
        day_one_reads = {
            "/api/deliverables",
            "/api/deliverables/breakdown_proposals",
            "/api/deliverables/breakdown_proposals/{proposal_id}",
            "/api/deliverables/{deliverable_id}",
            "/api/mission_status",
            "/api/deliverables/{deliverable_id}/mission_status",
            "/api/deliverables/{deliverable_id}/dependency_graph",
            "/api/deliverables/{deliverable_id}/closure_report",
        }
        router.routes[:] = [
            route for route in router.routes
            if not (route.path in day_one_reads and "GET" in (route.methods or set()))
        ]

    return router
