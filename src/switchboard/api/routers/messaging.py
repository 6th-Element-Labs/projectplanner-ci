"""Messaging REST routes (operator API + IXP send/ack).

The router owns ``/api/agent_messages/send``, ``/api/agent_messages/ack``,
``/ixp/v1/send``, and ``/ixp/v1/ack`` while the composition root supplies
project and principal boundaries. Domain persistence stays behind application
commands.
"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Request

import auth
from switchboard.application.commands import ack_message as ack_message_command
from switchboard.application.commands import send_agent_message as send_agent_message_command


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
BodyProjectResolver = Callable[[dict], str]


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  resolve_body_project: BodyProjectResolver) -> APIRouter:
    """Build messaging routes against the monolith's shared trust boundaries."""
    router = APIRouter()

    @router.post("/api/agent_messages/send")
    async def api_send_agent_message(request: Request, body: dict = Body(...)):
        """Operator → live agent nudge/redirect."""
        body = dict(body or {})
        project = resolve_project(
            request.query_params.get("project") or body.get("project") or "maxwell")
        principal = resolve_principal(
            request, project, ("write:tasks",), dev_actor="web")
        body["from_agent"] = auth.actor(principal)
        body["project"] = project
        result = send_agent_message_command.execute_mapping_result(
            body, principal_id=principal["id"])
        if result.get("error_code") == "invalid_send_agent_message":
            raise HTTPException(400, result.get("error") or "invalid send payload")
        return result

    @router.post("/api/agent_messages/ack")
    async def api_ack_message(request: Request, body: dict = Body(...)):
        """Operator acks/dismisses a required message on the recipient's behalf."""
        body = dict(body or {})
        project = resolve_project(
            request.query_params.get("project") or body.get("project") or "maxwell")
        principal = resolve_principal(
            request, project, ("write:tasks",), dev_actor="web")
        body["project"] = project
        result = ack_message_command.execute_mapping_result(
            body, actor=auth.actor(principal))
        if result.get("error_code") == "invalid_ack_message":
            raise HTTPException(400, result.get("error") or "invalid ack payload")
        return result

    @router.post("/ixp/v1/send")
    async def ixp_send(request: Request, body: dict = Body(...)):
        body = dict(body or {})
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("from_agent") or "agent")
        # Preserve monolith semantics: empty/null from_agent falls back to actor.
        body["from_agent"] = body.get("from_agent") or auth.actor(principal)
        body["project"] = project
        return send_agent_message_command.execute_mapping_result(
            body, principal_id=principal["id"])

    @router.post("/ixp/v1/ack")
    async def ixp_ack(request: Request, body: dict = Body(...)):
        body = dict(body or {})
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",), dev_actor="agent")
        body["project"] = project
        return ack_message_command.execute_mapping_result(
            body, actor=auth.actor(principal))

    return router
