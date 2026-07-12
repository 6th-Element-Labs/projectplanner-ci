"""Task persistence Protocol — SQL lives behind implementations, not in application/."""
from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class TaskRepository(Protocol):
    """Project-scoped task read/write surface used by application commands/queries."""

    def get_task(self, task_id: str, project: str = ...) -> Optional[dict[str, Any]]:
        """Return full task detail, or ``None`` when the id is absent."""

    def create_task(
            self,
            data: dict[str, Any],
            actor: str = ...,
            project: str = ...) -> Optional[dict[str, Any]]:
        """Persist a new task and return the hydrated detail row."""

    def update_task(
            self,
            task_id: str,
            fields: dict[str, Any],
            actor: str = ...,
            project: str = ...) -> Optional[dict[str, Any]]:
        """Apply a sparse field update; return the refreshed task or an error dict."""
