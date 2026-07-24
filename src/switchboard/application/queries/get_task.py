"""Get-task application query.

REST and MCP both resolve one project-scoped task through :func:`execute`; each
adapter keeps its own response shape. Application depends on
:class:`TaskRepository`; ``store.task_repository`` is the Phase-1A implementation.
"""
from __future__ import annotations

from typing import Any, Optional

import store

from switchboard.application.queries import task_session as task_session_query
from switchboard.application.queries import completion_projection
from switchboard.storage.repositories.protocols import TaskRepository

from ..contracts.tasks import GetTaskQuery


def execute(
        query: GetTaskQuery,
        *,
        tasks: Optional[TaskRepository] = None) -> Optional[dict[str, Any]]:
    """Return the full task detail for one task id, or ``None`` when absent."""
    repo = tasks or store.task_repository
    task = repo.get_task(query.task_id, project=query.project)
    completion_projection.attach_completion_projection(task, project=query.project)
    # SIMPLIFY-3: attach the TaskSession projection so modal/graph consumers
    # never invent a second answer to "what is running?".
    return task_session_query.attach_honest_display(task, project=query.project)


def execute_for(
        task_id: str,
        *,
        project: str,
        tasks: Optional[TaskRepository] = None) -> Optional[dict[str, Any]]:
    """Convenience adapter entrypoint mirroring the command modules."""
    return execute(GetTaskQuery.from_inputs(task_id, project=project), tasks=tasks)
