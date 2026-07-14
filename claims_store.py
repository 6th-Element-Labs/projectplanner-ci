""" Backward-compatible shim — prefer ``switchboard.storage.repositories.claims``."""
import scripts.switchboard_path  # noqa: F401 — make src/switchboard importable

from switchboard.storage.repositories.claims import (  # noqa: E402
    StoreClaimsRepository,
    abandon_claim,
    check_files,
    check_resources,
    claim_binding_target,
    claim_files,
    claim_next,
    claim_resources,
    claim_task,
    complete_claim,
    default_claims_repository,
    list_active_leases,
    list_active_resource_leases,
    release_files,
    release_resource_lease,
    revoke_claim,
)

__all__ = [
    "StoreClaimsRepository",
    "default_claims_repository",
    "claim_binding_target",
    "claim_task",
    "claim_next",
    "complete_claim",
    "abandon_claim",
    "revoke_claim",
    "claim_files",
    "release_files",
    "check_files",
    "list_active_leases",
    "claim_resources",
    "check_resources",
    "release_resource_lease",
    "list_active_resource_leases",
]
