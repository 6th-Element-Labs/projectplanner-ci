"""Transport-neutral Agent Host enrollment lifecycle commands."""
from __future__ import annotations

from typing import Any, Mapping

from pydantic import ValidationError

from switchboard.application.contracts.agents import (
    BeginHostEnrollmentCommand,
    CompleteHostEnrollmentCommand,
    FinalizeHostEnrollmentCommand,
    RevokeHostIdentityCommand,
    RotateHostIdentityCommand,
)
from switchboard.contracts import validation_error_message
from switchboard.storage.repositories.agent_host_enrollments import (
    begin_agent_host_enrollment,
    complete_agent_host_enrollment,
    finalize_agent_host_enrollment,
    revoke_agent_host_identity,
    rotate_agent_host_identity,
)


def _validation_error(exc: ValidationError) -> dict[str, Any]:
    message = validation_error_message(exc)
    return {"error": "invalid_agent_host_enrollment", "error_code":
            "invalid_agent_host_enrollment", "message": message}


def begin_mapping_result(data: Mapping[str, Any], *, actor: str,
                         principal_id: str) -> dict[str, Any]:
    try:
        command = BeginHostEnrollmentCommand.model_validate(dict(data or {}))
    except ValidationError as exc:
        return _validation_error(exc)
    return begin_agent_host_enrollment(
        owner_user_id=command.owner_user_id,
        requested_host_id=command.requested_host_id,
        tenant_allowlist=command.tenant_allowlist,
        project_allowlist=command.project_allowlist or [command.project],
        provider_allowlist=command.provider_allowlist,
        package_version=command.package_version,
        ttl_seconds=command.ttl_seconds,
        created_by_principal_id=principal_id,
        actor=actor,
        project=command.project,
    )


def complete_mapping_result(data: Mapping[str, Any]) -> dict[str, Any]:
    try:
        command = CompleteHostEnrollmentCommand.model_validate(dict(data or {}))
    except ValidationError as exc:
        return _validation_error(exc)
    return complete_agent_host_enrollment(
        bootstrap_code=command.bootstrap_code,
        hostname=command.hostname,
        platform=command.platform,
        public_key_fingerprint=command.public_key_fingerprint,
        completion_recovery_secret=command.completion_recovery_secret,
        agent_host_version=command.agent_host_version,
        project=command.project,
    )


def finalize_mapping_result(data: Mapping[str, Any], *, actor: str,
                            principal_id: str) -> dict[str, Any]:
    try:
        command = FinalizeHostEnrollmentCommand.model_validate(dict(data or {}))
    except ValidationError as exc:
        return _validation_error(exc)
    return finalize_agent_host_enrollment(
        enrollment_id=command.enrollment_id,
        host_id=command.host_id,
        principal_id=principal_id,
        actor=actor,
        project=command.project,
    )


def rotate_mapping_result(data: Mapping[str, Any], *, actor: str,
                          principal_id: str) -> dict[str, Any]:
    try:
        command = RotateHostIdentityCommand.model_validate(dict(data or {}))
    except ValidationError as exc:
        return _validation_error(exc)
    return rotate_agent_host_identity(
        host_id=command.host_id,
        principal_id=principal_id,
        public_key_fingerprint=command.public_key_fingerprint,
        actor=actor,
        project=command.project,
    )


def revoke_mapping_result(data: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
    try:
        command = RevokeHostIdentityCommand.model_validate(dict(data or {}))
    except ValidationError as exc:
        return _validation_error(exc)
    return revoke_agent_host_identity(
        host_id=command.host_id,
        actor=actor,
        reason=command.reason,
        final_status=command.final_status,
        project=command.project,
    )


__all__ = [
    "begin_mapping_result",
    "complete_mapping_result",
    "finalize_mapping_result",
    "rotate_mapping_result",
    "revoke_mapping_result",
]
