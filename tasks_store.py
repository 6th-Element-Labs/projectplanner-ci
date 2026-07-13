""" Backward-compatible shim — prefer ``switchboard.storage.repositories.tasks``."""
import scripts.switchboard_path  # noqa: F401 — make src/switchboard importable

from switchboard.storage.repositories.tasks import (  # noqa: E402
    EDITABLE,
    StoreTaskRepository,
    archive_task,
    board_payload,
    board_rollups,
    create_task,
    default_task_repository,
    delete_task,
    get_archived_task,
    get_task,
    list_tasks,
    list_tasks_for_board,
    list_tasks_slim,
    move_task,
    project_tally,
    project_task_stamp,
    task_tally,
    update_task,
)

__all__ = [
    "EDITABLE",
    "StoreTaskRepository",
    "default_task_repository",
    "list_tasks",
    "list_tasks_slim",
    "list_tasks_for_board",
    "board_rollups",
    "get_task",
    "update_task",
    "create_task",
    "task_tally",
    "project_tally",
    "delete_task",
    "get_archived_task",
    "archive_task",
    "move_task",
    "project_task_stamp",
    "board_payload",
]
