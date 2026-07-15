"""Resource-lease REST routes (IXP claim/check/release/leases).

Owns ``/ixp/v1/claim``, ``/ixp/v1/check``, ``/ixp/v1/release``, and
``/ixp/v1/leases`` while the composition root supplies project and principal
boundaries. Domain persistence stays behind ``store``'s lease primitives.
"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, Query, Request

import auth
import store


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
BodyProjectResolver = Callable[[dict], str]


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  resolve_body_project: BodyProjectResolver) -> APIRouter:
    """Build the resource-lease router against the monolith's shared trust boundaries."""
    router = APIRouter()

    @router.post("/ixp/v1/claim")
    async def ixp_claim(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:ixp",),
                                      dev_actor=body.get("agent_id") or "agent")
        names = body.get("names") or body.get("files") or []
        if isinstance(names, str):
            names = [x.strip() for x in names.replace("\n", ",").split(",") if x.strip()]
        return store.claim_resources(
            agent_id=(body.get("agent_id") or auth.actor(principal)).strip(),
            resource_type=(body.get("resource_type") or "file").strip(),
            names=names, task_id=body.get("task") or body.get("task_id"),
            ttl_seconds=int(body.get("ttl_s") or body.get("ttl_seconds") or
                            (int(body.get("ttl_min") or 30) * 60)),
            principal_id=principal["id"], actor=auth.actor(principal),
            idem_key=body.get("idem_key") or "", project=project)

    @router.post("/ixp/v1/check")
    async def ixp_check(body: dict = Body(...)):
        project = resolve_body_project(body)
        names = body.get("names") or body.get("files") or []
        if isinstance(names, str):
            names = [x.strip() for x in names.replace("\n", ",").split(",") if x.strip()]
        return {"held": store.check_resources((body.get("resource_type") or "file").strip(),
                                               names, project=project)}

    @router.post("/ixp/v1/release")
    async def ixp_release(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:ixp",), dev_actor="agent")
        return store.release_resource_lease((body.get("lease_id") or "").strip(),
                                            actor=auth.actor(principal), project=project)

    @router.get("/ixp/v1/leases")
    async def ixp_leases(project: str = Query(...)):
        return {"leases": store.list_active_resource_leases(project=resolve_project(project))}

    return router
