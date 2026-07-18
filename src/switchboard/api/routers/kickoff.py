"""UI-30: kickoff record routes — server-side Scope gate approvals (advisory).

GET  /api/kickoff                 -> the five-gate state + build_authorized
POST /api/kickoff/{gate}/approve  -> approve the frontier gate (attributed)
POST /api/kickoff/{gate}/revise   -> bump an approved gate; downstream go stale

Reads need ``read``; writes need ``write:tasks`` — the same boundary the other
plan-shaping writes use. Nothing here enforces: build_authorized is published
for the UI (and for the later enforcement slice) but claim_next ignores it.

The record functions are injected by the composition root, so this module
imports the repository contract only (no ``store`` façade dependency).
"""
from __future__ import annotations

from typing import Any, Callable, Dict

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel

import auth
from switchboard.storage.repositories.kickoff import KickoffGateError

ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
GetStateFn = Callable[..., Dict[str, Any]]
GateWriteFn = Callable[..., Dict[str, Any]]


class GateDecision(BaseModel):
    """Optional note recorded with an approve/revise."""

    note: str = ""


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  get_state: GetStateFn,
                  approve_gate: GateWriteFn,
                  revise_gate: GateWriteFn) -> APIRouter:
    router = APIRouter()

    @router.get("/api/kickoff")
    async def api_kickoff_state(request: Request, project: str = Query(...)):
        proj = resolve_project(project)
        resolve_principal(request, proj, ("read",), dev_actor="web")
        return get_state(project=proj)

    @router.post("/api/kickoff/{gate}/approve")
    async def api_kickoff_approve(request: Request, gate: str,
                                  body: GateDecision = Body(default_factory=GateDecision),
                                  project: str = Query(...)):
        proj = resolve_project(project)
        principal = resolve_principal(request, proj, ("write:tasks",), dev_actor="web")
        try:
            return approve_gate(gate, actor=auth.actor(principal),
                                note=body.note, project=proj)
        except KickoffGateError as e:
            raise HTTPException(409, str(e))

    @router.post("/api/kickoff/{gate}/revise")
    async def api_kickoff_revise(request: Request, gate: str,
                                 body: GateDecision = Body(default_factory=GateDecision),
                                 project: str = Query(...)):
        proj = resolve_project(project)
        principal = resolve_principal(request, proj, ("write:tasks",), dev_actor="web")
        try:
            return revise_gate(gate, actor=auth.actor(principal),
                               note=body.note, project=proj)
        except KickoffGateError as e:
            raise HTTPException(409, str(e))

    return router
