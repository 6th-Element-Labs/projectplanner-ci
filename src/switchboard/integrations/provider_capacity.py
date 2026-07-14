"""Live-safe controller for subscription capacity observations and reset polling."""
from __future__ import annotations

from typing import Any, Callable, Mapping

from switchboard.storage.repositories.provider_capacity import (
    ProviderCapacityRepository,
    default_provider_capacity_repository,
)


PROVIDER_CAPACITY_RECEIPT_SCHEMA = "switchboard.provider_capacity.receipt.v1"


class SubscriptionCapacityController:
    """Authorize one lane, normalize its result, and durably pause/resume work."""

    def __init__(
        self,
        *,
        repository: ProviderCapacityRepository = default_provider_capacity_repository,
    ) -> None:
        self.repository = repository

    def run_lane(
        self,
        binding: Mapping[str, Any],
        *,
        task_policy: Mapping[str, Any] | None,
        lane_policy: Mapping[str, Any] | None,
        checkpoint: Mapping[str, Any] | None,
        personal_request: Callable[[], Mapping[str, Any]],
        metered_request: Callable[[], Mapping[str, Any]] | None = None,
        host_available: bool = True,
        actor: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        decision = self.repository.admission_decision(
            binding, task_policy=task_policy, lane_policy=lane_policy,
            host_available=host_available, now=now)
        base = {
            "schema": PROVIDER_CAPACITY_RECEIPT_SCHEMA,
            "provider": decision.get("provider"),
            "provider_account": decision.get("provider_account"),
            "lane_kind": decision.get("lane_kind"),
            "metered": bool(decision.get("metered")),
            "request_issued": False,
        }
        if not decision.get("allowed"):
            return {
                **base,
                "allowed": False,
                "status": "paused",
                "state": decision.get("state"),
                "reason_code": decision.get("reason_code"),
                "retry_after_seconds": decision.get("retry_after_seconds"),
                "reset_at": decision.get("reset_at"),
                "next_poll_at": decision.get("next_poll_at"),
            }

        request = metered_request if decision.get("metered") else personal_request
        if request is None:
            return {
                **base,
                "allowed": False,
                "status": "paused",
                "state": "policy_blocked",
                "reason_code": "authorized_lane_requester_missing",
            }
        try:
            response = request()
        except Exception:
            response = {
                "capacity_state": "provider_capacity_exhausted",
                "error_code": "provider_request_unavailable",
                "retry_after_seconds": 120,
            }
        observed = self.repository.observe(
            binding, response, checkpoint=checkpoint, actor=actor, now=now)
        account = observed["account"]
        return {
            **base,
            "allowed": True,
            "request_issued": True,
            "status": "completed" if account["state"] == "ready" else "paused",
            "state": account["state"],
            "reason_code": account["reason_code"],
            "retry_after_seconds": account.get("retry_after_seconds"),
            "reset_at": account.get("reset_at"),
            "next_poll_at": account.get("next_poll_at"),
            "checkpoint": observed.get("checkpoint"),
            "budget_ceiling": decision.get("budget_ceiling"),
            "cost_attribution": decision.get("cost_attribution"),
        }

    def poll(
        self,
        binding: Mapping[str, Any],
        *,
        idem_key: str,
        checkpoint: Mapping[str, Any] | None,
        probe: Callable[[], Mapping[str, Any]],
        actor: str,
        now: float | None = None,
        max_attempts: int = 8,
        window_seconds: int = 3600,
    ) -> dict[str, Any]:
        poll = self.repository.begin_poll(
            binding, idem_key=idem_key, actor=actor, now=now,
            max_attempts=max_attempts, window_seconds=window_seconds)
        if not poll.get("execute_probe"):
            return poll
        try:
            response = probe()
        except Exception:
            response = {
                "capacity_state": "provider_capacity_exhausted",
                "error_code": "provider_probe_unavailable",
                "retry_after_seconds": 120,
            }
        return self.repository.complete_poll(
            binding, poll_id=str(poll["poll_id"]), attempt=int(poll["attempt"]),
            response=response,
            checkpoint=checkpoint, actor=actor, now=now)


__all__ = ["PROVIDER_CAPACITY_RECEIPT_SCHEMA", "SubscriptionCapacityController"]
