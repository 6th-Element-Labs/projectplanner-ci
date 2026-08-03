"""Claim command contracts — ``switchboard.claim.*.v1``."""
from __future__ import annotations

import json
import re
from typing import Any, ClassVar, Mapping

from pydantic import ConfigDict, Field, field_validator, model_validator

from ..base import VersionedModel
from ..registry import register

CLAIM_TASK_COMMAND_SCHEMA = "switchboard.claim.claim_task_command.v1"
CLAIM_NEXT_COMMAND_SCHEMA = "switchboard.claim.claim_next_command.v1"
COMPLETE_CLAIM_COMMAND_SCHEMA = "switchboard.claim.complete_claim_command.v1"
FINISH_TURN_COMMAND_SCHEMA = "switchboard.claim.finish_turn_command.v1"


def coerce_string_list(value: Any, *, upper: bool = False) -> tuple[str, ...]:
    """Normalize list fields that may arrive as a list or comma/newline string."""
    if value is None or value == "":
        return ()
    raw = value if isinstance(value, list) else [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        for part in str(item).replace("\n", ",").split(","):
            token = part.strip()
            if upper:
                token = token.upper()
            if token and token not in seen:
                seen.add(token)
                out.append(token)
    return tuple(out)


def parse_work_session(value: Any) -> dict[str, Any]:
    """Accept a dict, JSON object string, or empty value as a work_session payload."""
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
            raise ValueError("work_session_json must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("work_session_json must decode to an object")
        return parsed
    raise ValueError("work_session must be an object or JSON object string")


class ClaimTaskCommand(VersionedModel):
    """Transport-neutral input for claiming one exact ready task."""

    SCHEMA: ClassVar[str] = CLAIM_TASK_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=CLAIM_TASK_COMMAND_SCHEMA, alias="schema")
    task_id: str
    agent_id: str
    project: str = "maxwell"
    ttl_seconds: int = 1800
    idem_key: str = ""
    override_identity_risk: bool = False
    work_session_id: str = ""
    work_session: dict[str, Any] = Field(default_factory=dict)
    session_policy_profile: str = ""
    require_work_session: bool = False

    @field_validator("task_id", "agent_id", "project", "idem_key",
                     "work_session_id", "session_policy_profile", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("ttl_seconds", mode="before")
    @classmethod
    def _coerce_ttl(cls, value: Any) -> int:
        try:
            return max(60, int(value or 1800))
        except (TypeError, ValueError) as exc:
            raise ValueError("ttl_seconds must be an integer") from exc

    @field_validator("override_identity_risk", "require_work_session", mode="before")
    @classmethod
    def _coerce_bool(cls, value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @field_validator("work_session", mode="before")
    @classmethod
    def _coerce_work_session(cls, value: Any) -> dict[str, Any]:
        return parse_work_session(value)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ClaimTaskCommand:
        data = dict(value or {})
        if "work_session" not in data and "work_session_json" in data:
            data["work_session"] = data.get("work_session_json")
        if "ttl_seconds" not in data and "ttl_s" in data:
            data["ttl_seconds"] = data.get("ttl_s")
        if "session_policy_profile" not in data and data.get("policy_profile"):
            data["session_policy_profile"] = data.get("policy_profile")
        if not data.get("task_id"):
            data["task_id"] = data.get("task") or ""
        data.pop("work_session_json", None)
        data.pop("ttl_s", None)
        data.pop("policy_profile", None)
        data.pop("task", None)
        return cls.model_validate(data)


class ClaimNextCommand(VersionedModel):
    """Transport-neutral input for scheduler claim_next."""

    SCHEMA: ClassVar[str] = CLAIM_NEXT_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=CLAIM_NEXT_COMMAND_SCHEMA, alias="schema")
    agent_id: str
    project: str = "maxwell"
    lanes: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    max_risk: str = ""
    max_budget_usd: float | None = None
    ttl_seconds: int = 1800
    idem_key: str = ""
    override_identity_risk: bool = False
    work_session_id: str = ""
    work_session: dict[str, Any] = Field(default_factory=dict)
    session_policy_profile: str = ""
    require_work_session: bool = False
    deliverable_id: str = ""
    board_id: str = ""
    mission_id: str = ""
    milestone_id: str = ""

    @field_validator("agent_id", "project", "max_risk", "idem_key", "work_session_id",
                     "session_policy_profile", "deliverable_id", "board_id",
                     "mission_id", "milestone_id", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("lanes", mode="before")
    @classmethod
    def _coerce_lanes(cls, value: Any) -> tuple[str, ...]:
        return coerce_string_list(value, upper=True)

    @field_validator("capabilities", mode="before")
    @classmethod
    def _coerce_capabilities(cls, value: Any) -> tuple[str, ...]:
        return coerce_string_list(value, upper=False)

    @field_validator("ttl_seconds", mode="before")
    @classmethod
    def _coerce_ttl(cls, value: Any) -> int:
        try:
            return max(60, int(value or 1800))
        except (TypeError, ValueError) as exc:
            raise ValueError("ttl_seconds must be an integer") from exc

    @field_validator("max_budget_usd", mode="before")
    @classmethod
    def _coerce_budget(cls, value: Any) -> float | None:
        if value in (None, "", 0, 0.0):
            return None
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_budget_usd must be a number") from exc

    @field_validator("override_identity_risk", "require_work_session", mode="before")
    @classmethod
    def _coerce_bool(cls, value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @field_validator("work_session", mode="before")
    @classmethod
    def _coerce_work_session(cls, value: Any) -> dict[str, Any]:
        return parse_work_session(value)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ClaimNextCommand:
        data = dict(value or {})
        if "work_session" not in data and "work_session_json" in data:
            data["work_session"] = data.get("work_session_json")
        if "ttl_seconds" not in data and "ttl_s" in data:
            data["ttl_seconds"] = data.get("ttl_s")
        if "session_policy_profile" not in data and data.get("policy_profile"):
            data["session_policy_profile"] = data.get("policy_profile")
        lanes = data.get("lanes")
        if not lanes:
            lanes = data.get("lane")
        data["lanes"] = lanes
        data.pop("work_session_json", None)
        data.pop("ttl_s", None)
        data.pop("policy_profile", None)
        data.pop("lane", None)
        return cls.model_validate(data)


class CompleteClaimCommand(VersionedModel):
    """Transport-neutral input for completing one claim."""

    SCHEMA: ClassVar[str] = COMPLETE_CLAIM_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True)

    schema_id: str = Field(default=COMPLETE_CLAIM_COMMAND_SCHEMA, alias="schema")
    claim_id: str
    project: str = "maxwell"
    evidence: Any = ""
    final_status: str = ""
    mission_project: str = ""

    @field_validator("claim_id", "project", "final_status", "mission_project", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CompleteClaimCommand:
        data = dict(value or {})
        return cls.model_validate(data)


class FinishTurnCommand(VersionedModel):
    """One bounded, generation-fenced implementation completion submission.

    The contract deliberately has no status, next-role, merge, Done, or host-control
    fields.  Those decisions remain server-owned after the evidence is accepted.
    """

    SCHEMA: ClassVar[str] = FINISH_TURN_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str = Field(default=FINISH_TURN_COMMAND_SCHEMA, alias="schema")
    task_id: str
    claim_id: str
    execution_id: str
    generation: int
    work_session_id: str
    branch: str
    head_sha: str
    pr_number: int
    pr_url: str
    executed_test_run_id: str
    git_diff_check: str
    project: str = "maxwell"
    mission_project: str = ""

    @field_validator(
        "task_id", "claim_id", "execution_id", "work_session_id", "branch",
        "head_sha", "pr_url", "executed_test_run_id", "git_diff_check",
        "project", "mission_project", mode="before",
    )
    @classmethod
    def _strip_finish_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("task_id", mode="after")
    @classmethod
    def _normalize_task_id(cls, value: str) -> str:
        return value.upper()

    @field_validator("head_sha", mode="after")
    @classmethod
    def _normalize_head_sha(cls, value: str) -> str:
        return value.lower()

    @field_validator("git_diff_check", mode="after")
    @classmethod
    def _normalize_diff_check(cls, value: str) -> str:
        normalized = value.lower().replace("_", " ").strip()
        if normalized in {"clean", "pass", "passed", "success"}:
            return "passed"
        return normalized

    @field_validator("generation", "pr_number", mode="before")
    @classmethod
    def _coerce_positive_int(cls, value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("must be an integer") from exc

    @model_validator(mode="after")
    def _validate_finish_turn(self) -> "FinishTurnCommand":
        required = {
            "task_id": self.task_id,
            "claim_id": self.claim_id,
            "execution_id": self.execution_id,
            "work_session_id": self.work_session_id,
            "branch": self.branch,
            "pr_url": self.pr_url,
            "executed_test_run_id": self.executed_test_run_id,
            "project": self.project,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError("required fields missing: " + ", ".join(missing))
        if self.generation <= 0:
            raise ValueError("generation must be positive")
        if self.pr_number <= 0:
            raise ValueError("pr_number must be positive")
        if not re.fullmatch(r"[0-9a-f]{40}", self.head_sha):
            raise ValueError("head_sha must be a 40-character lowercase git SHA")
        match = re.search(r"/pull/(\d+)(?:/)?$", self.pr_url)
        if not match or int(match.group(1)) != self.pr_number:
            raise ValueError("pr_url must identify pr_number")
        if self.git_diff_check != "passed":
            raise ValueError("git_diff_check must report passed")
        return self

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FinishTurnCommand":
        return cls.model_validate(dict(value or {}))

    def completion_evidence(self) -> dict[str, Any]:
        """Return only evidence fields consumed by the existing completion owner."""
        return {
            "branch": self.branch,
            "head_sha": self.head_sha,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "git_diff_check": "passed",
            "executed_test_run_id": self.executed_test_run_id,
            "work_session_id": self.work_session_id,
            "execution_id": self.execution_id,
            "execution_generation": self.generation,
        }


register(ClaimTaskCommand)
register(ClaimNextCommand)
register(CompleteClaimCommand)
register(FinishTurnCommand)
