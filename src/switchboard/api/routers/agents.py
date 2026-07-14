"""Agent registry REST routes (IXP register / presence / hosts).

Owns agent/host registry IXP routes (register, heartbeat, list, host_status,
control_plane_probe) while the composition root supplies project and principal
boundaries. Domain persistence stays behind application commands / store.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request

import auth
import store
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

    @router.post("/ixp/v1/heartbeat")
    async def ixp_heartbeat(request: Request, body: dict = Body(...)):
        body = dict(body or {})
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "agent")
        return store.heartbeat((body.get("agent_id") or "").strip(),
                               actor=auth.actor(principal), project=project)

    @router.get("/ixp/v1/agents")
    async def ixp_agents(project: str = Query(store.DEFAULT_PROJECT), lane: str = ""):
        return {"agents": store.list_active_agents(lane=lane, project=resolve_project(project))}

    @router.post("/ixp/v1/heartbeat_host")
    async def ixp_heartbeat_host(request: Request, body: dict = Body(...)):
        body = dict(body or {})
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("host_id") or "agent-host")
        return control_plane_http(store.heartbeat_host(
            (body.get("host_id") or "").strip(),
            active_sessions=body.get("active_sessions"),
            capacity=body.get("capacity") or {},
            status=body.get("status") or "online",
            last_error=body.get("last_error") or "",
            actor=auth.actor(principal), project=project))

    @router.get("/ixp/v1/agent_hosts")
    async def ixp_agent_hosts(project: str = Query(store.DEFAULT_PROJECT), runtime: str = "",
                              lane: str = "", capability: str = "",
                              include_stale: bool = False):
        hosts = store.list_agent_hosts(runtime=runtime, lane=lane,
                                       capability=capability,
                                       include_stale=include_stale,
                                       project=resolve_project(project))
        control_plane_http(hosts)
        return {"hosts": hosts}

    @router.get("/ixp/v1/control_plane_probe")
    async def ixp_control_plane_probe(project: str = Query(store.DEFAULT_PROJECT), lane: str = "",
                                      include_heavy: bool = False):
        from switchboard.application.queries.control_plane_probe import execute
        return execute(
            project=resolve_project(project), lane=lane, include_heavy=include_heavy)

    @router.get("/ixp/v1/host_status")
    async def ixp_host_status(host_id: str, project: str = Query(store.DEFAULT_PROJECT)):
        status = control_plane_http(store.host_status(host_id, project=resolve_project(project)))
        if status.get("error"):
            raise HTTPException(404, status["error"])
        return status

    return router
