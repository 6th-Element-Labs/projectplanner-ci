"""Messaging command contracts — ``switchboard.messaging.*.v1``."""
from __future__ import annotations

from typing import Any, ClassVar, Mapping, Optional

from pydantic import ConfigDict, Field, field_validator

from ..base import VersionedModel
from ..registry import register

SEND_AGENT_MESSAGE_COMMAND_SCHEMA = "switchboard.messaging.send_agent_message_command.v1"
ACK_MESSAGE_COMMAND_SCHEMA = "switchboard.messaging.ack_message_command.v1"

# Adapter bodies may carry transport-only keys; keep legacy ignore-unknown behavior.
_SEND_MAPPING_KEYS = frozenset({
    "schema", "schema_id", "from_agent", "to_agent", "to", "message", "project",
    "task_id", "task", "requires_ack", "ack_deadline_minutes", "ack_timeout_seconds",
    "ack_timeout_s", "on_ack_timeout", "ack_timeout_action", "signal", "priority",
    "idem_key",
})
_ACK_MAPPING_KEYS = frozenset({
    "schema", "schema_id", "message_id", "id", "project", "response",
})


def _strip_text(value: Any) -> str:
    return str(value or "").strip()


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _coerce_optional_float(value: Any) -> Optional[float]:
    if value in (None, "", 0, 0.0, "0", "0.0"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("must be a number") from exc


class SendAgentMessageCommand(VersionedModel):
    """Transport-neutral input for directed agent messaging."""

    SCHEMA: ClassVar[str] = SEND_AGENT_MESSAGE_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=SEND_AGENT_MESSAGE_COMMAND_SCHEMA, alias="schema")
    from_agent: str
    to_agent: str
    message: str
    project: str = "maxwell"
    task_id: str = ""
    requires_ack: bool = False
    ack_deadline_minutes: Optional[float] = None
    ack_timeout_seconds: Optional[float] = None
    on_ack_timeout: str = "notify_sender"
    signal: str = ""
    priority: int = 0
    idem_key: str = ""

    @field_validator(
        "from_agent", "to_agent", "message", "project", "task_id",
        "on_ack_timeout", "signal", "idem_key", mode="before",
    )
    @classmethod
    def _strip(cls, value: Any) -> str:
        return _strip_text(value)

    @field_validator("requires_ack", mode="before")
    @classmethod
    def _bool(cls, value: Any) -> bool:
        return _coerce_bool(value)

    @field_validator("ack_deadline_minutes", mode="before")
    @classmethod
    def _deadline_minutes(cls, value: Any) -> Optional[float]:
        return _coerce_optional_float(value)

    @field_validator("ack_timeout_seconds", mode="before")
    @classmethod
    def _timeout_seconds(cls, value: Any) -> Optional[float]:
        return _coerce_optional_float(value)

    @field_validator("priority", mode="before")
    @classmethod
    def _priority(cls, value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("priority must be an integer") from exc

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SendAgentMessageCommand:
        raw = dict(value or {})
        data = {key: raw[key] for key in _SEND_MAPPING_KEYS if key in raw}
        if not data.get("to_agent"):
            data["to_agent"] = data.get("to") or ""
        if not data.get("task_id"):
            data["task_id"] = data.get("task") or ""
        # IXP accepts ack_timeout_s as a seconds alias; persistence converts
        # seconds → minutes when ack_deadline_minutes is unset.
        if data.get("ack_timeout_seconds") in (None, "", 0, "0") and (
                data.get("ack_timeout_s") not in (None, "", 0, "0")):
            data["ack_timeout_seconds"] = data.get("ack_timeout_s")
        if not data.get("on_ack_timeout"):
            data["on_ack_timeout"] = data.get("ack_timeout_action") or "notify_sender"
        data.pop("to", None)
        data.pop("task", None)
        data.pop("ack_timeout_s", None)
        data.pop("ack_timeout_action", None)
        return cls.model_validate(data)


class AckMessageCommand(VersionedModel):
    """Transport-neutral input for acknowledging a directed message."""

    SCHEMA: ClassVar[str] = ACK_MESSAGE_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=ACK_MESSAGE_COMMAND_SCHEMA, alias="schema")
    message_id: int
    project: str = "maxwell"
    response: str = ""

    @field_validator("project", "response", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> str:
        return _strip_text(value)

    @field_validator("message_id", mode="before")
    @classmethod
    def _message_id(cls, value: Any) -> int:
        if value in (None, ""):
            raise ValueError("message_id is required")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("message_id must be an integer") from exc

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> AckMessageCommand:
        raw = dict(value or {})
        data = {key: raw[key] for key in _ACK_MAPPING_KEYS if key in raw}
        if data.get("message_id") in (None, "") and data.get("id") not in (None, ""):
            data["message_id"] = data.get("id")
        data.pop("id", None)
        return cls.model_validate(data)


register(SendAgentMessageCommand)
register(AckMessageCommand)
