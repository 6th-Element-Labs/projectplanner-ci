"""Task repository adapter — store.py remains the green SQL facade for Phase 1A.

Application commands depend on :class:`TaskRepository`. This adapter implements
that Protocol by delegating to ``store`` function entrypoints so SQL stays out of
``application/`` while the strangler extraction (ARCH-MS-31+) relocates tables.
"""
from __future__ import annotations

from typing import Any, Optional

from constants import DEFAULT_PROJECT


class StoreTaskRepository:
    """``store.py``-backed :class:`~switchboard.storage.repositories.protocols.TaskRepository`."""

    def get_task(self, task_id: str, project: str = DEFAULT_PROJECT) -> Optional[dict[str, Any]]:
        import store
        return store.get_task(task_id, project=project)

    def create_task(
            self,
            data: dict[str, Any],
            actor: str = "user",
            project: str = DEFAULT_PROJECT) -> Optional[dict[str, Any]]:
        import store
        return store.create_task(data, actor=actor, project=project)

    def update_task(
            self,
            task_id: str,
            fields: dict[str, Any],
            actor: str = "user",
            project: str = DEFAULT_PROJECT) -> Optional[dict[str, Any]]:
        import store
        return store.update_task(task_id, fields, actor=actor, project=project)


def default_task_repository() -> StoreTaskRepository:
    """Canonical Phase-1A task repository (store facade)."""
    return StoreTaskRepository()
