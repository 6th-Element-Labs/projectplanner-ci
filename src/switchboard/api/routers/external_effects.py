"""External effects / CI / publication / merge_gate REST routes (IXP).

Extracted in ARCH-MS-67. Shared commands own effect claim/lifecycle and
merge_gate mapping; CI mirror + publication stay behind existing modules.
"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request

import auth
import external_ci_mirror
import store
from switchboard.application.commands import claim_external_effect as effect_command
from switchboard.application.commands import merge_gate as merge_gate_command


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
BodyProjectResolver = Callable[[dict], str]


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  resolve_body_project: BodyProjectResolver) -> APIRouter:
    """Build the external-effects / CI IXP router against shared trust boundaries."""
    router = APIRouter()

    @router.get("/ixp/v1/external_effects")
    async def ixp_external_effects(project: str = Query(...),
                                   effect_type: str = "", status: str = "",
                                   task_id: str = "", target: str = ""):
        return {"effects": effect_command.list_mapping_result(
            effect_type=effect_type, status=status, task_id=task_id,
            target=target, project=resolve_project(project))}

    @router.get("/ixp/v1/external_ci_runs")
    async def ixp_external_ci_runs(project: str = Query(...),
                                   task_id: str = "", source_project: str = "",
                                   source_sha: str = "", status: str = ""):
        return {"runs": store.list_external_ci_runs(
            task_id=task_id, source_project=source_project,
            source_sha=source_sha, status=status, project=resolve_project(project))}

    @router.get("/ixp/v1/external_ci_runs/{run_id}")
    async def ixp_external_ci_run(run_id: str,
                                  project: str = Query(...)):
        run = store.get_external_ci_run(run_id, project=resolve_project(project))
        if not run:
            raise HTTPException(404, "external_ci_run not found")
        return run

    @router.get("/ixp/v1/publication_evidence")
    async def ixp_publication_evidence(project: str = Query(...),
                                       task_id: str = "", source_project: str = "",
                                       source_sha: str = "", public_repo: str = ""):
        return {"publication_evidence": store.list_publication_evidence(
            task_id=task_id, source_project=source_project,
            source_sha=source_sha, public_repo=public_repo,
            project=resolve_project(project))}

    @router.post("/ixp/v1/publication_evidence")
    async def ixp_record_publication_evidence(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "agent")
        payload = dict(body.get("publication") or body)
        payload["principal_id"] = principal["id"]
        result = store.create_publication_evidence(
            payload, actor=auth.actor(principal), project=project)
        if result.get("error"):
            raise HTTPException(400, result)
        return result

    @router.post("/ixp/v1/external_ci_mirror/request")
    async def ixp_request_external_ci_mirror(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "agent")
        source_path = (body.get("source_path") or body.get("source_checkout") or "").strip()
        payload = dict(body.get("run") or body)
        payload.pop("source_path", None)
        payload.pop("source_checkout", None)
        result = external_ci_mirror.request_external_ci_mirror_run(
            payload, source_path, actor=auth.actor(principal), project=project)
        if result.get("error"):
            raise HTTPException(400, result)
        return result

    @router.post("/ixp/v1/external_ci_mirror/poll")
    async def ixp_poll_external_ci_mirror(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "agent")
        source_path = (body.get("source_path") or body.get("source_checkout") or "").strip()
        result = external_ci_mirror.poll_external_ci_mirror_run(
            body.get("run_id") or "", source_path, actor=auth.actor(principal),
            project=project,
            poll_interval_seconds=float(body.get("poll_interval_seconds") or 15),
            timeout_seconds=float(body.get("timeout_seconds") or 1800))
        if result.get("error"):
            raise HTTPException(400, result)
        return result

    @router.post("/ixp/v1/merge_gate")
    async def ixp_merge_gate(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "agent")
        payload = dict(body)
        payload["project"] = project
        return merge_gate_command.execute_mapping_result(
            payload, actor=auth.actor(principal), principal_id=principal["id"])

    @router.post("/ixp/v1/claim_external_effect")
    async def ixp_claim_external_effect(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "agent")
        result = effect_command.claim_mapping_result(
            {**dict(body), "project": project},
            actor=auth.actor(principal), principal_id=principal["id"])
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/ixp/v1/mark_external_effect_issued")
    async def ixp_mark_external_effect_issued(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",), dev_actor="agent")
        result = effect_command.mark_issued_mapping_result(
            {**dict(body), "project": project}, actor=auth.actor(principal))
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/ixp/v1/verify_external_effect")
    async def ixp_verify_external_effect(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",), dev_actor="agent")
        result = effect_command.verify_mapping_result(
            {**dict(body), "project": project}, actor=auth.actor(principal))
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/ixp/v1/fail_external_effect")
    async def ixp_fail_external_effect(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",), dev_actor="agent")
        result = effect_command.fail_mapping_result(
            {**dict(body), "project": project}, actor=auth.actor(principal))
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    return router
