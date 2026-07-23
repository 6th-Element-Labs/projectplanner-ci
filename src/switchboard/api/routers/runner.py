"""Runner session / control REST routes (IXP).

Extracted in ARCH-MS-67. Composition root supplies project and principal
boundaries; shared runner_control commands own transport-neutral mapping.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

import auth
import store
from switchboard.api.deps import (
    is_narrow_agent_host_principal,
    require_agent_host_identity,
    require_agent_host_runner_identity,
    resolve_agent_host_principal,
)
from switchboard.application.commands import runner_control as runner_control_command


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
BodyProjectResolver = Callable[[dict], str]


class RunnerInjectBody(BaseModel):
    """Typed CO-13 inject request (keeps ARCH-MS-84 untyped-body ceiling flat)."""

    model_config = ConfigDict(extra="allow")

    project: Optional[str] = None
    runner_session_id: Optional[str] = None
    id: Optional[str] = None
    task_id: Optional[str] = None
    text: Optional[str] = None
    message: Optional[str] = None
    kind: Optional[str] = None
    reason: Optional[str] = None
    options: dict[str, Any] = Field(default_factory=dict)


class MintHostTunnelUrlBody(BaseModel):
    """Typed WATCH-7 host tunnel renewal request."""

    model_config = ConfigDict(extra="allow")

    project: Optional[str] = None
    runner_session_id: Optional[str] = None
    id: Optional[str] = None
    host_id: Optional[str] = None


class RunnerLeaseDueBody(BaseModel):
    """Typed capacity-plane request to make the canonical lease due."""

    model_config = ConfigDict(extra="allow")

    project: Optional[str] = None
    runner_session_id: Optional[str] = None
    host_id: Optional[str] = None
    reason: Optional[str] = None
    authority: Optional[str] = None


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  resolve_body_project: BodyProjectResolver) -> APIRouter:
    """Build the runner IXP router against shared trust boundaries."""
    router = APIRouter()

    @router.get("/ixp/v1/runner_sessions")
    async def ixp_runner_sessions(project: str = Query(...),
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
                                        project: str = Query(...),
                                        include_stale: bool = False):
        """Open gate for operator Watch/Chat (COORD-34). Fail closed on incomplete bind."""
        return runner_control_command.resolve_watch(
            task_id=task_id, include_stale=include_stale,
            project=resolve_project(project))

    @router.get("/ixp/v1/runner_sessions/{runner_session_id}/relay_attachment")
    async def ixp_runner_session_relay_attachment(runner_session_id: str):
        """WATCH-4: the live host-tunnel attachment state for one runner session.

        Served from this process's RelayHub, the authority for live attachment.
        ``host_attached`` is ``null`` when this process has never held the session
        (the caller should keep DB-row inference); ``true``/``false`` when the hub
        owns it. Cross-process watch resolvers read this to key liveness on the
        relay rather than on a runner row.
        """
        from switchboard.application import runner_pty_relay as relay
        return {
            "runner_session_id": runner_session_id,
            "host_attached": relay.host_attached_for(runner_session_id),
        }

    @router.post("/ixp/v1/register_runner_session")
    async def ixp_register_runner_session(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project,
            dev_actor=body.get("host_id") or body.get("agent_id") or "runner")
        require_agent_host_identity(
            principal, str(body.get("host_id") or ""), project)
        require_agent_host_runner_identity(
            principal, str(body.get("runner_session_id") or body.get("id") or ""),
            str(body.get("host_id") or ""), project)
        record = dict(body)
        record.pop("project", None)
        result = runner_control_command.upsert_session_mapping_result(
            {**record, "project": project},
            principal_id=principal["id"], actor=auth.actor(principal))
        if is_narrow_agent_host_principal(principal) and result.get("error"):
            raise HTTPException(403, result)
        return result

    @router.post("/ixp/v1/heartbeat_runner_session")
    async def ixp_heartbeat_runner_session(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project,
            dev_actor=body.get("host_id") or body.get("agent_id") or "runner")
        require_agent_host_identity(
            principal, str(body.get("host_id") or ""), project)
        require_agent_host_runner_identity(
            principal, str(body.get("runner_session_id") or body.get("id") or ""),
            str(body.get("host_id") or ""), project)
        record = dict(body)
        record.pop("project", None)
        result = runner_control_command.upsert_session_mapping_result(
            {**record, "project": project},
            principal_id=principal["id"], actor=auth.actor(principal))
        if is_narrow_agent_host_principal(principal) and result.get("error"):
            raise HTTPException(403, result)
        return result

    @router.post("/ixp/v1/mint_host_tunnel_url")
    async def ixp_mint_host_tunnel_url(
            request: Request, body: MintHostTunnelUrlBody):
        """Return a fresh host-tunnel URL to the bearer that owns the runner."""
        payload = body.model_dump(exclude_none=True)
        project = resolve_body_project(payload)
        host_id = str(payload.get("host_id") or "")
        runner_session_id = str(
            payload.get("runner_session_id") or payload.get("id") or "")
        principal = resolve_agent_host_principal(
            resolve_principal, request, project, dev_actor=host_id or "runner")
        require_agent_host_identity(principal, host_id, project)
        require_agent_host_runner_identity(
            principal, runner_session_id, host_id, project)
        result = runner_control_command.mint_host_tunnel_url_mapping_result(
            {
                "project": project,
                "runner_session_id": runner_session_id,
            },
            principal_id=principal["id"], actor=auth.actor(principal),
        )
        if result.get("error"):
            raise HTTPException(404, result)
        if (is_narrow_agent_host_principal(principal)
                and result.get("server_relay", {}).get("error")):
            raise HTTPException(403, result)
        return result

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

    @router.post("/ixp/v1/request_runner_inject")
    async def ixp_request_runner_inject(request: Request, body: RunnerInjectBody):
        payload = body.model_dump(exclude_none=True)
        options = dict(payload.get("options") or {})
        if payload.get("task_id") and "task_id" not in options:
            options["task_id"] = payload.get("task_id")
        if payload.get("text") is not None and "text" not in options:
            options["text"] = payload.get("text")
        elif payload.get("message") is not None and "text" not in options and "message" not in options:
            options["text"] = payload.get("message")
        if payload.get("kind") and "kind" not in options:
            options["kind"] = payload.get("kind")
        return _request_control(request, payload, "inject", options=options)

    @router.post("/ixp/v1/runner_lease_due")
    async def ixp_runner_lease_due(request: Request, body: RunnerLeaseDueBody):
        payload = body.model_dump(exclude_none=True)
        project = resolve_body_project(payload)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project,
            dev_actor=payload.get("host_id") or "agent-host")
        require_agent_host_identity(
            principal, str(payload.get("host_id") or ""), project)
        result = runner_control_command.make_lease_due_mapping_result(
            {
                "project": project,
                "runner_session_id": payload.get("runner_session_id"),
                "reason": payload.get("reason") or "",
                "authority": payload.get("authority") or "capacity_plane",
            },
            actor=auth.actor(principal),
        )
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.get("/ixp/v1/runner_controls")
    async def ixp_runner_controls(project: str = Query(...),
                                  status: str = "", host_id: str = "",
                                  runner_session_id: str = ""):
        return {"requests": runner_control_command.list_control_requests(
            status=status, host_id=host_id, runner_session_id=runner_session_id,
            project=resolve_project(project))}

    @router.post("/ixp/v1/claim_runner_control")
    async def ixp_claim_runner_control(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project,
            dev_actor=body.get("host_id") or "agent-host")
        require_agent_host_identity(
            principal, str(body.get("host_id") or ""), project)
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
        principal = resolve_agent_host_principal(
            resolve_principal, request, project,
            dev_actor=body.get("host_id") or "agent-host")
        require_agent_host_identity(
            principal, str(body.get("host_id") or ""), project)
        result = runner_control_command.complete_mapping_result(
            {
                "request_id": (body.get("request_id") or body.get("id") or "").strip(),
                "result": body.get("result") or {},
                "snapshot": body.get("snapshot") or {},
                "status": body.get("status") or "",
                "host_id": ((body.get("host_id") or "").strip()
                            if is_narrow_agent_host_principal(principal) else ""),
                "project": project,
            },
            actor=auth.actor(principal),
        )
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    return router
