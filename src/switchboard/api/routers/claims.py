"""Claim REST routes (TXP claim surface).

The router owns ``/txp/v1/claim_next``, ``/txp/v1/claim_task``, and
``/txp/v1/complete_claim`` while the composition root supplies project and
principal boundaries. Domain persistence stays behind application commands.
"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Request

import auth
import store
from switchboard.api.deps import (
    is_narrow_agent_host_principal,
    require_personal_execution_authority,
    resolve_agent_host_principal,
)
from switchboard.api.idempotency import inject_idem_key, raise_if_idem_conflict
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
        body = inject_idem_key(request, body)
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "agent")
        body.setdefault("agent_id", auth.actor(principal))
        body["project"] = project
        if "work_session" not in body and body.get("work_session_json"):
            body["work_session"] = body.get("work_session_json")
        return raise_if_idem_conflict(claim_next_command.execute_mapping_result(
            body, actor=auth.actor(principal), principal_id=principal["id"]))

    @router.post("/txp/v1/claim_task")
    async def txp_claim_task(request: Request, body: dict = Body(...)):
        body = inject_idem_key(request, body)
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "agent")
        body.setdefault("agent_id", auth.actor(principal))
        body["project"] = project
        if "work_session" not in body and body.get("work_session_json"):
            body["work_session"] = body.get("work_session_json")
        return raise_if_idem_conflict(claim_task_command.execute_mapping_result(
            body, actor=auth.actor(principal), principal_id=principal["id"]))

    @router.post("/txp/v1/complete_claim")
    async def txp_complete_claim(request: Request, body: dict = Body(...)):
        body = inject_idem_key(request, body)
        project = resolve_body_project(body)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project, dev_actor="agent")
        binding = body.pop("personal_execution_binding", {}) or {}
        if (is_narrow_agent_host_principal(principal)
                and str(body.get("claim_id") or "")
                != str(binding.get("claim_id") or "")):
            raise HTTPException(403, "personal execution claim target mismatch")
        require_personal_execution_authority(
            principal, binding, "complete_claim", project)
        body["project"] = project
        return raise_if_idem_conflict(complete_claim_command.execute_mapping_result(
            body, actor=auth.actor(principal)))

    @router.post("/txp/v1/abandon_claim")
    async def txp_abandon_claim(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project, dev_actor="agent")
        binding = body.pop("personal_execution_binding", {}) or {}
        if (is_narrow_agent_host_principal(principal)
                and str(body.get("claim_id") or "")
                != str(binding.get("claim_id") or "")):
            raise HTTPException(403, "personal execution claim target mismatch")
        require_personal_execution_authority(
            principal, binding, "abandon_claim", project)
        return store.abandon_claim(body.get("claim_id") or "", reason=body.get("reason") or "unspecified",
                                   actor=auth.actor(principal), project=project)

    @router.post("/txp/v1/revoke_claim")
    async def txp_revoke_claim(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:ixp",),
                                       dev_actor=body.get("operator_agent") or "switchboard/operator")
        sort_order = body.get("sort_order")
        try:
            sort_order_value = int(sort_order) if sort_order not in (None, "") else None
        except (TypeError, ValueError):
            raise HTTPException(400, "sort_order must be an integer")
        return store.revoke_claim(
            body.get("claim_id") or "",
            reason=body.get("reason") or "operator override",
            reassign_to=body.get("reassign_to") or body.get("reassigned_to") or "",
            sort_order=sort_order_value,
            partial_evidence=body.get("partial_evidence") or body.get("evidence") or {},
            notify=body.get("notify") is not False,
            ack_deadline_minutes=float(body.get("ack_deadline_minutes") or 5),
            actor=auth.actor(principal),
            project=project,
        )

    return router
