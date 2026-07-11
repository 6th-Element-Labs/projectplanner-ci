"""Update-task application command.

``store.py`` remains the green persistence facade during Phase 0.  REST and MCP
adapters both call :func:`execute_mapping_result`; transport-specific
authentication, write-binding, and response serialization stay at their edges.
Consolidating here gives both surfaces the same fail-closed dependency check
that previously lived only in the MCP tool.
"""
from __future__ import annotations

from typing import Any

import store

from ..contracts.tasks import UpdateTaskCommand


class UpdateTaskError(ValueError):
    """A command validation failure that adapters can render for their transport."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        # Keep MCP's historic human-readable `error` field while adding a stable code.
        return {"error": self.message, "error_code": self.code,
                "message": self.message, **self.details}


def execute(command: UpdateTaskCommand, *, actor: str, project: str) -> Any:
    """Validate and apply one sparse task update through the store facade.

    Returns exactly what ``store.update_task`` returns: the refreshed task dict
    on success, ``None`` when the task does not exist, or a store error dict
    (e.g. ``done_requires_merge_provenance``).  Raises :class:`UpdateTaskError`
    for command-level validation failures the store never sees.
    """
    if command.depends_on:  # a non-empty replacement edge list; clearing needs no check
        unknown = [task_id for task_id in command.depends_on
                   if not store.get_task(task_id, project=project)]
        if unknown:
            joined = ", ".join(unknown)
            raise UpdateTaskError(
                "unknown_dependencies",
                f"unknown dependency id(s) on project '{project}': {joined} — task NOT updated. "
                "Create them first or fix the id.",
                project=project,
                dependency_ids=unknown,
            )
    return store.update_task(command.task_id, command.to_store_fields(),
                             actor=actor, project=project)


def execute_mapping_result(task_id: str, data: dict[str, Any], *, actor: str,
                           project: str) -> Any:
    """Execute adapter input and return the store result or a structured error."""
    try:
        return execute(UpdateTaskCommand.from_mapping(task_id, data),
                       actor=actor, project=project)
    except UpdateTaskError as exc:
        return exc.as_dict()
