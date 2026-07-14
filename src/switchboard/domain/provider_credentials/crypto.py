"""Fail-closed AES-256-GCM envelope encryption for provider auth capsules."""
from __future__ import annotations

import base64
import binascii
import os
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


KEY_ENV = "PM_PROVIDER_VAULT_KEY"
KEY_ID_ENV = "PM_PROVIDER_VAULT_KEY_ID"


class VaultKeyUnavailable(RuntimeError):
    """The vault master key is absent or malformed; no fallback is permitted."""


class VaultDecryptionError(RuntimeError):
    """Stored ciphertext or its identity binding failed authentication."""


@dataclass(frozen=True)
class VaultCiphertext:
    ciphertext: bytes
    nonce: bytes
    key_id: str


def _decode_key(raw: str) -> bytes:
    value = str(raw or "").strip()
    if not value:
        raise VaultKeyUnavailable("provider vault key unavailable")
    try:
        padded = value + "=" * (-len(value) % 4)
        key = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
        raise VaultKeyUnavailable("provider vault key invalid") from exc
    if len(key) != 32:
        raise VaultKeyUnavailable("provider vault key invalid")
    return key


def _key() -> tuple[bytes, str]:
    key = _decode_key(os.environ.get(KEY_ENV, ""))
    key_id = str(os.environ.get(KEY_ID_ENV, "env:v1") or "env:v1").strip()
    return key, key_id


def encrypt_credential(secret: str, *, associated_data: bytes) -> VaultCiphertext:
    value = str(secret or "")
    if not value:
        raise ValueError("credential is required")
    key, key_id = _key()
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, value.encode("utf-8"), associated_data)
    return VaultCiphertext(ciphertext=ciphertext, nonce=nonce, key_id=key_id)


def decrypt_credential(ciphertext: bytes, nonce: bytes, *, key_id: str,
                       associated_data: bytes) -> str:
    key, active_key_id = _key()
    if not key_id or key_id != active_key_id:
        raise VaultKeyUnavailable("provider vault key version unavailable")
    try:
        cleartext = AESGCM(key).decrypt(bytes(nonce), bytes(ciphertext), associated_data)
        return cleartext.decode("utf-8")
    except (InvalidTag, UnicodeDecodeError, TypeError, ValueError) as exc:
        raise VaultDecryptionError("provider credential authentication failed") from exc
