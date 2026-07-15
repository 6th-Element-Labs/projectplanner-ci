"""Runner session / control REST routes (IXP).

Extracted in ARCH-MS-67. Composition root supplies project and principal
boundaries; shared runner_control commands own transport-neutral mapping.
"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request

import auth
import store
from switchboard.application.commands import runner_control as runner_control_command


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
BodyProjectResolver = Callable[[dict], str]


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  resolve_body_project: BodyProjectResolver) -> APIRouter:
    """Build the runner IXP router against shared trust boundaries."""
    router = APIRouter()

    @router.get("/ixp/v1/runner_sessions")
    async def ixp_runner_sessions(project: str = Query(store.DEFAULT_PROJECT),
                                  host_id: str = "", runtime: str = "",
                                  task_id: str = "", status: str = "",
                                  include_stale: bool = False,
                                  for_watch: bool = False):
        project_id = resolve_project(project)
        if for_watch:
            # COORD-34 / UI-17: list alone is not enough — Watch/Chat opens only
            # through the typed bind gate.
            return runner_control_command.resolve_watch(
                task_id=task_id, include_stale=include_stale, project=project_id)
        return {"sessions": runner_control_command.list_sessions(
            host_id=host_id, runtime=runtime, task_id=task_id, status=status,
            include_stale=include_stale, project=project_id)}

    @router.get("/ixp/v1/runner_sessions/watch")
    async def ixp_runner_sessions_watch(task_id: str = Query(...),
                                        project: str = Query(store.DEFAULT_PROJECT),
                                        include_stale: bool = False):
        """Open gate for operator Watch/Chat (COORD-34). Fail closed on incomplete bind."""
        return runner_control_command.resolve_watch(
            task_id=task_id, include_stale=include_stale,
            project=resolve_project(project))

    @router.post("/ixp/v1/register_runner_session")
    async def ixp_register_runner_session(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("host_id") or body.get("agent_id") or "runner")
        record = dict(body)
        record.pop("project", None)
        return runner_control_command.upsert_session_mapping_result(
            {**record, "project": project},
            principal_id=principal["id"], actor=auth.actor(principal))

    @router.post("/ixp/v1/heartbeat_runner_session")
    async def ixp_heartbeat_runner_session(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("host_id") or body.get("agent_id") or "runner")
        record = dict(body)
        record.pop("project", None)
        return runner_control_command.upsert_session_mapping_result(
            {**record, "project": project},
            principal_id=principal["id"], actor=auth.actor(principal))

    def _request_control(request: Request, body: dict, action: str,
                         options: dict | None = None):
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor="switchboard/operator")
        result = runner_control_command.request_mapping_result(
            {
                "runner_session_id": body.get("runner_session_id") or body.get("id") or "",
                "action": action,
                "reason": body.get("reason") or "",
                "options": options if options is not None else (body.get("options") or {}),
                "project": project,
            },
            actor=auth.actor(principal),
            principal_id=principal["id"],
        )
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/ixp/v1/request_runner_snapshot")
    async def ixp_request_runner_snapshot(request: Request, body: dict = Body(...)):
        return _request_control(request, body, "snapshot")

    @router.post("/ixp/v1/request_runner_kill")
    async def ixp_request_runner_kill(request: Request, body: dict = Body(...)):
        return _request_control(
            request, body, "kill",
            options={"grace_seconds": body.get("grace_seconds"),
                     "signal": body.get("signal") or "TERM"})

    @router.post("/ixp/v1/request_runner_restart")
    async def ixp_request_runner_restart(request: Request, body: dict = Body(...)):
        return _request_control(request, body, "restart")

    @router.post("/ixp/v1/request_runner_health")
    async def ixp_request_runner_health(request: Request, body: dict = Body(...)):
        return _request_control(request, body, "health")

    @router.post("/ixp/v1/request_runner_logs")
    async def ixp_request_runner_logs(request: Request, body: dict = Body(...)):
        return _request_control(request, body, "logs")

    @router.post("/ixp/v1/request_runner_open")
    async def ixp_request_runner_open(request: Request, body: dict = Body(...)):
        return _request_control(request, body, "open")

    @router.get("/ixp/v1/runner_controls")
    async def ixp_runner_controls(project: str = Query(store.DEFAULT_PROJECT),
                                  status: str = "", host_id: str = "",
                                  runner_session_id: str = ""):
        return {"requests": runner_control_command.list_control_requests(
            status=status, host_id=host_id, runner_session_id=runner_session_id,
            project=resolve_project(project))}

    @router.post("/ixp/v1/claim_runner_control")
    async def ixp_claim_runner_control(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("host_id") or "agent-host")
        result = runner_control_command.claim_mapping_result(
            {
                "host_id": (body.get("host_id") or "").strip(),
                "request_id": (body.get("request_id") or body.get("id") or "").strip(),
                "project": project,
            },
            actor=auth.actor(principal),
        )
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/ixp/v1/complete_runner_control")
    async def ixp_complete_runner_control(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("host_id") or "agent-host")
        result = runner_control_command.complete_mapping_result(
            {
                "request_id": (body.get("request_id") or body.get("id") or "").strip(),
                "result": body.get("result") or {},
                "snapshot": body.get("snapshot") or {},
                "status": body.get("status") or "",
                "project": project,
            },
            actor=auth.actor(principal),
        )
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    return router
