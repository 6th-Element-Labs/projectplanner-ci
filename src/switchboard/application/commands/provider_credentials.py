"""Shared provider-credential vault commands for REST and MCP (CO-6)."""
from __future__ import annotations

from collections.abc import Callable
import time
from typing import Any

from pydantic import ValidationError

import store
from switchboard.contracts import validation_error_message
from switchboard.contracts.provider_credentials import (
    AcquireProviderCredentialLeaseCommand,
    DeleteProviderConnectionCommand,
    EnrollProviderConnectionCommand,
    ReleaseProviderCredentialLeaseCommand,
    RevokeProviderConnectionCommand,
    RotateProviderConnectionCommand,
)
from switchboard.domain.provider_credentials import CredentialPolicyError, CredentialPrincipal
from switchboard.storage.repositories.provider_credentials import (
    CredentialVaultError,
    ProviderCredentialRepository,
    default_provider_credential_repository,
)


def _error(exc: BaseException, default_code: str) -> dict[str, Any]:
    if isinstance(exc, CredentialVaultError):
        return exc.as_dict()
    if isinstance(exc, ValidationError):
        message = validation_error_message(exc)
    else:
        message = "provider credential request is invalid"
    return {"error": default_code, "error_code": default_code, "message": message}


def _credential_principal(value: dict[str, Any] | CredentialPrincipal) -> CredentialPrincipal:
    if isinstance(value, CredentialPrincipal):
        return value
    try:
        return CredentialPrincipal.from_mapping(value)
    except CredentialPolicyError as exc:
        raise CredentialVaultError(exc.code, exc.message, status_code=403) from exc


def _purge_safely(purge_runtime: Callable[[], Any] | None) -> None:
    if not purge_runtime:
        return
    try:
        purge_runtime()
    except Exception:
        # Cleanup failures are deliberately not reflected with provider output or secrets.
        pass


def _require_user_authority(requested_user_id: str,
                            principal: CredentialPrincipal | str,
                            *, admin: bool = False) -> None:
    if not isinstance(principal, CredentialPrincipal):
        if admin or (principal and principal == requested_user_id):
            return
        raise CredentialVaultError(
            "provider_user_binding_denied",
            "caller cannot act for the requested provider identity",
            status_code=403,
        )
    owner = (
        principal.principal_kind == "user"
        and principal.principal_id == requested_user_id
    )
    service = (
        principal.principal_kind in {"agent", "host", "system"}
        and principal.can_use_credentials()
    )
    if principal.admin or owner or service:
        return
    raise CredentialVaultError(
        "provider_user_binding_denied",
        "caller cannot act for the requested provider identity",
        status_code=403,
    )


def enroll_mapping(data: dict[str, Any], *, actor: str, principal_user_id: str,
                   admin: bool = False,
                   repository: ProviderCredentialRepository = default_provider_credential_repository,
                   raise_errors: bool = False) -> dict[str, Any]:
    try:
        command = EnrollProviderConnectionCommand.from_mapping(data)
        _require_user_authority(command.user_id, principal_user_id, admin=admin)
        return repository.enroll(
            project=command.project,
            user_id=command.user_id,
            provider=command.provider,
            provider_account_id=command.provider_account_id,
            auth_type=command.auth_type,
            credential=command.credential.get_secret_value(),
            project_allowlist=command.project_allowlist,
            actor=actor,
            expires_at=command.expires_at,
            refresh_state=command.refresh_state,
            concurrency_policy=command.concurrency_policy,
            audit_provenance={"credential_version": 1},
        )
    except (ValidationError, CredentialVaultError) as exc:
        if raise_errors:
            raise
        return _error(exc, "invalid_provider_enrollment")


def rotate_mapping(data: dict[str, Any], *, actor: str, principal_user_id: str,
                   admin: bool = False,
                   repository: ProviderCredentialRepository = default_provider_credential_repository,
                   raise_errors: bool = False) -> dict[str, Any]:
    try:
        command = RotateProviderConnectionCommand.from_mapping(data)
        return repository.rotate(
            command.credential_reference,
            project=command.project,
            credential=command.credential.get_secret_value(),
            actor=actor,
            expires_at=command.expires_at,
            refresh_state=command.refresh_state,
            principal_user_id=principal_user_id,
            admin=admin,
        )
    except (ValidationError, CredentialVaultError) as exc:
        if raise_errors:
            raise
        return _error(exc, "invalid_provider_rotation")


