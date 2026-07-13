"""Project/access persistence Protocol — registry SQL stays behind implementations."""
from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from switchboard.contracts.projects.v2 import ProjectUpdateCommand


@runtime_checkable
class AccessRepository(Protocol):
    """Project registry and access lookups used by application services."""

    def normalize_project_id(self, value: str) -> str:
        """Turn a human project name into a stable project id."""

    def has_project(self, project: Optional[str]) -> bool:
        """Return True when ``project`` is a known board id."""

    def projects(self) -> list[dict[str, Any]]:
        """Return the switcher's active project list ``[{id, label, ...}]``."""

    def project_access(self, project: str) -> dict[str, Any]:
        """Return access metadata for one project (purpose, boundary, owners)."""

    def get_project_record(self, project: str) -> dict[str, Any]:
        """Return the ``switchboard.project.v2`` projection for one project."""

    def list_registry_projects(self, *, include_archived: bool = True) -> list[dict[str, Any]]:
        """Return registry projections for known projects."""

    def update_project_metadata(self, command: Mapping[str, Any] | ProjectUpdateCommand,
                                actor: str = "system") -> dict[str, Any]:
        """Apply editable metadata and lifecycle transitions."""

    def transition_project_lifecycle(self, project_id: str, requested: str, *, actor: str,
                                     reason: str, impact_report_hash: str = "",
                                     validation: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        """Atomically transition one project and persist its audit event."""

    def list_project_lifecycle_events(self, project_id: str) -> list[dict[str, Any]]:
        """Return durable registry audit events for project lifecycle transitions."""
