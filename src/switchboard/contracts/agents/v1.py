"""Agent registry command contracts — ``switchboard.agent.*.v1``."""
from __future__ import annotations

import json
from typing import Any, ClassVar, Mapping

from pydantic import ConfigDict, Field, field_validator

from ..base import VersionedModel
from ..registry import register

REGISTER_AGENT_COMMAND_SCHEMA = "switchboard.agent.register_agent_command.v1"
REGISTER_HOST_COMMAND_SCHEMA = "switchboard.agent.register_host_command.v1"
BEGIN_HOST_ENROLLMENT_COMMAND_SCHEMA = "switchboard.agent.begin_host_enrollment_command.v1"
COMPLETE_HOST_ENROLLMENT_COMMAND_SCHEMA = "switchboard.agent.complete_host_enrollment_command.v1"
ROTATE_HOST_IDENTITY_COMMAND_SCHEMA = "switchboard.agent.rotate_host_identity_command.v1"
REVOKE_HOST_IDENTITY_COMMAND_SCHEMA = "switchboard.agent.revoke_host_identity_command.v1"


def parse_json_object(value: Any, *, field_name: str) -> dict[str, Any]:
    """Accept a dict, JSON object string, or empty value as an object payload."""
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"{field_name} must decode to an object")
        return parsed
    raise ValueError(f"{field_name} must be an object or JSON object string")