def revoke_mapping(data: dict[str, Any], *, actor: str, principal_user_id: str,
                   admin: bool = False,
                   repository: ProviderCredentialRepository = default_provider_credential_repository,
                   raise_errors: bool = False) -> dict[str, Any]:
    try:
        command = RevokeProviderConnectionCommand.model_validate(data)
        return repository.revoke(
            command.credential_reference,
            project=command.project,
            actor=actor,
            reason=command.reason,
            principal_user_id=principal_user_id,
            admin=admin,
        )
    except (ValidationError, CredentialVaultError) as exc:
        if raise_errors:
            raise
        return _error(exc, "invalid_provider_revocation")


def delete_mapping(data: dict[str, Any], *, actor: str, principal_user_id: str,
                   admin: bool = False,
                   repository: ProviderCredentialRepository = default_provider_credential_repository,
                   raise_errors: bool = False) -> dict[str, Any]:
    try:
        command = DeleteProviderConnectionCommand.model_validate(data)
        return repository.delete(
            command.credential_reference,
            project=command.project,
            actor=actor,
            reason=command.reason,
            principal_user_id=principal_user_id,
            admin=admin,
        )
    except (ValidationError, CredentialVaultError) as exc:
        if raise_errors:
            raise
        return _error(exc, "invalid_provider_deletion")


def _validate_runtime_binding(command: AcquireProviderCredentialLeaseCommand,
                              principal: CredentialPrincipal) -> None:
    task = store.get_task(command.task_id, project=command.project)
    if not task:
        raise CredentialVaultError(
            "credential_task_binding_invalid", "credential task binding is invalid", status_code=409)

    work_session = store.get_work_session(command.work_session_id, project=command.project)
    if (not work_session or work_session.get("task_id") != command.task_id
            or work_session.get("status") != "active"
            or (work_session.get("health") or {}).get("blocking")):
        raise CredentialVaultError(
            "credential_work_session_binding_invalid",
            "credential work-session binding is invalid",
            status_code=409,
        )

    runner_session = store.get_runner_session(
        command.runner_session_id, project=command.project)
    if (not runner_session or runner_session.get("task_id") != command.task_id
            or runner_session.get("host_id") != command.host_id
            or runner_session.get("stale")
            or str(runner_session.get("status") or "").lower()
            not in {"starting", "ready", "running"}):
        raise CredentialVaultError(
            "credential_runner_binding_invalid",
            "credential runner-session binding is invalid",
            status_code=409,
        )

    claim_id = str(work_session.get("claim_id") or "").strip()
    runner_claim = runner_session.get("claim") or {}
    agent_id = str(work_session.get("agent_id") or "").strip()
    if (not claim_id or runner_session.get("claim_id") != claim_id
            or runner_claim.get("id") != claim_id
            or runner_claim.get("status") != "active"
            or float(runner_claim.get("expires_at") or 0) <= time.time()
            or runner_claim.get("task_id") != command.task_id
            or runner_claim.get("agent_id") != agent_id
            or runner_session.get("agent_id") != agent_id):
        raise CredentialVaultError(
            "credential_claim_binding_invalid",
            "credential claim binding is invalid",
            status_code=409,
        )

    bound_principals = {
        str(work_session.get("principal_id") or "").strip(),
        str(runner_session.get("principal_id") or "").strip(),
        str(runner_claim.get("principal_id") or "").strip(),
    }
    if "" in bound_principals or bound_principals != {principal.principal_id}:
        raise CredentialVaultError(
            "credential_principal_binding_invalid",
            "credential runtime principal binding is invalid",
            status_code=409,
        )
    if (principal.principal_kind == "agent" and principal.principal_id != agent_id):
        raise CredentialVaultError(
            "credential_agent_binding_invalid", "credential agent binding is invalid",
            status_code=409,
        )
    if (principal.principal_kind == "host" and principal.principal_id != command.host_id):
        raise CredentialVaultError(
            "credential_host_binding_invalid", "credential host binding is invalid",
            status_code=409,
        )

    runner_metadata = runner_session.get("metadata") or {}
    if (runner_metadata.get("work_session_id") != command.work_session_id
            or runner_metadata.get("credential_reference") != command.credential_reference
            or runner_metadata.get("provider_account_id") != command.provider_account_id):
        raise CredentialVaultError(
            "credential_runner_account_binding_invalid",
            "credential runner account binding is invalid",
            status_code=409,
        )

    host = next((item for item in store.list_agent_hosts(
        include_stale=True, project=command.project)
        if item.get("host_id") == command.host_id), None)
    if (not host or host.get("stale") or host.get("status") != "online"
            or (host.get("principal_id") or "") != principal.principal_id):
        raise CredentialVaultError(
            "credential_host_binding_invalid", "credential host binding is invalid", status_code=409)


