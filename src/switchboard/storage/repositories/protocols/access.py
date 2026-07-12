"""Project/access persistence Protocol — registry SQL stays behind implementations."""
from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class AccessRepository(Protocol):
    """Project registry and access lookups used by application services."""

    def normalize_project_id(self, value: str) -> str:
        """Turn a human project name into a stable project id."""

    def has_project(self, project: Optional[str]) -> bool:
        """Return True when ``project`` is a known board id."""

    def projects(self) -> list[dict[str, Any]]:
        """Return the switcher's project list ``[{id, label, ...}]``."""

    def project_access(self, project: str) -> dict[str, Any]:
        """Return access metadata for one project (purpose, boundary, owners)."""
