"""Wake REST routes (TXP + IXP wake surface).

The router owns ``/txp/v1/request_wake``, ``/txp/v1/claim_wake``,
``/txp/v1/complete_wake``, ``/txp/v1/list_wake_intents``,
``/txp/v1/cancel_wake``, and their IXP fleet-control mirrors
(``/ixp/v1/wake_intents``, ``/ixp/v1/request_wake``, ``/ixp/v1/cancel_wake``)
while the composition root supplies project and principal boundaries. Domain
persistence stays behind application commands.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request

import auth
import store
from switchboard.api.idempotency import inject_idem_key, raise_if_idem_conflict
from switchboard.application.commands import claim_wake as claim_wake_command
from switchboard.application.commands import complete_wake as complete_wake_command
from switchboard.application.commands import request_wake as request_wake_command


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
BodyProjectResolver = Callable[[dict], str]
ControlPlaneHttp = Callable[[Any], Any]


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  resolve_body_project: BodyProjectResolver,
                  control_plane_http: ControlPlaneHttp) -> APIRouter:
    """Build the wake TXP router against the monolith's shared trust boundaries."""
    router = APIRouter()

    @router.post("/txp/v1/request_wake")
    async def txp_request_wake(request: Request, body: dict = Body(...)):
        body = inject_idem_key(request, body)
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("source") or "agent")
        body["project"] = project
        if not body.get("source"):
            body["source"] = auth.actor(principal)
        if not body.get("task_id") and body.get("task"):
            body["task_id"] = body.get("task")
        return control_plane_http(raise_if_idem_conflict(
            request_wake_command.execute_mapping_result(
                body, actor=auth.actor(principal), principal_id=principal["id"])))

    @router.post("/txp/v1/claim_wake")
    async def txp_claim_wake(request: Request, body: dict = Body(...)):
        body = inject_idem_key(request, body)
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("host_id") or "agent-host")
        body["project"] = project
        return control_plane_http(raise_if_idem_conflict(
            claim_wake_command.execute_mapping_result(
                body, actor=auth.actor(principal))))

    @router.post("/txp/v1/complete_wake")
    async def txp_complete_wake(request: Request, body: dict = Body(...)):
        body = inject_idem_key(request, body)
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("host_id") or body.get("agent_id") or "agent-host")
        body["project"] = project
        return control_plane_http(raise_if_idem_conflict(
            complete_wake_command.execute_mapping_result(
                body, actor=auth.actor(principal))))

    @router.get("/txp/v1/list_wake_intents")
    async def txp_list_wake_intents(project: str = Query(store.DEFAULT_PROJECT),
                                    status: str = "", host_id: str = "",
                                    runtime: str = ""):
        wakes = store.list_wake_intents(status=status, host_id=host_id,
                                        runtime=runtime, project=resolve_project(project))
        control_plane_http(wakes)
        return {"wake_intents": wakes}

    @router.post("/txp/v1/cancel_wake")
    async def txp_cancel_wake(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:ixp",), dev_actor="agent")
        return control_plane_http(store.cancel_wake(
            (body.get("wake_id") or body.get("id") or "").strip(),
            reason=body.get("reason") or "cancelled",
            actor=auth.actor(principal), project=project))

    # UI-8 Fleet control: wake-intent read/write over REST (hosts + runners already have
    # their routes above). Mirrors the request_wake / list_wake_intents / cancel_wake tools.
    @router.get("/ixp/v1/wake_intents")
    async def ixp_wake_intents(project: str = Query(store.DEFAULT_PROJECT),
                               status: str = "", host_id: str = "", runtime: str = ""):
        return {"wake_intents": store.list_wake_intents(
            status=status, host_id=host_id, runtime=runtime, project=resolve_project(project))}

    @router.post("/ixp/v1/request_wake")
    async def ixp_request_wake(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:ixp",), dev_actor="switchboard/operator")
        payload = dict(body or {})
        payload["project"] = project
        if not payload.get("source"):
            payload["source"] = auth.actor(principal)
        result = request_wake_command.execute_mapping_result(
            payload, actor=auth.actor(principal), principal_id=principal["id"])
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/ixp/v1/cancel_wake")
    async def ixp_cancel_wake(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:ixp",), dev_actor="switchboard/operator")
        result = store.cancel_wake(
            (body.get("wake_id") or "").strip(), reason=body.get("reason") or "cancelled",
            actor=auth.actor(principal), project=project)
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    return router
