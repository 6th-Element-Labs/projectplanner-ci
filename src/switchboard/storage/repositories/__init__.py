"""Repository stubs — SQL moves on-touch from store.py; Protocols define the seams."""
from switchboard.storage.repositories.access import (
    AccessStoreRepository,
    default_access_repository,
)
from switchboard.storage.repositories.claims import (
    StoreClaimsRepository,
    default_claims_repository,
)
from switchboard.storage.repositories.protocols import (
    AccessRepository,
    ClaimsRepository,
    TaskRepository,
)
from switchboard.storage.repositories.tasks import (
    StoreTaskRepository,
    default_task_repository,
)

__all__ = [
    "AccessRepository",
    "AccessStoreRepository",
    "ClaimsRepository",
    "StoreClaimsRepository",
    "StoreTaskRepository",
    "TaskRepository",
    "default_access_repository",
    "default_claims_repository",
    "default_task_repository",
]
