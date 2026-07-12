"""Project lifecycle read routes."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, HTTPException, Query, Request

import store
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

    return router
