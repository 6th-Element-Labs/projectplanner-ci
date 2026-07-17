"""Coordination monitor / reconcile / working-agreement / background-job REST
routes (IXP ops surface).

Owns ``/ixp/v1/monitors``, ``/ixp/v1/sweep_monitors``,
``/ixp/v1/reconcile_alerts``, ``/ixp/v1/resolve_monitor``,
``/ixp/v1/cancel_monitor``, ``/ixp/v1/delta``, ``/ixp/v1/working_agreement``,
``/ixp/v1/bugs/submit``, ``/ixp/v1/reconcile``, and the
``/ixp/v1/background_jobs*`` family while the composition root supplies
project and principal boundaries. Domain persistence stays behind ``store``.
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
                  resolve_body_project: BodyProjectResolver,
                  omit_coord_delta: bool = False) -> APIRouter:
    """Build the monitor/reconcile/background-job router against shared trust boundaries."""
    router = APIRouter()

    @router.get("/ixp/v1/monitors")
    async def ixp_monitors(project: str = Query(...), status: str = "",
                           kind: str = "", task_id: str = ""):
        return {"monitors": store.list_coordination_monitors(status=status, kind=kind,
                                                             task_id=task_id,
                                                             project=resolve_project(project))}

    @router.post("/ixp/v1/sweep_monitors")
    async def ixp_sweep_monitors(request: Request, body: dict = Body(default={})):
        project = resolve_body_project(body or {})
        resolve_principal(request, project, ("write:ixp",), dev_actor="switchboard/monitor")
        return store.sweep_coordination_monitors(project=project)

    @router.post("/ixp/v1/reconcile_alerts")
    async def ixp_reconcile_alerts(request: Request, body: dict = Body(default={})):
        project = resolve_body_project(body or {})
        resolve_principal(request, project, ("write:ixp",), dev_actor="switchboard/reconcile")
        return store.run_reconcile_alerts(
            project=project,
            alert_to=body.get("alert_to") or "switchboard/operator",
            min_severity=body.get("min_severity") or "medium")

    @router.post("/ixp/v1/resolve_monitor")
    async def ixp_resolve_monitor(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:ixp",), dev_actor="switchboard/monitor")
        return store.resolve_monitor(body.get("monitor_id") or body.get("id") or "",
                                     reason=body.get("reason") or "manual",
                                     actor=auth.actor(principal), project=project)

    @router.post("/ixp/v1/cancel_monitor")
    async def ixp_cancel_monitor(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:ixp",), dev_actor="switchboard/monitor")
        return store.cancel_monitor(body.get("monitor_id") or body.get("id") or "",
                                    reason=body.get("reason") or "cancelled",
                                    actor=auth.actor(principal), project=project)

    if not omit_coord_delta:
        @router.get("/ixp/v1/delta")
        async def ixp_delta(request: Request, project: str = Query(...), lane: str = "",
                            since_cursor: int = 0):
            # Delta carries its project in the query and enforces Auth here (BUG-73).
            proj = resolve_project(project)
            resolve_principal(request, proj, ("read",), dev_actor="agent")
            return store.get_activity_delta(since_cursor=since_cursor, lane=lane,
                                            project=proj)

    @router.get("/ixp/v1/working_agreement")
    async def ixp_working_agreement(project: str = Query(...)):
        from switchboard.application.queries.working_agreement import execute
        return execute(project=resolve_project(project))

    @router.post("/ixp/v1/bugs/submit")
    async def ixp_submit_bug(request: Request, body: dict = Body(...)):
        from switchboard.application.commands.submit_bug import execute_mapping_result
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:bug_intake",),
                                      dev_actor=body.get("source_agent") or "bug-intake")
        result = execute_mapping_result(
            body,
            actor=auth.actor(principal),
            project=project,
        )
        if result.get("error"):
            raise HTTPException(400, result)
        return result

    @router.get("/ixp/v1/reconcile")
    async def ixp_reconcile(project: str = Query(...)):
        return store.reconcile(project=resolve_project(project))

    @router.get("/ixp/v1/background_jobs")
    async def ixp_list_background_jobs():
        return store.list_background_jobs()

    @router.get("/ixp/v1/background_jobs/runs")
    async def ixp_list_background_job_runs(project: str = Query(...),
                                         job_name: str = Query(""),
                                         limit: int = Query(20, ge=1, le=200)):
        return store.list_background_job_runs(
            project=resolve_project(project), job_name=job_name, limit=limit)

    @router.get("/ixp/v1/background_jobs/runs/{run_id}")
    async def ixp_get_background_job_run(run_id: str,
                                         project: str = Query(...)):
        result = store.get_background_job_run(project=resolve_project(project), run_id=run_id)
        if result.get("error") == "run_not_found":
            raise HTTPException(404, result["error"])
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/ixp/v1/background_jobs/{job_name}/run")
    async def ixp_run_background_job(job_name: str, request: Request,
                                     project: str = Query(...)):
        body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
        if not isinstance(body, dict):
            raise HTTPException(400, "JSON object required")
        try:
            import background_jobs
            return store.run_background_job(
                project=resolve_project(project),
                job_name=job_name,
                run_id=str(body.get("run_id") or ""),
                resume=bool(body.get("resume", True)),
                params=body.get("params") if isinstance(body.get("params"), dict) else body,
                actor=str(body.get("actor") or "api/background_job"),
            )
        except background_jobs.JobBoundaryError as exc:
            raise HTTPException(400, str(exc)) from exc

    return router
