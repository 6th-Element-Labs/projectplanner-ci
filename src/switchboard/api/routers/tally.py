"""Tally / outcomes / KPI REST routes (ARCH-MS-51).

Owns ``/tally/v1/*`` while the composition root supplies project/principal
boundaries. Persistence stays on the store façade / kpis_economics repository.
"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request

import auth
import store


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
BodyProjectResolver = Callable[[dict], str]


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  resolve_body_project: BodyProjectResolver) -> APIRouter:
    """Build the tally router against shared trust boundaries."""
    router = APIRouter()

    @router.post("/tally/v1/spend/ingest")
    async def tally_spend_ingest(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:ixp",), dev_actor=body.get("agent_id") or "tally")
        return store.report_usage(
            source=body.get("source") or "agent_report",
            confidence=body.get("confidence") or "reported",
            task_id=body.get("task_id"), claim_id=body.get("claim_id"),
            outcome_id=body.get("outcome_id"), agent_id=body.get("agent_id"),
            principal_id=principal["id"], runtime=body.get("runtime") or "",
            call_site=body.get("call_site") or "", provider=body.get("provider") or "",
            model=body.get("model") or "", prompt_tokens=int(body.get("prompt_tokens") or 0),
            completion_tokens=int(body.get("completion_tokens") or 0),
            total_tokens=body.get("total_tokens"), cost_usd=float(body.get("cost_usd") or 0.0),
            latency_ms=body.get("latency_ms"), status=body.get("status") or "ok",
            metadata=body.get("metadata") or {}, request_id=body.get("request_id"),
            project=project)

    @router.put("/tally/v1/spend/envelope")
    async def tally_set_spend_envelope(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:ixp",), dev_actor="tally")
        return store.set_spend_envelope(
            principal_id=principal["id"], daily_limit_usd=body.get("daily_limit_usd"),
            monthly_limit_usd=body.get("monthly_limit_usd"), project=project)

    @router.post("/tally/v1/spend/reservations")
    async def tally_reserve_spend(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:ixp",), dev_actor="tally")
        return store.reserve_spend(
            principal_id=principal["id"], request_id=body.get("request_id") or "",
            worst_case_cost_usd=body.get("worst_case_cost_usd"),
            metadata=body.get("metadata") or {}, project=project)

    @router.post("/tally/v1/spend/reservations/{request_id}/reconcile")
    async def tally_reconcile_spend(request_id: str, request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:ixp",), dev_actor="tally")
        return store.reconcile_spend(
            principal_id=principal["id"], request_id=request_id,
            actual_cost_usd=body.get("actual_cost_usd"), provider=body.get("provider") or "",
            model=body.get("model") or "", prompt_tokens=int(body.get("prompt_tokens") or 0),
            completion_tokens=int(body.get("completion_tokens") or 0),
            metadata=body.get("metadata") or {}, project=project)


    @router.post("/tally/v1/outcomes")
    async def tally_record_outcome(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:ixp",),
                               dev_actor=body.get("actor") or body.get("agent_id") or "tally")
        return store.record_outcome(
            outcome_type=body.get("type") or body.get("outcome_type") or "",
            title=body.get("title") or "",
            task_id=body.get("task_id") or body.get("task"),
            claim_id=body.get("claim_id"),
            epic_id=body.get("epic_id") or body.get("epic"),
            status=body.get("status") or "proposed",
            verifier=body.get("verifier") or "",
            verification=body.get("verification") or "",
            evidence=body.get("evidence") or {},
            value=body.get("value") or {},
            actor=auth.actor(principal),
            project=project)


    @router.post("/tally/v1/outcomes/{outcome_id}/verify")
    async def tally_verify_outcome(outcome_id: str, request: Request, body: dict = Body(default={})):
        project = resolve_body_project(body or {})
        principal = resolve_principal(request, project, ("write:ixp",), dev_actor="tally")
        return store.verify_outcome(
            outcome_id,
            verifier=body.get("verifier") or auth.actor(principal),
            verification=body.get("verification") or "",
            evidence=body.get("evidence") or {},
            actor=auth.actor(principal),
            project=project)


    @router.post("/tally/v1/outcomes/{outcome_id}/reject")
    async def tally_reject_outcome(outcome_id: str, request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:ixp",), dev_actor="tally")
        return store.reject_outcome(
            outcome_id,
            verifier=body.get("verifier") or auth.actor(principal),
            reason=body.get("reason") or "rejected",
            actor=auth.actor(principal),
            project=project)


    @router.post("/tally/v1/kpis")
    async def tally_create_kpi(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:ixp",), dev_actor="tally")
        return store.create_kpi(
            name=body.get("name") or "",
            unit=body.get("unit") or "",
            direction=body.get("direction") or "",
            owner=body.get("owner") or "",
            baseline_value=body.get("baseline_value"),
            current_value=body.get("current_value"),
            target_value=body.get("target_value"),
            period=body.get("period") or "",
            actor=auth.actor(principal),
            project=project)


    @router.patch("/tally/v1/kpis/{kpi_id}")
    async def tally_update_kpi(kpi_id: str, request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:ixp",), dev_actor="tally")
        if body.get("current_value") is None:
            raise HTTPException(400, "current_value is required")
        return store.update_kpi_value(
            kpi_id,
            current_value=float(body.get("current_value")),
            evidence=body.get("evidence") or {},
            actor=auth.actor(principal),
            project=project)


    @router.post("/tally/v1/outcome_kpi_links")
    async def tally_link_outcome_kpi(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:ixp",), dev_actor="tally")
        return store.link_outcome_to_kpi(
            outcome_id=body.get("outcome_id") or "",
            kpi_id=body.get("kpi_id") or "",
            contribution=body.get("contribution"),
            contribution_unit=body.get("contribution_unit") or "",
            confidence=body.get("confidence") or "directional",
            rationale=body.get("rationale") or "",
            actor=auth.actor(principal),
            project=project)


    @router.get("/tally/v1/kpis")
    async def tally_list_kpis(project: str = Query(...)):
        return {"kpis": store.list_kpis(project=resolve_project(project))}


    @router.get("/tally/v1/outcomes")
    async def tally_list_outcomes(project: str = Query(...),
                                  status: str = Query(""), limit: int = Query(200)):
        return {"outcomes": store.list_outcomes(project=resolve_project(project),
                                                status=status, limit=limit)}


    @router.get("/tally/v1/task/{task_id}")
    async def tally_task(task_id: str, project: str = Query(...)):
        return store.task_tally(task_id, project=resolve_project(project))


    @router.get("/tally/v1/kpi/{kpi_id}")
    async def tally_kpi(kpi_id: str, project: str = Query(...)):
        return store.kpi_tally(kpi_id, project=resolve_project(project))


    @router.get("/tally/v1/project")
    async def tally_project(project: str = Query(...)):
        return store.project_tally(project=resolve_project(project))


    @router.get("/tally/v1/deliverable/{deliverable_id}")
    async def tally_deliverable(deliverable_id: str, project: str = Query(...)):
        result = store.deliverable_tally(deliverable_id, project=resolve_project(project))
        if result.get("error"):
            raise HTTPException(404, result["error"])
        return result

    return router
