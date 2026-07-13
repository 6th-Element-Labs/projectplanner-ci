"""Claim lifecycle Protocol — SQL lives behind implementations, not in application/."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ClaimsRepository(Protocol):
    """Project-scoped claim lifecycle surface used by application commands."""

    def claim_task(self, task_id: str, agent_id: str, **kwargs: Any) -> dict[str, Any]:
        """Atomically claim one exact ready, unblocked task."""

    def claim_next(self, agent_id: str, **kwargs: Any) -> dict[str, Any]:
        """Claim the highest-priority unblocked task for an agent."""

    def complete_claim(self, claim_id: str, **kwargs: Any) -> dict[str, Any]:
        """Release an active claim with completion evidence and move to In Review."""

    def abandon_claim(self, claim_id: str, reason: str, **kwargs: Any) -> dict[str, Any]:
        """Abandon an active claim without completion."""

    def revoke_claim(self, claim_id: str, reason: str, **kwargs: Any) -> dict[str, Any]:
        """Operator override: revoke a claim and optionally reassign."""
