"""Agent registry REST routes (IXP register / presence / hosts).

Owns agent/host registry IXP routes (register, heartbeat, list, host_status,
control_plane_probe) while the composition root supplies project and principal
boundaries. Domain persistence stays behind application commands / store.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request

import auth
import store
from switchboard.api.deps import (
    authorize_agent_host_principal,
    is_narrow_agent_host_principal,
    require_agent_host_bootstrap_authority,
    require_agent_host_identity,
    resolve_agent_host_principal,
)
from switchboard.application.commands import register_agent as register_agent_command
from switchboard.application.commands import agent_host_enrollment as enrollment_command
from switchboard.application.commands import register_host as register_host_command
from switchboard.application.contracts.agents import (
    BeginHostEnrollmentCommand,
    CompleteHostEnrollmentCommand,
    DirectAssignmentMCPTokenCommand,
    FinalizeHostEnrollmentCommand,
    RevokeHostIdentityCommand,
    RotateHostIdentityCommand,
    UpdateHostExecutionPolicyCommand,
)


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
BodyProjectResolver = Callable[[dict], str]
ControlPlaneHttp = Callable[[Any], Any]


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  resolve_body_project: BodyProjectResolver,
                  control_plane_http: ControlPlaneHttp) -> APIRouter:
    """Build the agent registry IXP router against shared trust boundaries."""
    router = APIRouter()

    @router.post("/ixp/v1/register_agent")
    async def ixp_register_agent(request: Request, body: dict = Body(...)):
        body = dict(body or {})
        project = resolve_body_project(body)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project,
            dev_actor=body.get("agent_id") or "agent")
        bootstrap = body.pop("agent_host_bootstrap_binding", {}) or {}
        if is_narrow_agent_host_principal(principal):
            authority = require_agent_host_bootstrap_authority(
                principal, bootstrap, "register_agent", project)
            mismatches = sorted(
                field for field, actual, expected in (
                    ("agent_id", body.get("agent_id"), authority.get("agent_id")),
                    ("task_id", body.get("task_id"), authority.get("task_id")),
                    ("runtime", body.get("runtime"), authority.get("runtime")),
                ) if str(actual or "") != str(expected or "")
            )
            if mismatches:
                raise HTTPException(403, {
                    "error_code": "agent_host_bootstrap_request_mismatch",
                    "mismatches": mismatches,
                })
        body.setdefault("agent_id", auth.actor(principal))
        body["project"] = project
        return register_agent_command.execute_mapping_result(
            body, actor=auth.actor(principal), principal_id=principal["id"])

    @router.post("/ixp/v1/register_host")
    async def ixp_register_host(request: Request, body: dict = Body(...)):
        body = dict(body or {})
        project = resolve_body_project(body)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project,
            dev_actor=body.get("host_id") or "agent-host")
        require_agent_host_identity(
            principal, str(body.get("host_id") or ""), project)
        body["project"] = project
        return control_plane_http(register_host_command.execute_mapping_result(
            body, actor=auth.actor(principal), principal_id=principal["id"]))

    @router.post("/ixp/v1/heartbeat")
    async def ixp_heartbeat(request: Request, body: dict = Body(...)):
        body = dict(body or {})
        project = resolve_body_project(body)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project,
            dev_actor=body.get("agent_id") or "agent")
        bootstrap = body.pop("agent_host_bootstrap_binding", {}) or {}
        if is_narrow_agent_host_principal(principal):
            authority = require_agent_host_bootstrap_authority(
                principal, bootstrap, "heartbeat_agent", project)
            mismatches = sorted(
                field for field in ("agent_id", "task_id")
                if str(body.get(field) or "") != str(authority.get(field) or "")
            )
            if mismatches:
                raise HTTPException(403, {
                    "error_code": "agent_host_bootstrap_request_mismatch",
                    "mismatches": mismatches,
                })
        return store.heartbeat((body.get("agent_id") or "").strip(),
                               actor=auth.actor(principal), project=project)

    @router.get("/ixp/v1/agents")
    async def ixp_agents(project: str = Query(...), lane: str = ""):
        return {"agents": store.list_active_agents(lane=lane, project=resolve_project(project))}

    @router.post("/ixp/v1/heartbeat_host")
    async def ixp_heartbeat_host(request: Request, body: dict = Body(...)):
        body = dict(body or {})
        project = resolve_body_project(body)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project,
            dev_actor=body.get("host_id") or "agent-host")
        require_agent_host_identity(
            principal, str(body.get("host_id") or ""), project)
        return control_plane_http(store.heartbeat_host(
            (body.get("host_id") or "").strip(),
            active_sessions=body.get("active_sessions"),
            capacity=body.get("capacity") or {},
            status=body.get("status") or "online",
            last_error=body.get("last_error") or "",
            principal_id=principal["id"], actor=auth.actor(principal), project=project))

    @router.post("/ixp/v1/direct_assignments/mcp_token")
    async def ixp_direct_assignment_mcp_token(
            request: Request, body: DirectAssignmentMCPTokenCommand = Body(...)):
        """Give an exact direct CLI its short-lived task-scoped MCP bearer."""
        payload = body.model_dump(by_alias=True)
        project = resolve_body_project(payload)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project,
            dev_actor=body.host_id or "direct-cli")
        host_id = body.host_id
        require_agent_host_identity(principal, host_id, project)
        result = store.issue_direct_session_mcp_token(
            body.wake_id, host_id, body.runner_session_id,
            principal_id=principal["id"], actor=auth.actor(principal),
            project=project,
        )
        if result.get("error"):
            raise HTTPException(403, result)
        return result

    @router.post("/ixp/v1/agent-host-enrollments")
    async def ixp_begin_agent_host_enrollment(
            request: Request, body: BeginHostEnrollmentCommand = Body(...)):
        payload = body.model_dump(by_alias=True)
        project = resolve_body_project(payload)
        principal = resolve_principal(
            request, project, ("write:system",), dev_actor="agent-host-enrollment")
        payload["project"] = project
        return control_plane_http(enrollment_command.begin_mapping_result(
            payload, actor=auth.actor(principal), principal_id=principal["id"]))

    @router.post("/ixp/v1/agent-host-enrollments/complete")
    async def ixp_complete_agent_host_enrollment(
            body: CompleteHostEnrollmentCommand = Body(...)):
        payload = body.model_dump(by_alias=True)
        payload["project"] = resolve_body_project(payload)
        return control_plane_http(enrollment_command.complete_mapping_result(payload))

    @router.post("/ixp/v1/agent-host-enrollments/rotate")
    async def ixp_rotate_agent_host_identity(
            request: Request, body: RotateHostIdentityCommand = Body(...)):
        payload = body.model_dump(by_alias=True)
        project = resolve_body_project(payload)
        host_id = body.host_id
        try:
            principal = resolve_agent_host_principal(
                resolve_principal, request, project, dev_actor=host_id)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            recovery = store.get_agent_host_rotation_recovery_principal(
                token=auth.bearer_from_request(request), host_id=host_id, project=project)
            if not recovery:
                raise
            try:
                principal = authorize_agent_host_principal(recovery, project)
            except PermissionError:
                raise exc
        payload["host_id"] = host_id
        payload["project"] = project
        return control_plane_http(enrollment_command.rotate_mapping_result(
            payload, actor=auth.actor(principal), principal_id=principal["id"]))

    @router.post("/ixp/v1/agent-host-enrollments/finalize")
    async def ixp_finalize_agent_host_enrollment(
            request: Request, body: FinalizeHostEnrollmentCommand = Body(...)):
        payload = body.model_dump(by_alias=True)
        project = resolve_body_project(payload)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project, dev_actor=body.host_id)
        payload["project"] = project
        return control_plane_http(enrollment_command.finalize_mapping_result(
            payload, actor=auth.actor(principal), principal_id=principal["id"]))

    @router.post("/ixp/v1/agent-host-enrollments/revoke")
    async def ixp_revoke_agent_host_identity(
            request: Request, body: RevokeHostIdentityCommand = Body(...)):
        payload = body.model_dump(by_alias=True)
        project = resolve_body_project(payload)
        host_id = body.host_id
        try:
            principal = resolve_agent_host_principal(
                resolve_principal, request, project, dev_actor=host_id)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            recovery = store.get_agent_host_revocation_recovery_principal(
                token=auth.bearer_from_request(request), host_id=host_id, project=project)
            if not recovery:
                raise
            try:
                principal = authorize_agent_host_principal(recovery, project)
            except PermissionError:
                raise exc
        enrollment = store.get_agent_host_enrollment(host_id, project=project)
        scopes = set(principal.get("effective_scopes") or principal.get("scopes") or [])
        owns_identity = enrollment.get("principal_id") == principal.get("id")
        is_operator = "admin" in scopes or "write:system" in scopes
        if not owns_identity and not is_operator:
            raise HTTPException(403, "host identity may be revoked only by its owner or an operator")
        payload["project"] = project
        return control_plane_http(enrollment_command.revoke_mapping_result(
            payload, actor=auth.actor(principal)))

    @router.post("/ixp/v1/agent-host-enrollments/execution-policy")
    async def ixp_update_agent_host_execution_policy(
            request: Request,
            body: UpdateHostExecutionPolicyCommand = Body(...)):
        payload = body.model_dump(by_alias=True)
        project = resolve_body_project(payload)
        principal = resolve_principal(
            request, project, ("write:system",), dev_actor="agent-host-policy")
        payload["project"] = project
        return control_plane_http(
            enrollment_command.update_execution_policy_mapping_result(
                payload, actor=auth.actor(principal), principal_id=principal["id"]))

    @router.get("/ixp/v1/agent-host-enrollment")
    async def ixp_agent_host_enrollment_status(
            request: Request, host_id: str,
            project: str = Query(...)):
        resolved = resolve_project(project)
        principal = resolve_principal(
            request, resolved, ("read",), dev_actor=host_id)
        enrollment = store.get_agent_host_enrollment(host_id, project=resolved)
        scopes = set(principal.get("effective_scopes") or principal.get("scopes") or [])
        if (enrollment.get("principal_id") != principal.get("id")
                and "admin" not in scopes and "write:system" not in scopes):
            raise HTTPException(403, "host enrollment is private to its owner")
        return control_plane_http(enrollment)

    @router.get("/ixp/v1/agent_hosts")
    async def ixp_agent_hosts(request: Request, project: str = Query(...), runtime: str = "",
                              lane: str = "", capability: str = "",
                              include_stale: bool = False):
        resolved = resolve_project(project)
        resolve_principal(request, resolved, ("read",), dev_actor="agent-host-inventory")
        hosts = store.list_agent_hosts(runtime=runtime, lane=lane,
                                       capability=capability,
                                       include_stale=include_stale,
                                       project=resolved)
        control_plane_http(hosts)
        return {"hosts": hosts}

    @router.get("/ixp/v1/control_plane_probe")
    async def ixp_control_plane_probe(project: str = Query(...), lane: str = "",
                                      include_heavy: bool = False):
        from switchboard.application.queries.control_plane_probe import execute
        return execute(
            project=resolve_project(project), lane=lane, include_heavy=include_heavy)

    @router.get("/ixp/v1/host_status")
    async def ixp_host_status(request: Request, host_id: str, project: str = Query(...)):
        resolved = resolve_project(project)
        resolve_principal(request, resolved, ("read",), dev_actor=host_id or "agent-host")
        status = control_plane_http(store.host_status(host_id, project=resolved))
        if status.get("error"):
            raise HTTPException(404, status["error"])
        return status

    return router
