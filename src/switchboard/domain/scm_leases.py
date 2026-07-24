"""Pure policy for the short-lived SCM lease broker (ENFORCE-13).

A repository operation belongs to exactly one phase. A read lease authorizes only
clone/fetch/read; a write lease authorizes only push/create_pr/merge. The broker
never lets a read lease perform a write (or vice versa) — the two phases are kept
separate so a clone credential can never be replayed to push.

This module holds no storage and no secrets: it maps operations to phases, models
the acquiring principal, and defines the trusted-runtime token-minting port. The
default minter is unconfigured on purpose — a real GitHub-App installation token is
materialized only inside the trusted runtime, never by the control plane at rest.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

# The operation vocabulary mirrors switchboard.scm_connection.v1 (ACCESS-28), split
# into the two phases ENFORCE-13 must keep apart.
READ_OPERATIONS = frozenset({"clone", "fetch", "read"})
WRITE_OPERATIONS = frozenset({"push", "create_pr", "merge"})
ALLOWED_OPERATIONS = READ_OPERATIONS | WRITE_OPERATIONS
PHASES = frozenset({"read", "write"})

# Scopes that let a service principal drive a repository-operation lease.
_CREDENTIAL_SCOPES = frozenset({"use:credentials", "admin"})
_SERVICE_KINDS = frozenset({"agent", "host", "system"})


class SCMLeaseError(ValueError):
    """Stable, secret-safe failure surfaced at application/transport boundaries."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "error_code": self.code, "message": self.message}


def phase_for_operation(operation: str) -> str:
    """Return the phase ('read'|'write') that owns ``operation`` or fail closed."""
    op = str(operation or "").strip().lower()
    if op in READ_OPERATIONS:
        return "read"
    if op in WRITE_OPERATIONS:
        return "write"
    raise SCMLeaseError(
        "invalid_scm_operation",
        "operation is not a recognized SCM repository operation",
    )


def normalize_operations(operations: Any) -> tuple[str, list[str]]:
    """Validate a requested operation set and return (phase, sorted-ops).

    Every operation must be known and must belong to the same phase; a lease is
    issued for one phase only. Empty or mixed-phase requests fail closed.
    """
    if isinstance(operations, str):
        items = [operations]
    elif isinstance(operations, (list, tuple, set, frozenset)):
        items = list(operations)
    else:
        raise SCMLeaseError("invalid_scm_operation", "operations must be a list of operations")
    ops = sorted({str(item or "").strip().lower() for item in items} - {""})
    if not ops:
        raise SCMLeaseError("scm_operations_required", "at least one operation is required")
    phases = {phase_for_operation(op) for op in ops}
    if len(phases) != 1:
        raise SCMLeaseError(
            "scm_operation_phase_conflict",
            "a lease may not mix read (clone/fetch) and write (push/PR/merge) operations",
        )
    return phases.pop(), ops


@dataclass(frozen=True)
class SCMLeasePrincipal:
    """The identity acquiring or releasing a lease (never carries a secret)."""

    principal_id: str
    principal_kind: str
    scopes: tuple[str, ...] = ()
    admin: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "SCMLeasePrincipal":
        data = dict(value or {})
        scopes = tuple(sorted({str(item) for item in (data.get("scopes") or [])}))
        return cls(
            principal_id=str(data.get("principal_id") or data.get("id") or "").strip(),
            principal_kind=str(data.get("principal_kind") or data.get("kind") or "").strip().lower(),
            scopes=scopes,
            admin=bool(data.get("admin")) or "admin" in scopes,
        )

    def can_use_credentials(self) -> bool:
        if not self.principal_id:
            return False
        if self.admin:
            return True
        return (
            self.principal_kind in _SERVICE_KINDS
            and bool(_CREDENTIAL_SCOPES & set(self.scopes))
        )


@dataclass(frozen=True)
class MintedSCMToken:
    """A short-lived repository token returned only across the trusted bridge."""

    token: str
    expires_at: float
    token_type: str = "installation"


class SCMTokenMinter(Protocol):
    """Trusted-runtime port that exchanges an opaque installation ref for a token.

    Implementations run only inside the trusted runtime and perform the GitHub-App
    JWT → installation-access-token exchange. The control plane calls this exactly
    once per materialized lease and never persists the returned token.
    """

    def mint(self, *, installation_ref: str, repository: str, phase: str,
             operations: tuple[str, ...], ttl_seconds: int) -> MintedSCMToken:
        ...


class UnconfiguredSCMTokenMinter:
    """Default minter: refuses to mint until a trusted-runtime minter is installed."""

    def mint(self, *, installation_ref: str, repository: str, phase: str,
             operations: tuple[str, ...], ttl_seconds: int) -> MintedSCMToken:
        raise SCMLeaseError(
            "scm_minter_unavailable",
            "no trusted-runtime SCM token minter is configured",
            status_code=503,
        )


__all__ = [
    "ALLOWED_OPERATIONS",
    "MintedSCMToken",
    "PHASES",
    "READ_OPERATIONS",
    "SCMLeaseError",
    "SCMLeasePrincipal",
    "SCMTokenMinter",
    "UnconfiguredSCMTokenMinter",
    "WRITE_OPERATIONS",
    "normalize_operations",
    "phase_for_operation",
]
