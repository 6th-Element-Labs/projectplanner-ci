"""IXP Work Session REST routes.

Owns ``/ixp/v1/work_sessions*``, managed sessions, session health, repo
preflight, and pre_tool_check. Mutating create/update/preflight/archive paths
go through application commands; reads stay thin store calls. Auth and project
resolution are supplied by the composition root.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

import auth
import store
from switchboard.api.deps import (
    is_narrow_agent_host_principal,
    require_agent_host_bootstrap_authority,
    require_personal_execution_authority,
    resolve_agent_host_principal,
)
from switchboard.application.commands import pre_tool_check as pre_tool_check_command
from switchboard.application.commands import work_sessions as work_session_commands


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
BodyProjectResolver = Callable[[dict], str]


class PersonalExecutionBindingBody(BaseModel):
    """Exact durable tuple accepted by the narrow recovery readback route."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    claim_id: str
    work_session_id: str
    runner_session_id: str
    host_id: str
    agent_id: str
    wake_id: str
    source_sha: str
    execution_connection_id: str


class PersonalPostprocessingEvidenceBody(BaseModel):
    """Evidence fields whose exact persisted values classify recovery state."""

    model_config = ConfigDict(extra="forbid")

    branch: str = ""
    executed_test_run: dict[str, Any] | None = None


class PersonalPostprocessingStateBody(BaseModel):
    """Typed request for transactional personal-host post-processing readback."""

    model_config = ConfigDict(extra="forbid")

    project: str
    binding: PersonalExecutionBindingBody
    completed_head_sha: str
    expected_evidence: PersonalPostprocessingEvidenceBody = Field(
        default_factory=PersonalPostprocessingEvidenceBody)


def _raise_work_session_error(result: dict) -> None:
    status = 404 if result.get("error") == "work_session_not_found" else 400
    raise HTTPException(status, result)


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  resolve_body_project: BodyProjectResolver) -> APIRouter:
    """Build the Work Session IXP router against shared trust boundaries."""
    router = APIRouter()

    @router.get("/ixp/v1/work_sessions")
    async def ixp_work_sessions(project: str = Query(...),
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
        principal = resolve_agent_host_principal(
            resolve_principal, request, project,
            dev_actor=body.get("agent_id") or "work-session")
        payload = dict(body or {})
        payload.pop("project", None)
        bootstrap = payload.pop("agent_host_bootstrap_binding", {}) or {}
        if is_narrow_agent_host_principal(principal):
            expected = {
                "task_id": str(bootstrap.get("task_id") or "").upper(),
                "agent_id": str(bootstrap.get("agent_id") or ""),
            }
            actual = {
                "task_id": str(payload.get("task_id") or "").upper(),
                "agent_id": str(payload.get("agent_id") or ""),
            }
            if actual != expected or str(payload.get("runtime") or "") != "codex":
                raise HTTPException(403, {
                    "error_code": "agent_host_bootstrap_work_session_scope_denied",
                })
        require_agent_host_bootstrap_authority(
            principal, bootstrap, "create_work_session", project)
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
                                   project: str = Query(...)):
        session = store.get_work_session(
            work_session_id, project=resolve_project(project))
        if not session:
            raise HTTPException(404, "work_session_not_found")
        return session

    @router.get("/ixp/v1/work_sessions/{work_session_id}/health")
    async def ixp_get_work_session_health(
            work_session_id: str,
            project: str = Query(...)):
        health = store.get_work_session_health(
            work_session_id, project=resolve_project(project))
        if not health:
            raise HTTPException(404, "work_session_not_found")
        return health

    @router.get("/ixp/v1/session_health")
    async def ixp_session_health(project: str = Query(...),
                                 task_id: str = "", agent_id: str = "",
                                 status: str = "", only_unsafe: bool = False):
        return store.list_session_health(
            project=resolve_project(project), task_id=task_id,
            agent_id=agent_id, status=status, only_unsafe=only_unsafe)

    @router.post("/ixp/v1/personal_execution/postprocessing_state")
    async def ixp_personal_execution_postprocessing_state(
            request: Request,
            body: PersonalPostprocessingStateBody = Body(...)):
        """Read one authenticated, transactionally verified recovery phase."""
        payload = body.model_dump()
        project = resolve_body_project(payload)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project,
            dev_actor=body.binding.host_id or "personal-execution-readback")
        return store.get_personal_execution_postprocessing_state(
            body.binding.model_dump(),
            principal_id=principal["id"],
            completed_head_sha=body.completed_head_sha,
            expected_evidence=body.expected_evidence.model_dump(exclude_none=True),
            project=project,
        )

    @router.patch("/ixp/v1/work_sessions/{work_session_id}")
    async def ixp_update_work_session(work_session_id: str, request: Request,
                                      body: dict = Body(...)):
        project = resolve_body_project(body)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project,
            dev_actor=body.get("agent_id") or "work-session")
        payload = dict(body or {})
        payload.pop("project", None)
        binding = payload.pop("personal_execution_binding", {}) or {}
        bootstrap = payload.pop("agent_host_bootstrap_binding", {}) or {}
        if is_narrow_agent_host_principal(principal):
            if bootstrap:
                require_agent_host_bootstrap_authority(
                    principal, bootstrap, "expire_work_session", project,
                    work_session_id=work_session_id)
                if payload != {
                    "agent_id": str(bootstrap.get("agent_id") or ""),
                    "status": "expired",
                }:
                    raise HTTPException(403, {
                        "error_code": "agent_host_bootstrap_expire_scope_denied",
                    })
            elif str(binding.get("work_session_id") or "") != work_session_id:
                raise HTTPException(
                    403, "personal execution Work Session target mismatch")
            else:
                expected_checkpoint = {
                    "agent_id": str(binding.get("agent_id") or ""),
                    "head_sha": str(binding.get("completed_head_sha") or ""),
                    "dirty_status": "clean",
                    "conflict_marker_count": 0,
                }
                if any(payload.get(field) != value
                       for field, value in expected_checkpoint.items()):
                    raise HTTPException(
                        403, "personal execution Work Session checkpoint mismatch")
                allowed_fields = {
                    "agent_id", "head_sha", "dirty_status",
                    "conflict_marker_count", "hygiene",
                }
                forbidden = sorted(set(payload) - allowed_fields)
                if forbidden:
                    raise HTTPException(403, {
                        "error_code": "personal_work_session_update_scope_denied",
                        "forbidden_fields": forbidden,
                    })
                require_personal_execution_authority(
                    principal, binding, "checkpoint_work_session", project)
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
