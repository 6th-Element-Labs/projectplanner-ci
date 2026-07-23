"""Administrative REST surface for project-scoped SCM connections."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Request

import auth
from switchboard.storage.repositories.scm_connections import (
    SCMConnectionError,
    default_scm_connection_repository as repository,
)


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]


def _admin(principal: dict) -> None:
    scopes = set(principal.get("effective_scopes") or principal.get("scopes") or [])
    if "admin" not in scopes:
        raise HTTPException(403, {
            "error": "scm_connection_admin_required",
            "message": "SCM connection administration requires admin scope",
        })


def _raise(exc: SCMConnectionError) -> None:
    raise HTTPException(exc.status_code, exc.as_dict()) from exc


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver) -> APIRouter:
    router = APIRouter()

    @router.post("/api/projects/{project}/scm-connections")
    def create_connection(request: Request, project: str, body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:credentials",), dev_actor="scm-connections")
        _admin(principal)
        try:
            return repository.create(
                {**dict(body or {}), "project_allowlist":
                 body.get("project_allowlist") or [project_id], "project": project_id},
                actor=auth.actor(principal))
        except SCMConnectionError as exc:
            _raise(exc)

    @router.get("/api/projects/{project}/scm-connections")
    def list_connections(request: Request, project: str):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("read:credentials",), dev_actor="scm-connections")
        _admin(principal)
        return {"connections": repository.list(project=project_id)}

    @router.get("/api/projects/{project}/scm-connections/{connection_id}")
    def get_connection(request: Request, project: str, connection_id: str):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("read:credentials",), dev_actor="scm-connections")
        _admin(principal)
        try:
            result = repository.get(connection_id, include_events=True)
            if project_id not in result["project_allowlist"]:
                raise SCMConnectionError("repository_not_authorized",
                                         "SCM connection is not authorized for this project",
                                         status_code=403)
            return result
        except SCMConnectionError as exc:
            _raise(exc)

    @router.patch("/api/projects/{project}/scm-connections/{connection_id}")
    def update_connection(request: Request, project: str, connection_id: str,
                          body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:credentials",), dev_actor="scm-connections")
        _admin(principal)
        try:
            current = repository.get(connection_id)
            if project_id not in current["project_allowlist"]:
                raise SCMConnectionError("repository_not_authorized",
                                         "SCM connection is not authorized for this project",
                                         status_code=403)
            return repository.update(connection_id, dict(body or {}),
                                     actor=auth.actor(principal), project=project_id)
        except SCMConnectionError as exc:
            _raise(exc)

    @router.post("/api/projects/{project}/scm-connections/{connection_id}/rotate")
    def rotate_connection(request: Request, project: str, connection_id: str,
                          body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:credentials",), dev_actor="scm-connections")
        _admin(principal)
        try:
            return repository.rotate(connection_id, body.get("installation_ref"),
                                     actor=auth.actor(principal), project=project_id)
        except SCMConnectionError as exc:
            _raise(exc)

    @router.post("/api/projects/{project}/scm-connections/{connection_id}/revoke")
    def revoke_connection(request: Request, project: str, connection_id: str,
                          body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:credentials",), dev_actor="scm-connections")
        _admin(principal)
        try:
            return repository.revoke(connection_id, body.get("reason"),
                                     actor=auth.actor(principal), project=project_id)
        except SCMConnectionError as exc:
            _raise(exc)

    @router.delete("/api/projects/{project}/scm-connections/{connection_id}")
    def delete_connection(request: Request, project: str, connection_id: str,
                          reason: str):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:credentials",), dev_actor="scm-connections")
        _admin(principal)
        try:
            return repository.delete(
                connection_id, reason, actor=auth.actor(principal), project=project_id)
        except SCMConnectionError as exc:
            _raise(exc)

    @router.post("/api/projects/{project}/scm-connections/{connection_id}/preflight")
    def preflight_connection(request: Request, project: str, connection_id: str,
                             body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("use:credentials",), dev_actor="scm-preflight")
        try:
            result = repository.preflight(
                connection_id, project=project_id,
                repository=body.get("repository"), operation=body.get("operation"),
                actor=auth.actor(principal))
            if not result["allowed"]:
                raise HTTPException(403, result)
            return result
        except SCMConnectionError as exc:
            _raise(exc)

    return router
