"""Tenant provider-connection vault REST routes (CO-6)."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import ValidationError

import auth
from switchboard.application.commands import provider_credentials as commands
from switchboard.domain.provider_credentials import list_provider_auth_capabilities
from switchboard.storage.repositories.provider_credentials import (
    CredentialVaultError,
    default_provider_credential_repository,
)


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]


def _access(principal: dict) -> dict:
    scopes = set(principal.get("effective_scopes") or principal.get("scopes") or [])
    return {
        "principal_id": str(principal.get("id") or ""),
        "principal_kind": str(principal.get("kind") or "").lower(),
        "scopes": sorted(scopes),
        "admin": "admin" in scopes,
    }


def _raise_http(exc: BaseException) -> None:
    if isinstance(exc, CredentialVaultError):
        raise HTTPException(exc.status_code, exc.as_dict()) from exc
    if isinstance(exc, ValidationError):
        raise HTTPException(400, {
            "error": "invalid_provider_credential_request",
            "message": "provider credential request is invalid",
        }) from exc
    raise HTTPException(400, {"error": "invalid_provider_credential_request"}) from exc


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects/{project}/provider-auth-capabilities")
    def get_provider_auth_capabilities(request: Request, project: str):
        """Return the same fail-closed CO-15 matrix used by server execution paths."""
        project_id = resolve_project(project)
        resolve_principal(
            request, project_id, ("read",), dev_actor="provider-auth-policy")
        return list_provider_auth_capabilities()

    @router.post("/api/projects/{project}/provider-connections")
    def enroll_provider_connection(request: Request, project: str,
                                   body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:credentials",), dev_actor="provider-vault")
        access = _access(principal)
        principal_id, is_admin = access["principal_id"], access["admin"]
        try:
            return commands.enroll_mapping(
                {**dict(body or {}), "project": project_id}, actor=auth.actor(principal),
                principal_user_id=principal_id, admin=is_admin, raise_errors=True)
        except (ValidationError, CredentialVaultError) as exc:
            _raise_http(exc)

    @router.get("/api/projects/{project}/provider-connections")
    def list_provider_connections(request: Request, project: str, user_id: str = ""):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("read:credentials",), dev_actor="provider-vault")
        access = _access(principal)
        principal_id, is_admin = access["principal_id"], access["admin"]
        try:
            return {"connections": default_provider_credential_repository.list_metadata(
                project=project_id, principal_user_id=principal_id, admin=is_admin,
                user_id=user_id if is_admin else principal_id)}
        except CredentialVaultError as exc:
            _raise_http(exc)

    @router.get(
        "/api/projects/{project}/provider-connections/{credential_reference}"
    )
    def get_provider_connection(request: Request, project: str,
                                credential_reference: str):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("read:credentials",), dev_actor="provider-vault")
        access = _access(principal)
        principal_id, is_admin = access["principal_id"], access["admin"]
        try:
            return default_provider_credential_repository.get_metadata(
                credential_reference, project=project_id,
                principal_user_id=principal_id, admin=is_admin, include_events=True)
        except CredentialVaultError as exc:
            _raise_http(exc)

    @router.post(
        "/api/projects/{project}/provider-connections/{credential_reference}/rotate"
    )
    def rotate_provider_connection(request: Request, project: str,
                                   credential_reference: str,
                                   body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:credentials",), dev_actor="provider-vault")
        access = _access(principal)
        principal_id, is_admin = access["principal_id"], access["admin"]
        try:
            return commands.rotate_mapping(
                {**dict(body or {}), "project": project_id,
                 "credential_reference": credential_reference},
                actor=auth.actor(principal), principal_user_id=principal_id,
                admin=is_admin, raise_errors=True)
        except (ValidationError, CredentialVaultError) as exc:
            _raise_http(exc)

    @router.post(
        "/api/projects/{project}/provider-connections/{credential_reference}/revoke"
    )
    def revoke_provider_connection(request: Request, project: str,
                                   credential_reference: str,
                                   body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:credentials",), dev_actor="provider-vault")
        access = _access(principal)
        principal_id, is_admin = access["principal_id"], access["admin"]
        try:
            return commands.revoke_mapping(
                {**dict(body or {}), "project": project_id,
                 "credential_reference": credential_reference},
                actor=auth.actor(principal), principal_user_id=principal_id,
                admin=is_admin, raise_errors=True)
        except (ValidationError, CredentialVaultError) as exc:
            _raise_http(exc)

    @router.delete(
        "/api/projects/{project}/provider-connections/{credential_reference}"
    )
    def delete_provider_connection(request: Request, project: str,
                                   credential_reference: str,
                                   body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:credentials",), dev_actor="provider-vault")
        access = _access(principal)
        principal_id, is_admin = access["principal_id"], access["admin"]
        try:
            return commands.delete_mapping(
                {**dict(body or {}), "project": project_id,
                 "credential_reference": credential_reference},
                actor=auth.actor(principal), principal_user_id=principal_id,
                admin=is_admin, raise_errors=True)
        except (ValidationError, CredentialVaultError) as exc:
            _raise_http(exc)

    @router.post(
        "/api/projects/{project}/provider-connections/{credential_reference}/leases"
    )
    def acquire_provider_credential_lease(request: Request, project: str,
                                          credential_reference: str,
                                          body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("use:credentials",), dev_actor="provider-vault")
        access = _access(principal)
        try:
            return commands.acquire_lease_mapping(
                {**dict(body or {}), "project": project_id,
                 "credential_reference": credential_reference},
                actor=auth.actor(principal), principal=access, raise_errors=True)
        except (ValidationError, CredentialVaultError) as exc:
            _raise_http(exc)

    @router.post(
        "/api/projects/{project}/provider-credential-leases/{lease_id}/release"
    )
    def release_provider_credential_lease(request: Request, project: str,
                                          lease_id: str,
                                          body: dict = Body(default={})):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("use:credentials",), dev_actor="provider-vault")
        access = _access(principal)
        try:
            return commands.release_lease_mapping(
                {**dict(body or {}), "project": project_id, "lease_id": lease_id},
                actor=auth.actor(principal), principal=access, raise_errors=True)
        except (ValidationError, CredentialVaultError) as exc:
            _raise_http(exc)

    @router.post(
        "/api/projects/{project}/provider-credential-leases/{lease_id}/materialize-envelope"
    )
    def materialize_provider_credential_envelope(
            request: Request, project: str, lease_id: str,
            body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("use:credentials",), dev_actor="provider-vault")
        try:
            return commands.materialize_worker_envelope_mapping(
                {**dict(body or {}), "project": project_id}, lease_id=lease_id,
                actor=auth.actor(principal), principal=_access(principal),
                raise_errors=True,
            )
        except (ValidationError, CredentialVaultError) as exc:
            _raise_http(exc)

    @router.post(
        "/api/projects/{project}/provider-credential-leases/{lease_id}/activate"
    )
    def activate_provider_credential_lease(
            request: Request, project: str, lease_id: str,
            body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("use:credentials",), dev_actor="provider-vault")
        try:
            return commands.activate_worker_lease_mapping(
                {**dict(body or {}), "project": project_id}, lease_id=lease_id,
                actor=auth.actor(principal), principal=_access(principal),
                raise_errors=True,
            )
        except (ValidationError, CredentialVaultError) as exc:
            _raise_http(exc)

    return router