def acquire_lease_mapping(data: dict[str, Any], *, actor: str,
                          principal: dict[str, Any] | CredentialPrincipal,
                          validate_runtime: bool = True,
                          repository: ProviderCredentialRepository = default_provider_credential_repository,
                          raise_errors: bool = False) -> dict[str, Any]:
    try:
        command = AcquireProviderCredentialLeaseCommand.model_validate(data)
        credential_principal = _credential_principal(principal)
        _require_user_authority(command.user_id, credential_principal)
        if validate_runtime:
            _validate_runtime_binding(command, credential_principal)
        return repository.acquire_lease(
            project=command.project,
            credential_reference=command.credential_reference,
            user_id=command.user_id,
            provider=command.provider,
            provider_account_id=command.provider_account_id,
            task_id=command.task_id,
            host_id=command.host_id,
            runner_session_id=command.runner_session_id,
            work_session_id=command.work_session_id,
            ttl_seconds=command.ttl_seconds,
            actor=actor,
            principal=credential_principal,
        )
    except (ValidationError, CredentialVaultError) as exc:
        if raise_errors:
            raise
        return _error(exc, "invalid_provider_credential_binding")


def release_lease_mapping(data: dict[str, Any], *, actor: str,
                          principal: dict[str, Any] | CredentialPrincipal,
                          repository: ProviderCredentialRepository = default_provider_credential_repository,
                          raise_errors: bool = False) -> dict[str, Any]:
    try:
        command = ReleaseProviderCredentialLeaseCommand.model_validate(data)
        credential_principal = _credential_principal(principal)
        return repository.release_lease(
            command.lease_id,
            project=command.project,
            actor=actor,
            reason=command.reason,
            principal=credential_principal,
        )
    except (ValidationError, CredentialVaultError) as exc:
        if raise_errors:
            raise
        return _error(exc, "invalid_provider_credential_release")


def start_with_provider_credential(
        data: dict[str, Any], *, lease_id: str, actor: str,
        start_process: Callable[[str], Any],
        principal: dict[str, Any] | CredentialPrincipal,
        purge_runtime: Callable[[], Any] | None = None,
        repository: ProviderCredentialRepository = default_provider_credential_repository,
        validate_runtime: bool = True) -> dict[str, Any]:
    """Trusted runner bridge: validate/materialize before invoking the process starter.

    This function is intentionally absent from REST and MCP. Its return contract is an
    allowlist, so even a buggy process adapter cannot echo the credential into a receipt.
    """
    try:
        command = AcquireProviderCredentialLeaseCommand.model_validate(data)
        credential_principal = _credential_principal(principal)
        if validate_runtime:
            _validate_runtime_binding(command, credential_principal)
        credential = repository.materialize_for_runtime(
            lease_id,
            project=command.project,
            user_id=command.user_id,
            provider=command.provider,
            provider_account_id=command.provider_account_id,
            task_id=command.task_id,
            host_id=command.host_id,
            runner_session_id=command.runner_session_id,
            work_session_id=command.work_session_id,
            actor=actor,
            principal=credential_principal,
        )
        try:
            started = start_process(credential)
        except Exception:
            repository.fence_materialized_lease(
                lease_id, actor=actor, reason="process_start_failed",
                principal=credential_principal,
            )
            _purge_safely(purge_runtime)
            raise CredentialVaultError(
                "provider_process_start_failed", "provider process failed to start",
                status_code=503,
            )
        started_ok = bool(started.get("started")) if isinstance(started, dict) else bool(started)
        if not started_ok:
            repository.fence_materialized_lease(
                lease_id, actor=actor, reason="process_start_failed",
                principal=credential_principal,
            )
            _purge_safely(purge_runtime)
            raise CredentialVaultError(
                "provider_process_start_failed", "provider process failed to start",
                status_code=503,
            )
        try:
            repository.activate_materialized_lease(
                lease_id, actor=actor, principal=credential_principal)
        except CredentialVaultError:
            _purge_safely(purge_runtime)
            raise
        if isinstance(started, dict):
            allowed = {
                key: started.get(key)
                for key in ("started", "pid", "runner_session_id", "status")
                if key in started
            }
        else:
            allowed = {"started": bool(started)}
        return {"allowed": True, **allowed}
    except (ValidationError, CredentialVaultError) as exc:
        return {"allowed": False, **_error(exc, "provider_launch_denied")}


__all__ = [
    "acquire_lease_mapping",
    "delete_mapping",
    "enroll_mapping",
    "release_lease_mapping",
    "revoke_mapping",
    "rotate_mapping",
    "start_with_provider_credential",
]
