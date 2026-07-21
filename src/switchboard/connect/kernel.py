"""Small, thread-safe reference kernel for the Connect state machine."""

from __future__ import annotations

from dataclasses import replace
import threading
import time
from typing import Callable
import uuid

from .contract import Ack, Assignment, Discover, LeaseState, Offer, Request


class ConnectRefused(RuntimeError):
    """Typed refusal at the Connect boundary."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ConnectKernel:
    """Reference state machine; durable adapters can preserve the same contract.

    The lock makes offer reservation and lease activation atomic in one process.
    A distributed adapter must provide the same compare-and-swap semantics.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        identifier: Callable[[], str] | None = None,
        offer_ttl_seconds: int = 30,
        heartbeat_interval_seconds: int = 30,
        terminal_retention_seconds: int = 3600,
    ) -> None:
        if min(offer_ttl_seconds, heartbeat_interval_seconds,
               terminal_retention_seconds) <= 0:
            raise ValueError("connect_intervals_must_be_positive")
        self._clock = clock
        self._identifier = identifier or (lambda: uuid.uuid4().hex)
        self._offer_ttl = offer_ttl_seconds
        self._heartbeat_interval = heartbeat_interval_seconds
        self._terminal_retention = terminal_retention_seconds
        self._lock = threading.RLock()
        self._pending: dict[str, Assignment] = {}
        self._offers: dict[str, Offer] = {}
        self._accepted: dict[str, tuple[str, str, str]] = {}
        self._leases: dict[str, Ack] = {}
        self._terminal_since: dict[str, float] = {}

    def enqueue(self, assignment: Assignment) -> None:
        with self._lock:
            if assignment.assignment_id in self._pending:
                raise ConnectRefused("assignment_exists")
            if any(
                lease.assignment.assignment_id == assignment.assignment_id
                and lease.active
                for lease in self._leases.values()
            ):
                raise ConnectRefused("assignment_active")
            self._pending[assignment.assignment_id] = assignment

    def discover(self, message: Discover) -> Offer | None:
        with self._lock:
            now = self._clock()
            self._expire_locked(now)
            retransmit = next((
                offer for offer in self._offers.values()
                if offer.host_id == message.host_id
                and offer.nonce == message.nonce
                and offer.expires_at > now
            ), None)
            if retransmit:
                return retransmit
            offered_count = sum(
                1 for offer in self._offers.values()
                if offer.host_id == message.host_id and offer.expires_at > now
            )
            if message.available_slots <= offered_count:
                return None
            reserved = {
                offer.assignment.assignment_id
                for offer in self._offers.values()
                if offer.expires_at > now
            }
            eligible = [
                item for item in self._pending.values()
                if item.assignment_id not in reserved
                and any(
                    capability.runtime == item.runtime
                    and capability.provider == item.provider
                    for capability in message.capabilities
                )
            ]
            if not eligible:
                return None
            assignment = min(eligible, key=lambda item: (item.queued_at, item.assignment_id))
            offer = Offer(
                offer_id=f"offer-{self._identifier()}",
                assignment=assignment,
                host_id=message.host_id,
                nonce=message.nonce,
                offered_at=now,
                expires_at=now + self._offer_ttl,
            )
            self._offers[offer.offer_id] = offer
            return offer

    def request(self, message: Request) -> Ack:
        with self._lock:
            now = self._clock()
            self._expire_locked(now, preserve_offer_id=message.offer_id)
            accepted = self._accepted.get(message.offer_id)
            if accepted:
                host_id, nonce, lease_id = accepted
                if host_id != message.host_id or nonce != message.nonce:
                    raise ConnectRefused("offer_binding_mismatch")
                lease = self._leases.get(lease_id)
                if lease:
                    return lease
                self._accepted.pop(message.offer_id, None)
            offer = self._offers.get(message.offer_id)
            if not offer:
                raise ConnectRefused("offer_not_found")
            if offer.host_id != message.host_id or offer.nonce != message.nonce:
                raise ConnectRefused("offer_binding_mismatch")
            if message.requested_at > offer.expires_at or now > offer.expires_at:
                self._offers.pop(offer.offer_id, None)
                raise ConnectRefused("offer_expired")
            assignment = self._pending.pop(offer.assignment.assignment_id, None)
            if not assignment:
                raise ConnectRefused("assignment_unavailable")
            self._offers.pop(offer.offer_id, None)
            issued_at = max(now, message.requested_at)
            ack = Ack(
                lease_id=f"connect-{self._identifier()}",
                runner_id=f"run-{self._identifier()}",
                assignment=assignment,
                host_id=message.host_id,
                issued_at=issued_at,
                expires_at=issued_at + assignment.limits.max_runtime_seconds,
                heartbeat_interval_seconds=self._heartbeat_interval,
                last_heartbeat_at=issued_at,
            )
            self._leases[ack.lease_id] = ack
            self._accepted[offer.offer_id] = (offer.host_id, offer.nonce, ack.lease_id)
            return ack

    def heartbeat(self, lease_id: str, runner_id: str, *, observed_at: float | None = None) -> Ack:
        with self._lock:
            now = self._clock() if observed_at is None else observed_at
            self._expire_locked(now)
            lease = self._leases.get(lease_id)
            if not lease:
                raise ConnectRefused("lease_not_found")
            if lease.runner_id != runner_id:
                raise ConnectRefused("runner_binding_mismatch")
            if not lease.active:
                raise ConnectRefused(f"lease_{lease.state.value}")
            refreshed = replace(lease, last_heartbeat_at=now)
            self._leases[lease_id] = refreshed
            return refreshed

    def release(self, lease_id: str, runner_id: str) -> Ack:
        return self._terminalize(lease_id, runner_id, LeaseState.RELEASED, "released")

    def kill(self, lease_id: str, *, reason: str) -> Ack:
        return self._terminalize(lease_id, "", LeaseState.KILLED, reason or "killed")

    def expire(self) -> tuple[Ack, ...]:
        with self._lock:
            return self._expire_locked(self._clock())

    def get(self, lease_id: str) -> Ack | None:
        with self._lock:
            return self._leases.get(lease_id)

    def active_count(self, host_id: str | None = None) -> int:
        with self._lock:
            return sum(
                1 for lease in self._leases.values()
                if lease.active and (host_id is None or lease.host_id == host_id)
            )

    def _terminalize(
        self,
        lease_id: str,
        runner_id: str,
        state: LeaseState,
        reason: str,
    ) -> Ack:
        with self._lock:
            now = self._clock()
            self._expire_locked(now)
            lease = self._leases.get(lease_id)
            if not lease:
                raise ConnectRefused("lease_not_found")
            if runner_id and lease.runner_id != runner_id:
                raise ConnectRefused("runner_binding_mismatch")
            if not lease.active:
                return lease
            terminal = replace(lease, state=state, terminal_reason=reason)
            self._leases[lease_id] = terminal
            self._terminal_since[lease_id] = now
            return terminal

    def _expire_locked(
        self,
        now: float,
        *,
        preserve_offer_id: str = "",
    ) -> tuple[Ack, ...]:
        expired: list[Ack] = []
        heartbeat_grace = self._heartbeat_interval * 2
        for lease_id, lease in tuple(self._leases.items()):
            if not lease.active:
                continue
            reason = ""
            if now >= lease.expires_at:
                reason = "lease_deadline"
            elif now - lease.last_heartbeat_at >= heartbeat_grace:
                reason = "heartbeat_timeout"
            if reason:
                terminal = replace(lease, state=LeaseState.EXPIRED, terminal_reason=reason)
                self._leases[lease_id] = terminal
                self._terminal_since[lease_id] = now
                expired.append(terminal)
        for offer_id, offer in tuple(self._offers.items()):
            if offer_id != preserve_offer_id and now >= offer.expires_at:
                self._offers.pop(offer_id, None)
        for lease_id, terminal_at in tuple(self._terminal_since.items()):
            if now - terminal_at < self._terminal_retention:
                continue
            self._leases.pop(lease_id, None)
            self._terminal_since.pop(lease_id, None)
            for offer_id, accepted in tuple(self._accepted.items()):
                if accepted[2] == lease_id:
                    self._accepted.pop(offer_id, None)
        return tuple(expired)
