"""Create-task application command.

Application depends on :class:`TaskRepository`. ``store.task_repository`` is the
Phase-1A implementation (SQL still behind the store facade until ARCH-MS-31+).
REST and MCP adapters both call :func:`execute`; transport-specific auth and
response serialization stay at their edges.
"""
from __future__ import annotations

from typing import Any, Optional

from switchboard.domain.validation_policy import classify_task

from pydantic import ValidationError

import store

from switchboard.contracts import validation_error_message
from switchboard.storage.repositories.protocols import TaskRepository

from ..contracts.tasks import CreateTaskCommand


class CreateTaskError(ValueError):
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


def execute(
        command: CreateTaskCommand,
        *,
        actor: str,
        project: str,
        tasks: Optional[TaskRepository] = None) -> dict[str, Any]:
    """Validate and create one project-scoped task through a TaskRepository."""
    repo = tasks or store.task_repository
    if not command.workstream_id or not command.title:
        raise CreateTaskError(
            "invalid_create_task",
            "workstream_id and title are required",
        )

    unknown = [
        task_id for task_id in command.depends_on
        if not repo.get_task(task_id, project=project)
    ]
    if unknown:
        joined = ", ".join(unknown)
        raise CreateTaskError(
            "unknown_dependencies",
            f"unknown dependency id(s) on project '{project}': {joined} — task NOT created. "
            "Create them first or fix the id.",
            project=project,
            dependency_ids=unknown,
        )

    store_data = command.to_store_data()
    validation = classify_task(store_data, project=project, material_rescope=True)
    if not validation.get("ok"):
        raise CreateTaskError(
            validation.get("error") or "ui_validation_policy_failed",
            validation.get("message") or "Task UI validation classification failed.",
            project=project,
        )
    store_data["ui_impact"] = validation.get("ui_impact")
    created = repo.create_task(store_data, actor=actor, project=project)
    if not created:
        raise CreateTaskError(
            "invalid_create_task",
            "workstream_id and title are required",
        )
    return created


def execute_mapping_result(data: dict[str, Any], *, actor: str,
                           project: str,
                           tasks: Optional[TaskRepository] = None) -> dict[str, Any]:
    """Execute adapter input and return either a task or a structured command error."""
    try:
        return execute(CreateTaskCommand.from_mapping(data), actor=actor,
                       project=project, tasks=tasks)
    except CreateTaskError as exc:
        return exc.as_dict()
    except ValidationError as exc:
        # Contract rejections (missing/mistyped fields) are caller errors —
        # same structured shape as CreateTaskError, never a transport 500.
        return CreateTaskError(
            "invalid_create_task", validation_error_message(exc)).as_dict()
