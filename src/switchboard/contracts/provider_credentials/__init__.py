"""Provider-credential vault contracts."""

from .v1 import (
    ACQUIRE_PROVIDER_CREDENTIAL_LEASE_SCHEMA,
    DELETE_PROVIDER_CONNECTION_SCHEMA,
    ENROLL_PROVIDER_CONNECTION_SCHEMA,
    RELEASE_PROVIDER_CREDENTIAL_LEASE_SCHEMA,
    REVOKE_PROVIDER_CONNECTION_SCHEMA,
    ROTATE_PROVIDER_CONNECTION_SCHEMA,
    AcquireProviderCredentialLeaseCommand,
    DeleteProviderConnectionCommand,
    EnrollProviderConnectionCommand,
    ReleaseProviderCredentialLeaseCommand,
    RevokeProviderConnectionCommand,
    RotateProviderConnectionCommand,
)

__all__ = [
    "ACQUIRE_PROVIDER_CREDENTIAL_LEASE_SCHEMA",
    "DELETE_PROVIDER_CONNECTION_SCHEMA",
    "ENROLL_PROVIDER_CONNECTION_SCHEMA",
    "RELEASE_PROVIDER_CREDENTIAL_LEASE_SCHEMA",
    "REVOKE_PROVIDER_CONNECTION_SCHEMA",
    "ROTATE_PROVIDER_CONNECTION_SCHEMA",
    "AcquireProviderCredentialLeaseCommand",
    "DeleteProviderConnectionCommand",
    "EnrollProviderConnectionCommand",
    "ReleaseProviderCredentialLeaseCommand",
    "RevokeProviderConnectionCommand",
    "RotateProviderConnectionCommand",
]
