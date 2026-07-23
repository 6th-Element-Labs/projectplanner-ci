"""Repository stubs — SQL moves on-touch from store.py; Protocols define the seams."""
from switchboard.storage.repositories.access import (
    AccessStoreRepository,
    default_access_repository,
)
from switchboard.storage.repositories.attention import (
    AttentionRepository,
    AttentionStoreError,
    default_attention_repository,
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
from switchboard.storage.repositories.work_sessions import (
    StoreWorkSessionsRepository,
    default_work_sessions_repository,
)
from switchboard.storage.repositories.external_ci import (
    StoreExternalCiRepository,
    default_external_ci_repository,
)
from switchboard.storage.repositories.publication import (
    StorePublicationRepository,
    default_publication_repository,
)
from switchboard.storage.repositories.projects import (
    StoreProjectsRepository,
    default_projects_repository,
)
from switchboard.storage.repositories.kpis_economics import (
    StoreKpisEconomicsRepository,
    default_kpis_economics_repository,
)
from switchboard.storage.repositories.review_verdicts import (
    ReviewVerdictError,
    ReviewVerdictRepository,
    default_review_verdict_repository,
)
from switchboard.storage.repositories.review_remediations import (
    ReviewRemediationRepository,
    default_review_remediation_repository,
)

__all__ = [
    "AccessRepository",
    "AttentionRepository",
    "AttentionStoreError",
    "AccessStoreRepository",
    "ClaimsRepository",
    "StoreClaimsRepository",
    "StoreCoordinationRepository",
    "StoreDeliverablesRepository",
    "StoreExternalCiRepository",
    "StoreKpisEconomicsRepository",
    "StoreProjectsRepository",
    "StoreProvenanceRepository",
    "StorePublicationRepository",
    "StoreWorkSessionsRepository",
    "CredentialVaultError",
    "ProviderCredentialRepository",
    "ReviewVerdictError",
    "ReviewVerdictRepository",
    "ReviewRemediationRepository",
    "StoreTaskRepository",
    "TaskRepository",
    "default_access_repository",
    "default_attention_repository",
    "default_claims_repository",
    "default_coordination_repository",
    "default_deliverables_repository",
    "default_external_ci_repository",
    "default_kpis_economics_repository",
    "default_projects_repository",
    "default_provenance_repository",
    "default_provider_credential_repository",
    "default_review_verdict_repository",
    "default_review_remediation_repository",
    "default_publication_repository",
    "default_task_repository",
    "default_work_sessions_repository",
]
