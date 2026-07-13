"""Project lifecycle read routes."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request

import auth
import store
from switchboard.application.commands import project_lifecycle
from switchboard.application.queries import project_impact


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects/{project}/impact")
    def project_impact_report(request: Request, project: str,
                              limit: int = Query(50, ge=1, le=200)):
        project_id = resolve_project(project)
        resolve_principal(request, project_id, ("read",), dev_actor="web")
        result = project_impact.execute_for(
            project_id,
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
            limit=limit,
        )
        if result.get("error"):
            raise HTTPException(404, result)
        return result

    @router.post("/api/projects/{project}/archive")
    def archive_project(request: Request, project: str,
                        body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        result = project_lifecycle.archive_project(
            {**dict(body or {}), "project_id": project_id,
             "actor": auth.actor(principal)},
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            code = 404 if str(result.get("error")).startswith("unknown project") else 409
            if str(result.get("error")).startswith("invalid_archive"):
                code = 400
            raise HTTPException(code, result)
        return result

    @router.post("/api/projects/{project}/restore")
    def restore_project(request: Request, project: str,
                        body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        result = project_lifecycle.restore_project(
            {**dict(body or {}), "project_id": project_id,
             "actor": auth.actor(principal)},
            access_repository=store.access_repository,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            code = 404 if str(result.get("error")).startswith("unknown project") else 409
            if str(result.get("error")).startswith("invalid_restore"):
                code = 400
            raise HTTPException(code, result)
        return result

    return router
