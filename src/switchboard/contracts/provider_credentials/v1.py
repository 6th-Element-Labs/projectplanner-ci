"""Versioned CO-6 provider-credential vault command contracts."""
from __future__ import annotations

import json
from typing import Any, ClassVar, Mapping

from pydantic import ConfigDict, Field, SecretStr, field_validator

from ..base import VersionedModel
from ..registry import register


ENROLL_PROVIDER_CONNECTION_SCHEMA = "switchboard.provider_connection.enroll_command.v1"
ROTATE_PROVIDER_CONNECTION_SCHEMA = "switchboard.provider_connection.rotate_command.v1"
REVOKE_PROVIDER_CONNECTION_SCHEMA = "switchboard.provider_connection.revoke_command.v1"
DELETE_PROVIDER_CONNECTION_SCHEMA = "switchboard.provider_connection.delete_command.v1"
ACQUIRE_PROVIDER_CREDENTIAL_LEASE_SCHEMA = (
    "switchboard.provider_credential.acquire_lease_command.v1"
)
RELEASE_PROVIDER_CREDENTIAL_LEASE_SCHEMA = (
    "switchboard.provider_credential.release_lease_command.v1"
)


def _parse_object(value: Any, field_name: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} must be valid JSON") from exc
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"{field_name} must be an object")


def _parse_string_list(value: Any) -> tuple[str, ...]:
    values = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
    result: list[str] = []
    for raw in values:
        item = str(raw or "").strip().lower()
        if item and item not in result:
            result.append(item)
    return tuple(result)


class EnrollProviderConnectionCommand(VersionedModel):
    """Enroll one personal-subscription provider identity without serializing its secret."""

    SCHEMA: ClassVar[str] = ENROLL_PROVIDER_CONNECTION_SCHEMA
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    schema_id: str = Field(default=ENROLL_PROVIDER_CONNECTION_SCHEMA, alias="schema")
    project: str
    user_id: str
    provider: str
    provider_account_id: str
    auth_type: str
    credential: SecretStr
    project_allowlist: tuple[str, ...]
    expires_at: float | None = None
    refresh_state: str = "not_applicable"
    concurrency_policy: dict[str, Any] = Field(
        default_factory=lambda: {"mode": "exclusive", "max_parallel": 1}
    )

    @field_validator(
        "project", "user_id", "provider", "provider_account_id", "auth_type",
        "refresh_state", mode="before",
    )
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("provider", mode="after")
    @classmethod
    def _lower_provider(cls, value: str) -> str:
        return value.lower()

    @field_validator("project_allowlist", mode="before")
    @classmethod
    def _allowlist(cls, value: Any) -> tuple[str, ...]:
        return _parse_string_list(value)

    @field_validator("concurrency_policy", mode="before")
    @classmethod
    def _policy(cls, value: Any) -> dict[str, Any]:
        return _parse_object(value, "concurrency_policy")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EnrollProviderConnectionCommand":
        data = dict(value or {})
        if "credential" not in data:
            data["credential"] = data.pop("auth_capsule", None)
        return cls.model_validate(data)


class RotateProviderConnectionCommand(VersionedModel):
    SCHEMA: ClassVar[str] = ROTATE_PROVIDER_CONNECTION_SCHEMA
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    schema_id: str = Field(default=ROTATE_PROVIDER_CONNECTION_SCHEMA, alias="schema")
    project: str
    credential_reference: str
    credential: SecretStr
    expires_at: float | None = None
    refresh_state: str = "fresh"

    @field_validator("project", "credential_reference", "refresh_state", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RotateProviderConnectionCommand":
        data = dict(value or {})
        if "credential" not in data:
            data["credential"] = data.pop("auth_capsule", None)
        return cls.model_validate(data)


class RevokeProviderConnectionCommand(VersionedModel):
    SCHEMA: ClassVar[str] = REVOKE_PROVIDER_CONNECTION_SCHEMA
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    schema_id: str = Field(default=REVOKE_PROVIDER_CONNECTION_SCHEMA, alias="schema")
    project: str
    credential_reference: str
    reason: str

    @field_validator("project", "credential_reference", "reason", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()


class DeleteProviderConnectionCommand(RevokeProviderConnectionCommand):
    SCHEMA: ClassVar[str] = DELETE_PROVIDER_CONNECTION_SCHEMA
    schema_id: str = Field(default=DELETE_PROVIDER_CONNECTION_SCHEMA, alias="schema")


class AcquireProviderCredentialLeaseCommand(VersionedModel):
    SCHEMA: ClassVar[str] = ACQUIRE_PROVIDER_CREDENTIAL_LEASE_SCHEMA
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    schema_id: str = Field(default=ACQUIRE_PROVIDER_CREDENTIAL_LEASE_SCHEMA, alias="schema")
    project: str
    credential_reference: str
    user_id: str
    provider: str
    provider_account_id: str
    task_id: str
    host_id: str
    runner_session_id: str
    work_session_id: str
    account_affinity_id: str = ""
    ttl_seconds: int = Field(default=900, ge=30, le=3600)

    @field_validator(
        "project", "credential_reference", "user_id", "provider",
        "provider_account_id", "task_id", "host_id", "runner_session_id",
        "work_session_id", "account_affinity_id", mode="before",
    )
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("provider", mode="after")
    @classmethod
    def _lower_provider(cls, value: str) -> str:
        return value.lower()


class ReleaseProviderCredentialLeaseCommand(VersionedModel):
    SCHEMA: ClassVar[str] = RELEASE_PROVIDER_CREDENTIAL_LEASE_SCHEMA
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    schema_id: str = Field(default=RELEASE_PROVIDER_CREDENTIAL_LEASE_SCHEMA, alias="schema")
    project: str
    lease_id: str
    reason: str = "released"

    @field_validator("project", "lease_id", "reason", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()


for _model in (
    EnrollProviderConnectionCommand,
    RotateProviderConnectionCommand,
    RevokeProviderConnectionCommand,
    DeleteProviderConnectionCommand,
    AcquireProviderCredentialLeaseCommand,
    ReleaseProviderCredentialLeaseCommand,
):
    register(_model)
