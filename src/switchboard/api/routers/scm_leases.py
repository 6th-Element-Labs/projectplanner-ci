"""Host/operator REST surface for short-lived SCM leases (ENFORCE-13).

Hosts acquire a lease after claiming the exact wake, release it on drain, and read
its state. There is deliberately no endpoint that materializes a token: token
minting is a trusted in-process bridge, never an HTTP response.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException, Request

import auth
from switchboard.domain.scm_leases import SCMLeaseError, SCMLeasePrincipal
from switchboard.storage.repositories.scm_leases import (
    default_scm_lease_repository as repository,
)


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]


def _access(principal: dict) -> dict[str, Any]:
    scopes = set(principal.get("effective_scopes") or principal.get("scopes") or [])
    return {
        "principal_id": str(principal.get("id") or ""),
        "principal_kind": str(principal.get("kind") or "").lower(),
        "scopes": sorted(scopes),
        "admin": "admin" in scopes,
    }


def _raise(exc: SCMLeaseError) -> None:
    raise HTTPException(exc.status_code, exc.as_dict()) from exc


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver) -> APIRouter:
    router = APIRouter()

    @router.post("/api/projects/{project}/scm-leases")
    def acquire_lease(request: Request, project: str, body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("use:credentials",), dev_actor="scm-leases")
        payload = dict(body or {})
        try:
            return repository.acquire_lease(
                project=project_id,
                connection_id=str(payload.get("connection_id") or ""),
                repository=str(payload.get("repository") or ""),
                org_id=str(payload.get("org_id") or ""),
                operations=payload.get("operations") or [],
                task_id=str(payload.get("task_id") or ""),
                generation=str(payload.get("generation") or ""),
                context_digest=str(payload.get("context_digest") or ""),
                host_id=str(payload.get("host_id") or ""),
                runner_session_id=str(payload.get("runner_session_id") or ""),
                work_session_id=str(payload.get("work_session_id") or ""),
                claim_id=str(payload.get("claim_id") or ""),
                wake_id=str(payload.get("wake_id") or ""),
                ttl_seconds=int(payload.get("ttl_seconds") or 900),
                actor=auth.actor(principal),
                principal=SCMLeasePrincipal.from_mapping(_access(principal)))
        except SCMLeaseError as exc:
            _raise(exc)

    @router.post("/api/projects/{project}/scm-leases/{lease_id}/release")
    def release_lease(request: Request, project: str, lease_id: str,
                      body: dict = Body(default={})):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("use:credentials",), dev_actor="scm-leases")
        try:
            return repository.release_lease(
                lease_id, project=project_id, actor=auth.actor(principal),
                reason=str((body or {}).get("reason") or "released"),
                principal=SCMLeasePrincipal.from_mapping(_access(principal)))
        except SCMLeaseError as exc:
            _raise(exc)

    @router.get("/api/projects/{project}/scm-leases/{lease_id}")
    def get_lease(request: Request, project: str, lease_id: str):
        project_id = resolve_project(project)
        resolve_principal(
            request, project_id, ("read:credentials",), dev_actor="scm-leases")
        try:
            return repository.get_lease(lease_id, project=project_id)
        except SCMLeaseError as exc:
            _raise(exc)

    return router


__all__ = ["create_router"]
