"""Claim REST routes (TXP claim surface).

The router owns ``/txp/v1/claim_next``, ``/txp/v1/claim_task``, and
``/txp/v1/complete_claim`` while the composition root supplies project and
principal boundaries. Domain persistence stays behind application commands.
"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, Request

import auth
from switchboard.application.commands import claim_next as claim_next_command
from switchboard.application.commands import claim_task as claim_task_command
from switchboard.application.commands import complete_claim as complete_claim_command


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
BodyProjectResolver = Callable[[dict], str]


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  resolve_body_project: BodyProjectResolver) -> APIRouter:
    """Build the claim TXP router against the monolith's shared trust boundaries."""
    router = APIRouter()

    @router.post("/txp/v1/claim_next")
    async def txp_claim_next(request: Request, body: dict = Body(...)):
        body = dict(body or {})
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "agent")
        body.setdefault("agent_id", auth.actor(principal))
        body["project"] = project
        if "work_session" not in body and body.get("work_session_json"):
            body["work_session"] = body.get("work_session_json")
        return claim_next_command.execute_mapping_result(
            body, actor=auth.actor(principal), principal_id=principal["id"])

    @router.post("/txp/v1/claim_task")
    async def txp_claim_task(request: Request, body: dict = Body(...)):
        body = dict(body or {})
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "agent")
        body.setdefault("agent_id", auth.actor(principal))
        body["project"] = project
        if "work_session" not in body and body.get("work_session_json"):
            body["work_session"] = body.get("work_session_json")
        return claim_task_command.execute_mapping_result(
            body, actor=auth.actor(principal), principal_id=principal["id"])

    @router.post("/txp/v1/complete_claim")
    async def txp_complete_claim(request: Request, body: dict = Body(...)):
        body = dict(body or {})
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",), dev_actor="agent")
        body["project"] = project
        return complete_claim_command.execute_mapping_result(
            body, actor=auth.actor(principal))

    return router
