"""Task contracts — re-export the current version."""
from .v1 import (
    CREATE_TASK_COMMAND_SCHEMA,
    GET_TASK_QUERY_SCHEMA,
    MOVE_TASK_COMMAND_SCHEMA,
    UPDATE_TASK_COMMAND_SCHEMA,
    UPDATE_TASK_FIELDS,
    CreateTaskCommand,
    GetTaskQuery,
    MoveTaskCommand,
    UpdateTaskCommand,
    coerce_is_blocking,
    normalize_depends_on_replacement,
)

__all__ = [
    "CREATE_TASK_COMMAND_SCHEMA",
    "GET_TASK_QUERY_SCHEMA",
    "MOVE_TASK_COMMAND_SCHEMA",
    "UPDATE_TASK_COMMAND_SCHEMA",
    "UPDATE_TASK_FIELDS",
    "CreateTaskCommand",
    "GetTaskQuery",
    "MoveTaskCommand",
    "UpdateTaskCommand",
    "coerce_is_blocking",
    "normalize_depends_on_replacement",
]
