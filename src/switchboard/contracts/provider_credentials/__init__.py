"""Provider-credential vault contracts."""

from .v1 import (
    ACQUIRE_PROVIDER_CREDENTIAL_LEASE_SCHEMA,
    BIND_HOST_NATIVE_CONNECTION_SCHEMA,
    DELETE_PROVIDER_CONNECTION_SCHEMA,
    ENROLL_PROVIDER_CONNECTION_SCHEMA,
    RELEASE_PROVIDER_CREDENTIAL_LEASE_SCHEMA,
    REVOKE_PROVIDER_CONNECTION_SCHEMA,
    ROTATE_PROVIDER_CONNECTION_SCHEMA,
    VERIFY_PROVIDER_CONNECTION_SCHEMA,
    AcquireProviderCredentialLeaseCommand,
    BindHostNativeConnectionCommand,
    DeleteProviderConnectionCommand,
    EnrollProviderConnectionCommand,
    ReleaseProviderCredentialLeaseCommand,
    RevokeProviderConnectionCommand,
    RotateProviderConnectionCommand,
    VerifyProviderConnectionCommand,
)

__all__ = [
    "ACQUIRE_PROVIDER_CREDENTIAL_LEASE_SCHEMA",
    "BIND_HOST_NATIVE_CONNECTION_SCHEMA",
    "DELETE_PROVIDER_CONNECTION_SCHEMA",
    "ENROLL_PROVIDER_CONNECTION_SCHEMA",
    "RELEASE_PROVIDER_CREDENTIAL_LEASE_SCHEMA",
    "REVOKE_PROVIDER_CONNECTION_SCHEMA",
    "ROTATE_PROVIDER_CONNECTION_SCHEMA",
    "VERIFY_PROVIDER_CONNECTION_SCHEMA",
    "AcquireProviderCredentialLeaseCommand",
    "BindHostNativeConnectionCommand",
    "DeleteProviderConnectionCommand",
    "EnrollProviderConnectionCommand",
    "ReleaseProviderCredentialLeaseCommand",
    "RevokeProviderConnectionCommand",
    "RotateProviderConnectionCommand",
    "VerifyProviderConnectionCommand",
]
