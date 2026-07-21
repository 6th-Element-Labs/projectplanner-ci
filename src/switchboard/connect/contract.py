"""Wire contract for Switchboard Connect.

The exchange intentionally mirrors DHCP:

``Discover -> Offer -> Request -> Ack``

An Ack is a time-bounded assignment lease.  Once the provider process starts,
Connect participates only in capacity accounting, heartbeat, expiry, and kill.
The assignment's ``work_ref`` is opaque; Connect never interprets it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import re
from typing import Any


SCHEMA_PREFIX = "switchboard.connect"
DISCOVER_SCHEMA = f"{SCHEMA_PREFIX}.discover.v1"
OFFER_SCHEMA = f"{SCHEMA_PREFIX}.offer.v1"
REQUEST_SCHEMA = f"{SCHEMA_PREFIX}.request.v1"
ACK_SCHEMA = f"{SCHEMA_PREFIX}.ack.v1"

_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,239}$")


def _required_ref(name: str, value: str) -> str:
    normalized = str(value or "").strip()
    if not _REF.fullmatch(normalized):
        raise ValueError(f"invalid_{name}")
    return normalized


def _positive(name: str, value: int | float) -> int | float:
    if value <= 0:
        raise ValueError(f"{name}_must_be_positive")
    return value


class LeaseState(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    KILLED = "killed"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Hard limits that Connect may account for without understanding work."""

    max_runtime_seconds: int
    spend_limit_microunits: int = 0
    memory_limit_bytes: int = 0

    def __post_init__(self) -> None:
        _positive("max_runtime_seconds", self.max_runtime_seconds)
        if self.spend_limit_microunits < 0:
            raise ValueError("spend_limit_microunits_must_be_non_negative")
        if self.memory_limit_bytes < 0:
            raise ValueError("memory_limit_bytes_must_be_non_negative")


@dataclass(frozen=True, slots=True)
class Assignment:
    """Opaque unit waiting for a compatible host."""

    assignment_id: str
    principal_ref: str
    work_ref: str
    runtime: str
    provider: str
    workspace_ref: str
    limits: ResourceLimits
    queued_at: float

    def __post_init__(self) -> None:
        for name in (
            "assignment_id", "principal_ref", "work_ref", "runtime", "provider",
            "workspace_ref",
        ):
            _required_ref(name, getattr(self, name))
        _positive("queued_at", self.queued_at)


@dataclass(frozen=True, slots=True)
class RuntimeCapability:
    """One provider runtime that a host can actually launch."""

    runtime: str
    provider: str

    def __post_init__(self) -> None:
        _required_ref("runtime", self.runtime)
        _required_ref("provider", self.provider)


@dataclass(frozen=True, slots=True)
class Discover:
    """Host capability and free-capacity advertisement.

    ``available_slots`` is current free headroom. Active processes are already
    excluded by the host; outstanding Offers consume slots from this Discover.
    """

    host_id: str
    nonce: str
    capabilities: tuple[RuntimeCapability, ...]
    available_slots: int
    observed_at: float
    schema: str = field(default=DISCOVER_SCHEMA, init=False)

    def __post_init__(self) -> None:
        _required_ref("host_id", self.host_id)
        _required_ref("nonce", self.nonce)
        if not self.capabilities:
            raise ValueError("capabilities_required")
        _positive("available_slots", self.available_slots)
        _positive("observed_at", self.observed_at)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Offer:
    """Short reservation of one assignment for one host discovery."""

    offer_id: str
    assignment: Assignment
    host_id: str
    nonce: str
    offered_at: float
    expires_at: float
    schema: str = field(default=OFFER_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for name in ("offer_id", "host_id", "nonce"):
            _required_ref(name, getattr(self, name))
        _positive("offered_at", self.offered_at)
        if self.expires_at <= self.offered_at:
            raise ValueError("offer_expiry_must_follow_offer")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Request:
    """A host accepting the exact offer it received."""

    offer_id: str
    host_id: str
    nonce: str
    requested_at: float
    schema: str = field(default=REQUEST_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for name in ("offer_id", "host_id", "nonce"):
            _required_ref(name, getattr(self, name))
        _positive("requested_at", self.requested_at)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Ack:
    """Active or terminal assignment lease returned to the host."""

    lease_id: str
    runner_id: str
    assignment: Assignment
    host_id: str
    issued_at: float
    expires_at: float
    heartbeat_interval_seconds: int
    last_heartbeat_at: float
    state: LeaseState = LeaseState.ACTIVE
    terminal_reason: str = ""
    schema: str = field(default=ACK_SCHEMA, init=False)

    def __post_init__(self) -> None:
        for name in ("lease_id", "runner_id", "host_id"):
            _required_ref(name, getattr(self, name))
        _positive("issued_at", self.issued_at)
        _positive("last_heartbeat_at", self.last_heartbeat_at)
        _positive("heartbeat_interval_seconds", self.heartbeat_interval_seconds)
        if self.expires_at <= self.issued_at:
            raise ValueError("lease_expiry_must_follow_issue")

    @property
    def active(self) -> bool:
        return self.state is LeaseState.ACTIVE

    def to_dict(self) -> dict[str, Any]:
        body = asdict(self)
        body["state"] = self.state.value
        return body
