"""Provider-credential domain policy and envelope encryption."""

from .crypto import VaultCiphertext, VaultKeyUnavailable, decrypt_credential, encrypt_credential
from .capabilities import (
    PROVIDER_AUTH_POLICY_VERSION,
    auth_host_classes_for_host,
    concurrency_policy_for_capability,
    host_class_satisfies,
    list_provider_auth_capabilities,
    provider_auth_decision,
)
from .policy import (
    CredentialPrincipal,
    CredentialPolicyError,
    normalize_concurrency_policy,
    normalize_provider,
    validate_auth_type,
)

__all__ = [
    "CredentialPolicyError",
    "CredentialPrincipal",
    "VaultCiphertext",
    "VaultKeyUnavailable",
    "auth_host_classes_for_host",
    "concurrency_policy_for_capability",
    "decrypt_credential",
    "encrypt_credential",
    "host_class_satisfies",
    "normalize_concurrency_policy",
    "normalize_provider",
    "PROVIDER_AUTH_POLICY_VERSION",
    "list_provider_auth_capabilities",
    "provider_auth_decision",
    "validate_auth_type",
]
