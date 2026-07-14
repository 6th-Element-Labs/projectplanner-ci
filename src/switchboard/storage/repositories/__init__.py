"""Repository stubs — SQL moves on-touch from store.py; Protocols define the seams."""
from switchboard.storage.repositories.access import (
    AccessStoreRepository,
    default_access_repository,
)
from switchboard.storage.repositories.claims import (
    StoreClaimsRepository,
    default_claims_repository,
)
from switchboard.storage.repositories.coordination import (
    StoreCoordinationRepository,
    default_coordination_repository,
)
from switchboard.storage.repositories.deliverables import (
    StoreDeliverablesRepository,
    default_deliverables_repository,
)
from switchboard.storage.repositories.provenance import (
    StoreProvenanceRepository,
    default_provenance_repository,
)
from switchboard.storage.repositories.provider_credentials import (
    CredentialVaultError,
    ProviderCredentialRepository,
    default_provider_credential_repository,
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
    "StoreCoordinationRepository",
    "StoreDeliverablesRepository",
    "StoreProvenanceRepository",
    "CredentialVaultError",
    "ProviderCredentialRepository",
    "StoreTaskRepository",
    "TaskRepository",
    "default_access_repository",
    "default_claims_repository",
    "default_coordination_repository",
    "default_deliverables_repository",
    "default_provenance_repository",
    "default_provider_credential_repository",
    "default_task_repository",
]
