"""Provider-credential domain policy and envelope encryption."""

from .crypto import VaultCiphertext, VaultKeyUnavailable, decrypt_credential, encrypt_credential
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
    "decrypt_credential",
    "encrypt_credential",
    "normalize_concurrency_policy",
    "normalize_provider",
    "validate_auth_type",
]
