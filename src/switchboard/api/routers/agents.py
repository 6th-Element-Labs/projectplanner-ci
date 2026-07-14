"""Agent registry REST routes (IXP register surface).

The router owns ``/ixp/v1/register_agent`` and ``/ixp/v1/register_host`` while
the composition root supplies project and principal boundaries. Domain
persistence stays behind application commands.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Body, Request

import auth
from switchboard.application.commands import register_agent as register_agent_command
from switchboard.application.commands import register_host as register_host_command


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
BodyProjectResolver = Callable[[dict], str]
ControlPlaneHttp = Callable[[Any], Any]


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  resolve_body_project: BodyProjectResolver,
                  control_plane_http: ControlPlaneHttp) -> APIRouter:
    """Build the agent registry IXP router against shared trust boundaries."""
    router = APIRouter()

    @router.post("/ixp/v1/register_agent")
    async def ixp_register_agent(request: Request, body: dict = Body(...)):
        body = dict(body or {})
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "agent")
        body.setdefault("agent_id", auth.actor(principal))
        body["project"] = project
        return register_agent_command.execute_mapping_result(
            body, actor=auth.actor(principal), principal_id=principal["id"])

    @router.post("/ixp/v1/register_host")
    async def ixp_register_host(request: Request, body: dict = Body(...)):
        body = dict(body or {})
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("host_id") or "agent-host")
        body["project"] = project
        return control_plane_http(register_host_command.execute_mapping_result(
            body, actor=auth.actor(principal), principal_id=principal["id"]))

    return router
