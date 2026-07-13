"""Project lifecycle read routes."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request

import auth
import store
from switchboard.application.commands import (
    project_consolidation,
    project_lifecycle,
    project_metadata,
    project_purge,
)
from switchboard.application.queries import project_admin, project_impact


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects/{project}")
    def get_project(request: Request, project: str):
        project_id = resolve_project(project)
        principal = resolve_principal(request, project_id, ("read",), dev_actor="web")
        result = project_admin.execute_for(
            project_id,
            access_repository=store.access_repository,
            repo_topology_provider=store.get_project_repo_topology,
            access_model_provider=store.project_access_model,
            principal_id=str(principal.get("id") or ""),
            principal_scopes=list(
                principal.get("effective_scopes") or principal.get("scopes") or []),
        )
        if result.get("error"):
            raise HTTPException(404, result)
        return result

    @router.patch("/api/projects/{project}")
    def update_project(request: Request, project: str,
                       body: dict = Body(...)):
        project_id = resolve_project(project)
        trust_boundary = bool({"boundary", "visibility"}.intersection(body or {}))
        principal = resolve_principal(
            request, project_id,
            (("write:system",) if trust_boundary else ("write:projects",)),
            dev_actor="web")
        result = project_metadata.execute(
            {**dict(body or {}), "project_id": project_id},
            actor=auth.actor(principal),
            access_repository=store.access_repository,
        )
        if result.get("error"):
            code = 423 if result.get("error") == "project_archived" else 400
            if str(result.get("error")).startswith("unknown project"):
                code = 404
            raise HTTPException(code, result)
        return result

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

    @router.post("/api/projects/{project}/consolidation/plan")
    def plan_project_consolidation(request: Request, project: str,
                                   body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        result = project_consolidation.plan_project_consolidation(
            {**dict(body or {}), "source_project_id": project_id,
             "actor": auth.actor(principal)},
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            code = 404 if str(result.get("error")).startswith("unknown project") else 409
            if str(result.get("error")).startswith("invalid_project_consolidation"):
                code = 400
            raise HTTPException(code, result)
        return result

    @router.post("/api/projects/{project}/consolidation/apply")
    def apply_project_consolidation(request: Request, project: str,
                                    body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        plan = dict((body or {}).get("plan") or {})
        if plan.get("source_project_id") != project_id:
            raise HTTPException(400, {"error": "plan source does not match route project"})
        result = project_consolidation.apply_project_consolidation(
            {**dict(body or {}), "actor": auth.actor(principal)},
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            raise HTTPException(409, result)
        return result

    @router.get("/api/projects/{project}/consolidation/{consolidation_id}/verify")
    def verify_project_consolidation(request: Request, project: str,
                                     consolidation_id: str):
        project_id = resolve_project(project)
        resolve_principal(request, project_id, ("read",), dev_actor="web")
        result = project_consolidation.verify_project_consolidation(
            project_id, consolidation_id,
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            raise HTTPException(404, result)
        return result

    @router.post("/api/projects/{project}/consolidation/{consolidation_id}/rollback")
    def rollback_project_consolidation(request: Request, project: str,
                                       consolidation_id: str,
                                       body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        result = project_consolidation.rollback_project_consolidation(
            {**dict(body or {}), "source_project_id": project_id,
             "consolidation_id": consolidation_id, "actor": auth.actor(principal)},
            access_repository=store.access_repository,
            project_configs=store._project_map(),
        )
        if result.get("error"):
            raise HTTPException(409, result)
        return result

    @router.post("/api/projects/{project}/purge/intents")
    def create_project_purge_intent(request: Request, project: str,
                                    body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        result = project_purge.create_purge_intent(
            {**dict(body or {}), "project_id": project_id,
             "actor": auth.actor(principal)},
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            raise HTTPException(409 if not str(result["error"]).startswith("invalid_") else 400,
                                result)
        return result

    @router.post("/api/projects/{project}/purge/intents/{intent_id}/verify")
    def verify_project_purge_intent(request: Request, project: str, intent_id: str,
                                    body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        result = project_purge.verify_purge_intent(
            {**dict(body or {}), "project_id": project_id, "intent_id": intent_id,
             "verifier": auth.actor(principal)},
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            raise HTTPException(409, result)
        return result

    @router.post("/api/projects/{project}/purge/intents/{intent_id}/execute")
    def execute_project_purge(request: Request, project: str, intent_id: str,
                              body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        result = project_purge.execute_purge(
            {**dict(body or {}), "project_id": project_id, "intent_id": intent_id,
             "actor": auth.actor(principal)},
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            raise HTTPException(409, result)
        return result

    @router.post("/api/projects/{project}/cleanup-review")
    def record_project_cleanup_review(request: Request, project: str,
                                      body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        actor_name = auth.actor(principal)
        result = project_purge.record_cleanup_review(
            {**dict(body or {}), "project_id": project_id,
             "approved_by": actor_name},
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            raise HTTPException(409, result)
        return result

    return router
