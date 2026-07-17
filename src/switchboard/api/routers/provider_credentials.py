"""Tenant provider-connection vault REST routes (CO-6)."""
from __future__ import annotations

import math
from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

import auth
from switchboard.api.deps import (
    require_agent_host_identity,
    resolve_agent_host_principal,
)
from switchboard.application.commands import provider_credentials as commands
from switchboard.domain.provider_credentials import list_provider_auth_capabilities
from switchboard.storage.repositories.provider_credentials import (
    CredentialVaultError,
    default_provider_credential_repository,
)


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]


class VerifyConnectionBody(BaseModel):
    """Typed wire body for the verify route — ARCH-MS-84 caps untyped dict bodies.

    Deliberately empty: verify takes no caller input, the proof is always derived
    server-side (see commands.verify_mapping / _derive_host_native_proof)."""

    model_config = ConfigDict(extra="forbid")


class BindHostNativeBody(BaseModel):
    """Typed wire body for bind-host — the browser never supplies a proof; the route
    derives it server-side from the selected, already-attested live host."""

    provider: str
    provider_account_id: str
    project_allowlist: list[str] = Field(default_factory=list)
    host_id: str = ""
    auth_type: str = ""


class HostApiKeyEnrollmentBody(BaseModel):
    """One-use secret body accepted only from an enrolled Agent Host bearer.

    ``SecretStr`` keeps the credential out of validation representations. The route
    never returns or logs this model and passes the plaintext directly to the vault,
    where it is envelope-encrypted before any response is built.
    """

    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1, max_length=128, pattern=r".*\S.*")
    host_id: str = Field(min_length=1, max_length=160, pattern=r"^host/")
    provider: str = "openai-codex"
    provider_account_id: str = Field(min_length=1, max_length=255)
    billing_account_id: str = Field(min_length=1, max_length=255)
    budget_ceiling: float
    budget_currency: str = "USD"
    api_key: SecretStr = Field(min_length=1, max_length=16384)

    @field_validator("provider")
    @classmethod
    def _openai_mvp_only(cls, value: str) -> str:
        provider = str(value or "").strip().lower()
        if provider != "openai-codex":
            raise ValueError("only openai-codex API enrollment is enabled")
        return provider

    @field_validator("provider_account_id", "billing_account_id", mode="before")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("budget_ceiling", mode="after")
    @classmethod
    def _positive_finite_budget(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("budget_ceiling must be positive and finite")
        return value

    @field_validator("budget_currency", mode="before")
    @classmethod
    def _currency(cls, value: str) -> str:
        currency = str(value or "").strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("budget_currency must be a three-letter code")
        return currency

    @field_validator("api_key", mode="after")
    @classmethod
    def _nonempty_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("api_key is required")
        return value


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
                principal_user_id=principal_id, principal_kind=access["principal_kind"],
                admin=is_admin, raise_errors=True)
        except (ValidationError, CredentialVaultError) as exc:
            _raise_http(exc)

    @router.post("/ixp/v1/agent-host-provider-connections/enroll-api-key")
    def enroll_host_api_key_connection(
            request: Request,
            body: HostApiKeyEnrollmentBody = Body(...)):
        """Enroll one metered API key from its owner-controlled Agent Host.

        This is deliberately not a browser/user-principal endpoint. The narrow host
        bearer, exact host identity, active enrollment, live presence, owner binding,
        project/provider allowlists, and vault write all have to agree. The plaintext
        exists only in this request and the immediate vault-encryption call.
        """
        project_id = resolve_project(body.project)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project_id, dev_actor=body.host_id)
        require_agent_host_identity(principal, body.host_id, project_id)
        secret = body.api_key.get_secret_value()
        try:
            return commands.enroll_host_api_key_mapping({
                "project": project_id,
                "host_id": body.host_id,
                "provider": body.provider,
                "provider_account_id": body.provider_account_id,
                "billing_account_id": body.billing_account_id,
                "budget_currency": body.budget_currency,
                "budget_ceiling": body.budget_ceiling,
                "api_key": secret,
            }, actor=auth.actor(principal), host_principal_id=principal.get("id") or "",
                raise_errors=True)
        except (ValidationError, CredentialVaultError) as exc:
            _raise_http(exc)
        finally:
            secret = ""

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
                principal_user_id=principal_id, admin=is_admin,
                include_events=True, include_lease_count=True)
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
                principal_kind=access["principal_kind"], admin=is_admin,
                raise_errors=True)
        except (ValidationError, CredentialVaultError) as exc:
            _raise_http(exc)

    @router.post(
        "/api/projects/{project}/provider-connections/{credential_reference}/verify"
    )
    def verify_provider_connection(request: Request, project: str,
                                   credential_reference: str,
                                   body: VerifyConnectionBody = Body(
                                       default_factory=VerifyConnectionBody)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:credentials",), dev_actor="provider-vault")
        access = _access(principal)
        principal_id, is_admin = access["principal_id"], access["admin"]
        try:
            return commands.verify_mapping(
                {**body.model_dump(), "project": project_id,
                 "credential_reference": credential_reference},
                actor=auth.actor(principal), principal_user_id=principal_id,
                principal_kind=access["principal_kind"], admin=is_admin,
                raise_errors=True)
        except (ValidationError, CredentialVaultError) as exc:
            _raise_http(exc)

    @router.post("/api/projects/{project}/provider-connections/bind-host")
    def bind_host_native_provider_connection(request: Request, project: str,
                                             body: BindHostNativeBody = Body(...)):
        """Owner-locked enrollment whose proof is derived server-side from a
        caller-selected live Agent Host — the browser never supplies a proof."""
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:credentials",), dev_actor="provider-vault")
        access = _access(principal)
        principal_id = access["principal_id"]
        try:
            return commands.bind_host_native_mapping(
                {**body.model_dump(), "project": project_id},
                actor=auth.actor(principal), principal_user_id=principal_id,
                raise_errors=True)
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
