"""Wake command contracts — ``switchboard.wake.*.v1``."""
from __future__ import annotations

import json
from typing import Any, ClassVar, Mapping

from pydantic import ConfigDict, Field, field_validator

from ..base import VersionedModel
from ..registry import register

REQUEST_WAKE_COMMAND_SCHEMA = "switchboard.wake.request_wake_command.v1"
CLAIM_WAKE_COMMAND_SCHEMA = "switchboard.wake.claim_wake_command.v1"
COMPLETE_WAKE_COMMAND_SCHEMA = "switchboard.wake.complete_wake_command.v1"


def parse_object_payload(value: Any, *, field_name: str) -> dict[str, Any]:
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


class RequestWakeCommand(VersionedModel):
    """Transport-neutral input for creating a durable wake intent."""

    SCHEMA: ClassVar[str] = REQUEST_WAKE_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=REQUEST_WAKE_COMMAND_SCHEMA, alias="schema")
    selector: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    source: str = ""
    policy: dict[str, Any] = Field(default_factory=dict)
    task_id: str = ""
    idem_key: str = ""
    project: str = "maxwell"

    @field_validator("reason", "source", "task_id", "idem_key", "project", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("selector", mode="before")
    @classmethod
    def _coerce_selector(cls, value: Any) -> dict[str, Any]:
        return parse_object_payload(value, field_name="selector")

    @field_validator("policy", mode="before")
    @classmethod
    def _coerce_policy(cls, value: Any) -> dict[str, Any]:
        return parse_object_payload(value, field_name="policy")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RequestWakeCommand:
        data = dict(value or {})
        if "selector" not in data and "selector_json" in data:
            data["selector"] = data.get("selector_json")
        if "policy" not in data and "policy_json" in data:
            data["policy"] = data.get("policy_json")
        if not data.get("task_id"):
            data["task_id"] = data.get("task") or ""
        data.pop("selector_json", None)
        data.pop("policy_json", None)
        data.pop("task", None)
        return cls.model_validate(data)


class ClaimWakeCommand(VersionedModel):
    """Transport-neutral input for claiming one pending wake intent."""

    SCHEMA: ClassVar[str] = CLAIM_WAKE_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=CLAIM_WAKE_COMMAND_SCHEMA, alias="schema")
    host_id: str
    wake_id: str
    project: str = "maxwell"

    @field_validator("host_id", "wake_id", "project", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ClaimWakeCommand:
        data = dict(value or {})
        if not data.get("wake_id"):
            data["wake_id"] = data.get("id") or ""
        data.pop("id", None)
        return cls.model_validate(data)


class CompleteWakeCommand(VersionedModel):
    """Transport-neutral input for completing a claimed wake intent."""

    SCHEMA: ClassVar[str] = COMPLETE_WAKE_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=COMPLETE_WAKE_COMMAND_SCHEMA, alias="schema")
    wake_id: str
    runner_session_id: str = ""
    agent_id: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    project: str = "maxwell"

    @field_validator("wake_id", "runner_session_id", "agent_id", "project", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("result", mode="before")
    @classmethod
    def _coerce_result(cls, value: Any) -> dict[str, Any]:
        return parse_object_payload(value, field_name="result")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CompleteWakeCommand:
        data = dict(value or {})
        if not data.get("wake_id"):
            data["wake_id"] = data.get("id") or ""
        if "result" not in data and "result_json" in data:
            data["result"] = data.get("result_json")
        data.pop("id", None)
        data.pop("result_json", None)
        return cls.model_validate(data)


register(RequestWakeCommand)
register(ClaimWakeCommand)
register(CompleteWakeCommand)
