"""IXP Work Session REST routes.

Owns ``/ixp/v1/work_sessions*``, managed sessions, session health, repo
preflight, and pre_tool_check. Mutating create/update/preflight/archive paths
go through application commands; reads stay thin store calls. Auth and project
resolution are supplied by the composition root.
"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request

import auth
import store
from switchboard.application.commands import pre_tool_check as pre_tool_check_command
from switchboard.application.commands import work_sessions as work_session_commands


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
BodyProjectResolver = Callable[[dict], str]


def _raise_work_session_error(result: dict) -> None:
    status = 404 if result.get("error") == "work_session_not_found" else 400
    raise HTTPException(status, result)


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  resolve_body_project: BodyProjectResolver) -> APIRouter:
    """Build the Work Session IXP router against shared trust boundaries."""
    router = APIRouter()

    @router.get("/ixp/v1/work_sessions")
    async def ixp_work_sessions(project: str = Query(store.DEFAULT_PROJECT),
                                task_id: str = "", agent_id: str = "",
                                status: str = "", repo_role: str = "",
                                include_expired: bool = True):
        project = resolve_project(project)
        return {
            "project": project,
            "contract": store.work_session_contract(project),
            "work_sessions": store.list_work_sessions(
                project=project, task_id=task_id, agent_id=agent_id,
                status=status, repo_role=repo_role,
                include_expired=include_expired),
        }

    @router.post("/ixp/v1/work_sessions")
    async def ixp_create_work_session(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "work-session")
        payload = dict(body or {})
        payload.pop("project", None)
        result = work_session_commands.create(
            payload, actor=auth.actor(principal),
            principal_id=principal["id"], project=project)
        if result.get("error"):
            raise HTTPException(400, result)
        return result

    @router.post("/ixp/v1/managed_work_sessions")
    async def ixp_create_managed_work_session(request: Request,
                                              body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "managed-work-session")
        payload = dict(body or {})
        payload.pop("project", None)
        result = work_session_commands.create_managed(
            payload, actor=auth.actor(principal),
            principal_id=principal["id"], project=project)
        if result.get("error"):
            raise HTTPException(400, result)
        return result

    @router.get("/ixp/v1/work_sessions/{work_session_id}")
    async def ixp_get_work_session(work_session_id: str,
                                   project: str = Query(store.DEFAULT_PROJECT)):
        session = store.get_work_session(
            work_session_id, project=resolve_project(project))
        if not session:
            raise HTTPException(404, "work_session_not_found")
        return session

    @router.get("/ixp/v1/work_sessions/{work_session_id}/health")
    async def ixp_get_work_session_health(
            work_session_id: str,
            project: str = Query(store.DEFAULT_PROJECT)):
        health = store.get_work_session_health(
            work_session_id, project=resolve_project(project))
        if not health:
            raise HTTPException(404, "work_session_not_found")
        return health

    @router.get("/ixp/v1/session_health")
    async def ixp_session_health(project: str = Query(store.DEFAULT_PROJECT),
                                 task_id: str = "", agent_id: str = "",
                                 status: str = "", only_unsafe: bool = False):
        return store.list_session_health(
            project=resolve_project(project), task_id=task_id,
            agent_id=agent_id, status=status, only_unsafe=only_unsafe)

    @router.patch("/ixp/v1/work_sessions/{work_session_id}")
    async def ixp_update_work_session(work_session_id: str, request: Request,
                                      body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "work-session")
        payload = dict(body or {})
        payload.pop("project", None)
        result = work_session_commands.update(
            work_session_id, payload, actor=auth.actor(principal),
            project=project)
        if result.get("error"):
            _raise_work_session_error(result)
        return result

    @router.post("/ixp/v1/work_sessions/{work_session_id}/archive_workspace")
    async def ixp_archive_work_session_workspace(
            work_session_id: str, request: Request,
            body: dict = Body(default={})):
        body = body or {}
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "managed-work-session")
        result = work_session_commands.archive(
            work_session_id,
            remove_workspace=bool(body.get("remove_workspace", False)),
            actor=auth.actor(principal),
            project=project,
        )
        if result.get("error"):
            _raise_work_session_error(result)
        return result

    @router.post("/ixp/v1/repo_preflight")
    async def ixp_repo_preflight(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "repo-preflight")
        # Side-effectful FS/git inspection still lives on store until ARCH-MS-58.
        return store.repo_preflight(
            worktree_path=body.get("worktree_path") or body.get("path") or "",
            project=project,
            task_id=body.get("task_id") or body.get("task") or "",
            agent_id=body.get("agent_id") or "",
            repo_role=body.get("repo_role") or "canonical",
            expected_branch=body.get("expected_branch") or "",
            expected_base_ref=body.get("expected_base_ref") or "",
            scan_conflicts=bool(body.get("scan_conflicts", True)),
        )

    @router.post("/ixp/v1/work_sessions/{work_session_id}/preflight")
    async def ixp_preflight_work_session(
            work_session_id: str, request: Request,
            body: dict = Body(default={})):
        body = body or {}
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "work-session")
        result = work_session_commands.preflight(
            work_session_id, actor=auth.actor(principal), project=project,
            expected_branch=body.get("expected_branch") or "",
            expected_base_ref=body.get("expected_base_ref") or "")
        if result.get("error"):
            _raise_work_session_error(result)
        return result

    @router.post("/ixp/v1/pre_tool_check")
    async def ixp_pre_tool_check(request: Request, body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_principal(
            request, project, ("write:ixp",),
            dev_actor=body.get("agent_id") or "pre-tool")
        payload = dict(body or {})
        payload.pop("project", None)
        return pre_tool_check_command.execute_mapping_result(
            payload, actor=auth.actor(principal),
            principal_id=principal["id"], project=project)

    return router
