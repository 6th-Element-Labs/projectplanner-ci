"""Versioned contract for typed executed-test evidence (COORD-62).

BREAKDOWN 20: the executed-test contract existed only as a parser inside the
completion/merge gates, so a runner with the right facts could still misfile
them (``executed_tests``, ``output_log_sha256``, …). This command makes the
wrong key unrepresentable: the input schema IS the evidence contract, one
canonical hash key, and the server stamps ``completed_at`` itself.
"""
from __future__ import annotations

import re
from typing import Any, ClassVar, Mapping

from pydantic import ConfigDict, Field, field_validator, model_validator

from ..base import VersionedModel
from ..registry import register


RECORD_EXECUTED_TEST_RUN_COMMAND_SCHEMA = (
    "switchboard.executed_test_run.record_command.v1"
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

MAX_COMMANDS = 50
MAX_COMMAND_LENGTH = 2000


class RecordExecutedTestRunCommand(VersionedModel):
    """One executed-and-passing test run, recorded against the bound Work Session.

    ``completed_at`` is deliberately not a field — the server stamps it. The only
    hash key is ``output_sha256``; the server maps it onto the gate's accepted set.
    ``head_sha``/``branch`` are optional cross-checks: when given they must match
    the Work Session, and the persisted record is always pinned to the session's
    own values.
    """

    SCHEMA: ClassVar[str] = RECORD_EXECUTED_TEST_RUN_COMMAND_SCHEMA
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    schema_id: str = Field(
        default=RECORD_EXECUTED_TEST_RUN_COMMAND_SCHEMA, alias="schema")
    task_id: str = Field(description="Board task this evidence belongs to.")
    work_session_id: str = Field(
        description="The BOUND Work Session; a forked session cannot satisfy the gate.")
    commands: tuple[str, ...] = Field(
        description="The exact command(s) that ran, in order. At least one.")
    passed: bool | None = Field(
        default=None, description="True only if every command passed.")
    exit_code: int | None = Field(
        default=None, description="Process exit code of the run (0 == pass).")
    output_sha256: str = Field(
        description="Lowercase hex sha256 over the combined run output/log.")
    branch: str = Field(
        default="", description="Optional cross-check; must match the session branch.")
    head_sha: str = Field(
        default="", description="Optional cross-check; must match the session head_sha.")

    @field_validator("task_id", "work_session_id", "branch", "head_sha", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("output_sha256", mode="before")
    @classmethod
    def _normalize_hash(cls, value: Any) -> str:
        return str(value or "").strip().lower()

    @field_validator("commands", mode="before")
    @classmethod
    def _normalize_commands(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = (value,)
        try:
            return tuple(str(item or "").strip() for item in value)
        except TypeError:
            return (str(value).strip(),)

    @model_validator(mode="after")
    def _validate_command(self) -> "RecordExecutedTestRunCommand":
        if not self.task_id:
            raise ValueError("task_id is required")
        if not self.work_session_id:
            raise ValueError("work_session_id is required")
        if not self.commands or any(not command for command in self.commands):
            raise ValueError("commands requires at least one non-empty command")
        if len(self.commands) > MAX_COMMANDS:
            raise ValueError(f"commands must stay under {MAX_COMMANDS} entries")
        if any(len(command) > MAX_COMMAND_LENGTH for command in self.commands):
            raise ValueError(
                f"commands entries must stay under {MAX_COMMAND_LENGTH} characters")
        if self.passed is None and self.exit_code is None:
            raise ValueError(
                "passed and/or exit_code is required — the run must record its result")
        if (self.passed is not None and self.exit_code is not None
                and self.passed != (self.exit_code == 0)):
            raise ValueError("passed contradicts exit_code")
        if not SHA256_RE.match(self.output_sha256):
            raise ValueError(
                "output_sha256 must be a 64-character lowercase hex sha256 over the run output")
        if self.head_sha and not GIT_SHA_RE.match(self.head_sha):
            raise ValueError("head_sha must be a 40-character lowercase git SHA")
        return self

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RecordExecutedTestRunCommand":
        return cls.model_validate(dict(value or {}))

    def to_repository_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "task_id": self.task_id,
            "work_session_id": self.work_session_id,
            "commands": list(self.commands),
            "output_sha256": self.output_sha256,
            "branch": self.branch,
            "head_sha": self.head_sha,
        }
        if self.passed is not None:
            data["passed"] = self.passed
        if self.exit_code is not None:
            data["exit_code"] = self.exit_code
        return data


register(RecordExecutedTestRunCommand)
