"""Backward-compatible shim — prefer ``switchboard.storage.repositories.claims``."""
import scripts.switchboard_path  # noqa: F401 — make src/switchboard importable

from switchboard.storage.repositories.claims import (  # noqa: E402
    StoreClaimsRepository,
    abandon_claim,
    claim_binding_target,
    claim_next,
    claim_task,
    complete_claim,
    default_claims_repository,
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
]
