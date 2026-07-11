"""Get-task application query.

REST and MCP both resolve one project-scoped task through :func:`execute`; each
adapter keeps its own response shape (raw JSON + 404 for REST, task-brief text
for MCP).  ``store.get_task`` stays the green persistence facade during Phase 0.
"""
from __future__ import annotations

from typing import Any, Optional

import store

from ..contracts.tasks import GetTaskQuery


def execute(query: GetTaskQuery) -> Optional[dict[str, Any]]:
    """Return the full task detail for one task id, or ``None`` when absent."""
    return store.get_task(query.task_id, project=query.project)


def execute_for(task_id: str, *, project: str) -> Optional[dict[str, Any]]:
    """Convenience adapter entrypoint mirroring the command modules."""
    return execute(GetTaskQuery.from_inputs(task_id, project=project))
