"""Messaging REST routes (operator API + IXP send/ack/inbox).

The router owns ``/api/agent_messages/send``, ``/api/agent_messages/ack``,
``/ixp/v1/send``, ``/ixp/v1/ack``, ``/ixp/v1/inbox``,
``/ixp/v1/message_status``, and ``/ixp/v1/pending_acks`` while the
composition root supplies project and principal boundaries. Domain
persistence stays behind application commands.
"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request

import auth
import store
from switchboard.api.idempotency import (
    inject_idem_key,
    raise_if_idem_conflict,
    run_with_idempotency,
)
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

    def _ack_with_idempotency(body: dict, *, actor: str) -> dict:
        idem_key = str(body.get("idem_key") or "").strip()
        payload = {
            "message_id": body.get("message_id") if "message_id" in body else body.get("id"),
            "response": body.get("response") or "",
            "project": body.get("project"),
        }
        if not (payload.get("project") or "").strip():
            raise HTTPException(400, "project required")
        cmd_body = {k: v for k, v in body.items() if k != "idem_key"}
        result, _replayed = run_with_idempotency(
            project=str(payload["project"]),
            operation="ack",
            actor=actor,
            idem_key=idem_key,
            payload=payload,
            execute=lambda: ack_message_command.execute_mapping_result(
                cmd_body, actor=actor),
        )
        return result


    @router.post("/api/agent_messages/send")
    async def api_send_agent_message(request: Request, body: dict = Body(...)):
        """Operator → live agent nudge/redirect."""
        body = inject_idem_key(request, body)
        project = resolve_project(
            request.query_params.get("project") or body.get("project"))
        principal = resolve_principal(
            request, project, ("write:tasks",), dev_actor="web")
        body["from_agent"] = auth.actor(principal)
        body["project"] = project
        result = raise_if_idem_conflict(send_agent_message_command.execute_mapping_result(
            body, principal_id=principal["id"]))
        if result.get("error_code") == "invalid_send_agent_message":
            raise HTTPException(400, result.get("error") or "invalid send payload")
        return result

    @router.post("/api/agent_messages/ack")
    async def api_ack_message(request: Request, body: dict = Body(...)):
        """Operator acks/dismisses a required message on the recipient's behalf."""
        body = inject_idem_key(request, body)
        project = resolve_project(
            request.query_params.get("project") or body.get("project"))
        principal = resolve_principal(
            request, project, ("write:tasks",), dev_actor="web")
        body["project"] = project
        result = raise_if_idem_conflict(
            _ack_with_idempotency(body, actor=auth.actor(principal)))
        if result.get("error_code") == "invalid_ack_message":
            raise HTTPException(400, result.get("error") or "invalid ack payload")
        return result

    @router.post("/ixp/v1/send")
    async def ixp_send(request: Request, body: dict = Body(...)):
        body = inject_idem_key(request, body)
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("from_agent") or "agent")
        # Preserve monolith semantics: empty/null from_agent falls back to actor.
        body["from_agent"] = body.get("from_agent") or auth.actor(principal)
        body["project"] = project
        return raise_if_idem_conflict(send_agent_message_command.execute_mapping_result(
            body, principal_id=principal["id"]))

    @router.post("/ixp/v1/ack")
    async def ixp_ack(request: Request, body: dict = Body(...)):
        body = inject_idem_key(request, body)
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",), dev_actor="agent")
        body["project"] = project
        return raise_if_idem_conflict(
            _ack_with_idempotency(body, actor=auth.actor(principal)))

    @router.get("/api/agent_messages/pending")
    async def api_pending_acks(request: Request, project: str = Query(...),
                               agent_id: str = ""):
        """The operator's ack inbox: required messages they are party to that are still
        unacked (defaults to the caller's own identity so it survives a reload)."""
        proj = resolve_project(project)
        principal = resolve_principal(request, proj, ("read",), dev_actor="web")
        return {"project": proj,
                "pending_acks": store.list_pending_acks(
                    agent_id=(agent_id or auth.actor(principal)), project=proj)}

    @router.get("/api/agent_messages/{message_id}/status")
    async def api_message_status(request: Request, message_id: int,
                                 project: str = Query(...)):
        """Poll one message to see whether the recipient has acked it (and delivery state)."""
        proj = resolve_project(project)
        resolve_principal(request, proj, ("read",), dev_actor="web")
        msg = store.get_message_status(message_id, project=proj)
        if not msg:
            raise HTTPException(404, "message not found")
        return msg

    @router.get("/ixp/v1/inbox")
    async def ixp_inbox(project: str = Query(...),
                        to_agent: str = "", unacked: bool = True, signal: str = ""):
        msgs = store.list_unacked_messages(to_agent, project=resolve_project(project)) if unacked else []
        if signal:
            msgs = [m for m in msgs if m.get("signal") == signal]
        return {"messages": msgs}

    @router.get("/ixp/v1/message_status")
    async def ixp_message_status(message_id: int, project: str = Query(...)):
        msg = store.get_message_status(message_id, project=resolve_project(project))
        if not msg:
            raise HTTPException(404, "message not found")
        return msg

    @router.get("/ixp/v1/pending_acks")
    async def ixp_pending_acks(project: str = Query(...), agent_id: str = ""):
        return {"pending_acks": store.list_pending_acks(
            agent_id=agent_id, project=resolve_project(project))}

    return router
