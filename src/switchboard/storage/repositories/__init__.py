"""Repository stubs — SQL moves on-touch from store.py; Protocols define the seams."""
from switchboard.storage.repositories.access import (
    AccessStoreRepository,
    default_access_repository,
)
from switchboard.storage.repositories.protocols import AccessRepository, TaskRepository
from switchboard.storage.repositories.tasks import (
    StoreTaskRepository,
    default_task_repository,
)

__all__ = [
    "AccessRepository",
    "AccessStoreRepository",
    "StoreTaskRepository",
    "TaskRepository",
    "default_access_repository",
    "default_task_repository",
]
