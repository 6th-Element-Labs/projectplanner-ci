"""One-use hybrid encryption for delivering a leased credential to a remote worker."""
from __future__ import annotations

import base64
import json
import os
from typing import Any, Mapping

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


SCHEMA = "switchboard.worker_credential_envelope.v1"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    raw = str(value or "").encode("ascii")
    return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))


def _aad(binding: Mapping[str, Any]) -> bytes:
    allowed = {
        key: str(binding.get(key) or "")
        for key in (
            "project", "task_id", "host_id", "runner_session_id",
            "work_session_id", "lease_id",
        )
    }
    if not all(allowed.values()):
        raise ValueError("worker credential envelope binding is incomplete")
    return json.dumps(allowed, sort_keys=True, separators=(",", ":")).encode()


def encrypt_for_worker(credential: str, public_key_pem: str,
                       binding: Mapping[str, Any]) -> dict[str, Any]:
    public_key = serialization.load_pem_public_key(str(public_key_pem or "").encode())
    if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size < 2048:
        raise ValueError("worker public key must be RSA-2048 or stronger")
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    aad = _aad(binding)
    ciphertext = AESGCM(data_key).encrypt(nonce, str(credential or "").encode(), aad)
    wrapped = public_key.encrypt(
        data_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return {
        "schema": SCHEMA,
        "algorithm": "RSA-OAEP-256+A256GCM",
        "wrapped_key": _b64(wrapped),
        "nonce": _b64(nonce),
        "ciphertext": _b64(ciphertext),
        "binding": json.loads(aad.decode()),
    }


def decrypt_on_worker(envelope: Mapping[str, Any], private_key_pem: bytes) -> str:
    if envelope.get("schema") != SCHEMA:
        raise ValueError("worker credential envelope schema mismatch")
    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError("worker private key is invalid")
    data_key = private_key.decrypt(
        _unb64(str(envelope.get("wrapped_key") or "")),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    cleartext = AESGCM(data_key).decrypt(
        _unb64(str(envelope.get("nonce") or "")),
        _unb64(str(envelope.get("ciphertext") or "")),
        _aad(envelope.get("binding") or {}),
    )
    return cleartext.decode()


__all__ = ["SCHEMA", "decrypt_on_worker", "encrypt_for_worker"]
