"""Versioned Pydantic contracts shared by REST, MCP, and events (ARCH-MS-25)."""
from . import registry, tasks
from .base import (
    SCHEMA_ID_PREFIX,
    VersionedModel,
    normalize_dependency_ids,
    validation_error_message,
)
from .registry import get_schema, list_schemas, register
from .tasks import (
    CREATE_TASK_COMMAND_SCHEMA,
    GET_TASK_QUERY_SCHEMA,
    UPDATE_TASK_COMMAND_SCHEMA,
    CreateTaskCommand,
    GetTaskQuery,
    UpdateTaskCommand,
)

__all__ = [
    "CREATE_TASK_COMMAND_SCHEMA",
    "GET_TASK_QUERY_SCHEMA",
    "SCHEMA_ID_PREFIX",
    "UPDATE_TASK_COMMAND_SCHEMA",
    "CreateTaskCommand",
    "GetTaskQuery",
    "UpdateTaskCommand",
    "VersionedModel",
    "get_schema",
    "list_schemas",
    "normalize_dependency_ids",
    "register",
    "registry",
    "tasks",
    "validation_error_message",
]