def parse_json_list(value: Any, *, field_name: str) -> list[Any]:
    """Accept a list, JSON array string, or empty value as a list payload."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} must be valid JSON") from exc
        if not isinstance(parsed, list):
            raise ValueError(f"{field_name} must decode to an array")
        return list(parsed)
    raise ValueError(f"{field_name} must be an array or JSON array string")


class RegisterAgentCommand(VersionedModel):
    """Transport-neutral input for registering a live agent session."""

    SCHEMA: ClassVar[str] = REGISTER_AGENT_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=REGISTER_AGENT_COMMAND_SCHEMA, alias="schema")
    agent_id: str
    runtime: str
    project: str = "maxwell"
    model: str = ""
    lane: str = ""
    task_id: str = ""
    ttl_s: int = 120
    control: dict[str, Any] = Field(default_factory=dict)
    protocol: dict[str, Any] = Field(default_factory=dict)

    @field_validator("agent_id", "runtime", "project", "model", "lane", "task_id",
                     mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("ttl_s", mode="before")
    @classmethod
    def _coerce_ttl(cls, value: Any) -> int:
        try:
            return max(10, int(value or 120))
        except (TypeError, ValueError) as exc:
            raise ValueError("ttl_s must be an integer") from exc

    @field_validator("control", mode="before")
    @classmethod
    def _coerce_control(cls, value: Any) -> dict[str, Any]:
        return parse_json_object(value, field_name="control")

    @field_validator("protocol", mode="before")
    @classmethod
    def _coerce_protocol(cls, value: Any) -> dict[str, Any]:
        return parse_json_object(value, field_name="protocol")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RegisterAgentCommand:
        data = dict(value or {})
        if "control" not in data and "control_json" in data:
            data["control"] = data.get("control_json")
        if "protocol" not in data and "protocol_json" in data:
            data["protocol"] = data.get("protocol_json")
        if "ttl_s" not in data and "ttl_seconds" in data:
            data["ttl_s"] = data.get("ttl_seconds")
        if not data.get("task_id"):
            data["task_id"] = data.get("task") or ""
        data.pop("control_json", None)
        data.pop("protocol_json", None)
        data.pop("ttl_seconds", None)
        data.pop("task", None)
        return cls.model_validate(data)


class RegisterHostCommand(VersionedModel):
    """Transport-neutral input for registering an Agent Host inventory record."""

    SCHEMA: ClassVar[str] = REGISTER_HOST_COMMAND_SCHEMA
    # Host inventories historically carry advisory fields (e.g. top-level policy)
    # that store.register_host ignores. Match that ignore-unknown behavior so
    # Agent Host daemons keep registering after the application-command cutover.
    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_id: str = Field(default=REGISTER_HOST_COMMAND_SCHEMA, alias="schema")
    host_id: str
    project: str = "maxwell"
    hostname: str = ""
    repo_root: str = ""
    agent_host_version: str = "0.1.0"
    runtimes: list[Any] = Field(default_factory=list)
    limits: dict[str, Any] = Field(default_factory=dict)
    capacity: dict[str, Any] = Field(default_factory=dict)
    heartbeat_ttl_s: int = 60
    active_sessions: int | None = None

    @field_validator("host_id", "project", "hostname", "repo_root",
                     "agent_host_version", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("heartbeat_ttl_s", mode="before")
    @classmethod
    def _coerce_ttl(cls, value: Any) -> int:
        try:
            return max(10, int(value or 60))
        except (TypeError, ValueError) as exc:
            raise ValueError("heartbeat_ttl_s must be an integer") from exc

    @field_validator("runtimes", mode="before")
    @classmethod
    def _coerce_runtimes(cls, value: Any) -> list[Any]:
        return parse_json_list(value, field_name="runtimes")

    @field_validator("limits", mode="before")
    @classmethod
    def _coerce_limits(cls, value: Any) -> dict[str, Any]:
        return parse_json_object(value, field_name="limits")

    @field_validator("capacity", mode="before")
    @classmethod
    def _coerce_capacity(cls, value: Any) -> dict[str, Any]:
        return parse_json_object(value, field_name="capacity")

    @field_validator("active_sessions", mode="before")
    @classmethod
    def _coerce_active_sessions(cls, value: Any) -> int | None:
        if value is None or value == "" or value == -1:
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("active_sessions must be an integer") from exc

    def to_inventory(self) -> dict[str, Any]:
        """Shape expected by ``store.register_host``."""
        inventory: dict[str, Any] = {
            "host_id": self.host_id,
            "hostname": self.hostname,
            "repo_root": self.repo_root,
            "agent_host_version": self.agent_host_version or "0.1.0",
            "runtimes": list(self.runtimes),
            "limits": dict(self.limits),
            "capacity": dict(self.capacity),
            "heartbeat_ttl_s": self.heartbeat_ttl_s,
        }
        if self.active_sessions is not None:
            inventory["active_sessions"] = self.active_sessions
        return inventory

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RegisterHostCommand:
        data = dict(value or {})
        if "runtimes" not in data and "runtimes_json" in data:
            data["runtimes"] = data.get("runtimes_json")
        if "limits" not in data and "limits_json" in data:
            data["limits"] = data.get("limits_json")
        if "capacity" not in data and "capacity_json" in data:
            data["capacity"] = data.get("capacity_json")
        if "heartbeat_ttl_s" not in data and "ttl_s" in data:
            data["heartbeat_ttl_s"] = data.get("ttl_s")
        data.pop("runtimes_json", None)
        data.pop("limits_json", None)
        data.pop("capacity_json", None)
        data.pop("ttl_s", None)
        return cls.model_validate(data)


class BeginHostEnrollmentCommand(VersionedModel):
    """Operator-authorized request for a short-lived bootstrap ceremony."""

    SCHEMA: ClassVar[str] = BEGIN_HOST_ENROLLMENT_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=BEGIN_HOST_ENROLLMENT_COMMAND_SCHEMA, alias="schema")
    project: str = "switchboard"
    owner_user_id: str
    requested_host_id: str = ""
    tenant_allowlist: list[str] = Field(default_factory=list)
    project_allowlist: list[str] = Field(default_factory=list)
    provider_allowlist: list[str] = Field(default_factory=list)
    package_version: str = ""
    ttl_seconds: int = 600

    @field_validator("project", "owner_user_id", "requested_host_id", "package_version",
                     mode="before")
    @classmethod
    def _strip_enrollment_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("tenant_allowlist", "project_allowlist", "provider_allowlist",
                     mode="before")
    @classmethod
    def _coerce_allowlist(cls, value: Any) -> list[str]:
        return [str(item).strip() for item in parse_json_list(
            value, field_name="allowlist") if str(item).strip()]

    @field_validator("ttl_seconds", mode="before")
    @classmethod
    def _coerce_bootstrap_ttl(cls, value: Any) -> int:
        try:
            return min(900, max(60, int(value or 600)))
        except (TypeError, ValueError) as exc:
            raise ValueError("ttl_seconds must be an integer") from exc


class CompleteHostEnrollmentCommand(VersionedModel):
    """Single-use bootstrap completion with bounded response-loss recovery."""

    SCHEMA: ClassVar[str] = COMPLETE_HOST_ENROLLMENT_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=COMPLETE_HOST_ENROLLMENT_COMMAND_SCHEMA, alias="schema")
    project: str = "switchboard"
    bootstrap_code: str
    hostname: str
    platform: str
    public_key_fingerprint: str
    completion_recovery_secret: str
    agent_host_version: str = ""

    @field_validator("project", "bootstrap_code", "hostname", "platform",
                     "public_key_fingerprint", "completion_recovery_secret",
                     "agent_host_version", mode="before")
    @classmethod
    def _strip_completion_text(cls, value: Any) -> str:
        return str(value or "").strip()


class RotateHostIdentityCommand(VersionedModel):
    """Authenticated rotation of host bearer and key fingerprint."""

    SCHEMA: ClassVar[str] = ROTATE_HOST_IDENTITY_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=ROTATE_HOST_IDENTITY_COMMAND_SCHEMA, alias="schema")
    project: str = "switchboard"
    host_id: str
    public_key_fingerprint: str

    @field_validator("project", "host_id", "public_key_fingerprint", mode="before")
    @classmethod
    def _strip_rotation_text(cls, value: Any) -> str:
        return str(value or "").strip()


class RevokeHostIdentityCommand(VersionedModel):
    """Authenticated owner/operator revocation of a personal host identity."""

    SCHEMA: ClassVar[str] = REVOKE_HOST_IDENTITY_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=REVOKE_HOST_IDENTITY_COMMAND_SCHEMA, alias="schema")
    project: str = "switchboard"
    host_id: str
    reason: str = "host_revoke"
    final_status: str = "revoked"

    @field_validator("project", "host_id", "reason", "final_status", mode="before")
    @classmethod
    def _strip_revocation_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("final_status")
    @classmethod
    def _validate_final_status(cls, value: str) -> str:
        if value not in {"revoked", "uninstalled"}:
            raise ValueError("final_status must be revoked or uninstalled")
        return value


register(RegisterAgentCommand)
register(RegisterHostCommand)
register(BeginHostEnrollmentCommand)
register(CompleteHostEnrollmentCommand)
register(RotateHostIdentityCommand)
register(RevokeHostIdentityCommand)
