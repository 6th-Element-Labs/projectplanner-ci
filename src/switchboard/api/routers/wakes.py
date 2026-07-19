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
from switchboard.api.deps import (
    is_narrow_agent_host_principal,
    require_agent_host_bootstrap_authority,
    require_direct_task_completion_authority,
    require_agent_host_identity,
    resolve_agent_host_principal,
)
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
        principal = resolve_agent_host_principal(
            resolve_principal, request, project,
            dev_actor=body.get("host_id") or "agent-host")
        require_agent_host_identity(
            principal, str(body.get("host_id") or ""), project)
        body["project"] = project
        return control_plane_http(raise_if_idem_conflict(
            claim_wake_command.execute_mapping_result(
                body, actor=auth.actor(principal), principal_id=principal["id"])))

    @router.post("/txp/v1/complete_wake")
    async def txp_complete_wake(request: Request, body: dict = Body(...)):
        body = inject_idem_key(request, body)
        project = resolve_body_project(body)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project,
            dev_actor=body.get("host_id") or body.get("agent_id") or "agent-host")
        if is_narrow_agent_host_principal(principal):
            wake_id = str(body.get("wake_id") or body.get("id") or "").strip()
            wake = next(iter(store.list_wake_intents(
                wake_id=wake_id, project=project, limit=1)), {})
            policy = dict(wake.get("policy") or {})
            execution = dict(policy.get("execution_binding") or {})
            personal_exact = (
                policy.get("execution_mode") == "personal_agent_host"
                or policy.get("require_exact_host_binding")
            )
            direct_task = (
                policy.get("mode") == "direct_task"
                and policy.get("execution_mode") == "direct_personal_cli"
                and policy.get("require_runner_bind") is False
            )
            if direct_task:
                selector = dict(wake.get("selector") or {})
                require_direct_task_completion_authority(
                    principal,
                    {
                        "wake_id": wake_id,
                        "host_id": str(selector.get("host_id") or ""),
                        "runner_session_id": str(
                            body.get("runner_session_id") or ""),
                        "task_id": str(
                            wake.get("task_id") or selector.get("task_id") or ""),
                        "agent_id": str(
                            body.get("agent_id") or selector.get("agent_id") or ""),
                    },
                    project,
                )
            elif personal_exact:
                if str(execution.get("host_principal_id") or "") \
                        != str(principal.get("id") or ""):
                    raise HTTPException(
                        403, "host bearer may complete only its exact personal wake")
            else:
                selector = dict(wake.get("selector") or {})
                require_agent_host_bootstrap_authority(
                    principal,
                    {
                        "wake_id": wake_id,
                        "host_id": str(wake.get("claimed_by_host") or ""),
                        "runner_session_id": str(
                            body.get("runner_session_id") or ""),
                        "task_id": str(
                            wake.get("task_id") or selector.get("task_id") or ""),
                        "agent_id": str(
                            body.get("agent_id") or selector.get("agent_id") or ""),
                    },
                    "complete_wake",
                    project,
                )
        body["project"] = project
        return control_plane_http(raise_if_idem_conflict(
            complete_wake_command.execute_mapping_result(
                body, actor=auth.actor(principal), principal_id=principal["id"])))

    @router.get("/txp/v1/list_wake_intents")
    async def txp_list_wake_intents(request: Request, project: str = Query(...),
                                    status: str = "", host_id: str = "",
                                    runtime: str = "", task_id: str = "",
                                    deliverable_id: str = "", history: bool = False,
                                    limit: int = Query(50, ge=1, le=200),
                                    before_requested_at: float | None = None,
                                    before_wake_id: str = ""):
        resolved = resolve_project(project)
        resolve_principal(
            request, resolved, ("read",),
            dev_actor=host_id or "agent-host")
        wakes = store.list_wake_intents(
            status=status, host_id=host_id, runtime=runtime, task_id=task_id,
            deliverable_id=deliverable_id, project=resolved,
            active_only=not history and not status, include_archived=history,
            limit=limit + 1, before_requested_at=before_requested_at,
            before_wake_id=before_wake_id, newest_first=True)
        control_plane_http(wakes)
        has_more = len(wakes) > limit
        wakes = wakes[:limit]
        last = wakes[-1] if wakes else {}
        return {"wake_intents": wakes, "page": {
            "limit": limit, "has_more": has_more,
            "next_before_requested_at": last.get("requested_at") if has_more else None,
            "next_before_wake_id": last.get("wake_id") if has_more else None,
        }}

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
    async def ixp_wake_intents(request: Request, project: str = Query(...),
                               status: str = "", host_id: str = "", runtime: str = "",
                               task_id: str = "", deliverable_id: str = "",
                               history: bool = False,
                               limit: int = Query(50, ge=1, le=200),
                               before_requested_at: float | None = None,
                               before_wake_id: str = ""):
        resolved = resolve_project(project)
        resolve_principal(
            request, resolved, ("read",),
            dev_actor=host_id or "switchboard/operator")
        wakes = store.list_wake_intents(
            status=status, host_id=host_id, runtime=runtime, task_id=task_id,
            deliverable_id=deliverable_id, project=resolved,
            active_only=not history and not status, include_archived=history,
            limit=limit + 1, before_requested_at=before_requested_at,
            before_wake_id=before_wake_id, newest_first=True)
        has_more = len(wakes) > limit
        wakes = wakes[:limit]
        last = wakes[-1] if wakes else {}
        return {"wake_intents": wakes, "page": {
            "limit": limit, "has_more": has_more,
            "next_before_requested_at": last.get("requested_at") if has_more else None,
            "next_before_wake_id": last.get("wake_id") if has_more else None,
        }}

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
