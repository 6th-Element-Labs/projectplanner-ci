"""Project-scoped repo constitution — layout truth distinct from repo_topology.

``repo_topology`` names Git remotes and Done/CI authority.
``repo_constitution`` names where product code, tests, docs, and agent front
doors live inside a checkout, and how growth/shims are policed.
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import ConfigDict, Field, field_validator

from ..base import VersionedModel
from ..registry import register


REPO_CONSTITUTION_SCHEMA = "switchboard.repo_constitution.v1"

ShimPolicy = Literal["timed", "none"]
EnforcementMode = Literal["off", "warn", "enforce"]


class RepoConstitution(VersionedModel):
    """Frozen layout contract for one project's canonical checkout shape."""

    SCHEMA: ClassVar[str] = REPO_CONSTITUTION_SCHEMA
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    schema_id: str = Field(default=REPO_CONSTITUTION_SCHEMA, alias="schema")
    profile_id: str = Field(
        description="Named layout profile, e.g. python_modular_monolith.")
    project_id: str = Field(
        description="Switchboard project this constitution binds to.")
    product_root: str = Field(
        description="Root directory for product code (e.g. src/switchboard/).")
    test_root: str = Field(
        description="Root directory for tests (e.g. tests/).")
    docs_front_door: str = Field(
        description="Human docs entry path (e.g. docs/INDEX.md).")
    agent_front_door: str = Field(
        description="Agent operating-entry path (e.g. AGENTS.md).")
    entrypoints: list[str] = Field(
        default_factory=list,
        description="Declared process/module entrypoints relative to repo root.")
    forbid_new: list[str] = Field(
        default_factory=list,
        description="Globs/paths where new files are forbidden (e.g. root *.py).")
    shim_policy: ShimPolicy = Field(
        description="Root shim policy: timed (sunset required) or none.")
    required_files: list[str] = Field(
        default_factory=list,
        description="Paths that must exist for the profile to be considered present.")
    archive_roots: list[str] = Field(
        default_factory=list,
        description="Roots reserved for archived/retired code.")
    enforcement_mode: EnforcementMode = Field(
        description="off | warn | enforce — how constitution drift is gated.")
    notes: str = Field(
        default="",
        description="Optional human notes; not used for enforcement.")

    @field_validator(
        "profile_id", "project_id", "product_root", "test_root",
        "docs_front_door", "agent_front_door", mode="before",
    )
    @classmethod
    def _required_text(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("value is required")
        return text

    @field_validator(
        "entrypoints", "forbid_new", "required_files", "archive_roots",
        mode="before",
    )
    @classmethod
    def _string_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("must be a list of strings")
        out: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text:
                out.append(text)
        return out


register(RepoConstitution)
