"""Explicit Maxwell compatibility — unreachable by customer-facing omission (SEG-4).

Internal cron/job paths that still target the historical Maxwell board MUST
import and call this adapter by name. Customer REST/UI/Ask Taikun ingress must
never land here via a missing ``project`` argument.
"""
from __future__ import annotations

from constants import DEFAULT_PROJECT
from switchboard.domain.projects.context import ProjectContext

ADAPTER_NAME = "legacy_maxwell_default"
SOURCE = f"adapter:{ADAPTER_NAME}"


def maxwell_project_id() -> str:
    """Return the Maxwell project id for explicitly opt-in internal callers."""
    return DEFAULT_PROJECT


def project_context(*, principal_id: str = "", label: str = "") -> ProjectContext:
    """Build a ProjectContext bound to Maxwell via the named adapter only."""
    return ProjectContext(
        project_id=DEFAULT_PROJECT,
        source=SOURCE,
        principal_id=principal_id,
        label=label or "Maxwell",
    )
