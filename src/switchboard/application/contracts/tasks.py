""" Backward-compatible re-exports of versioned task contracts.

New code should import from ``switchboard.contracts`` directly.  The
``application.contracts`` path remains during the Phase 1 strangler so
existing commands/adapters keep working.
"""
from switchboard.contracts.tasks.v1 import (
    UPDATE_TASK_FIELDS,
    CreateTaskCommand,
    GetTaskQuery,
    MoveTaskCommand,
    UpdateTaskCommand,
    coerce_is_blocking,
    normalize_depends_on_replacement,
)
from switchboard.contracts.base import normalize_dependency_ids

__all__ = [
    "UPDATE_TASK_FIELDS",
    "CreateTaskCommand",
    "GetTaskQuery",
    "MoveTaskCommand",
    "UpdateTaskCommand",
    "coerce_is_blocking",
    "normalize_depends_on_replacement",
    "normalize_dependency_ids",
]
