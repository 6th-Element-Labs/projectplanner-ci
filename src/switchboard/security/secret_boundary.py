"""Final containment for the provider credential owned by the local gateway."""
from __future__ import annotations

import os
from typing import Any

REDACTED = "[REDACTED]"
_PROVIDER_SECRET_ENV = ("OPENAI_API_KEY",)


def _known_provider_secrets() -> tuple[str, ...]:
    """Read live values without caching or exposing them in diagnostics."""
    return tuple({
        value for name in _PROVIDER_SECRET_ENV
        if len(value := str(os.environ.get(name) or "")) >= 8
    })


def redact_provider_secrets(value: Any) -> Any:
    """Return a shape-preserving copy with known provider-key values removed."""
    secrets = _known_provider_secrets()
    if not secrets:
        return value
    if isinstance(value, dict):
        return {key: redact_provider_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_provider_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_provider_secrets(item) for item in value)
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, REDACTED)
        return value
    return value


def redact_provider_secrets_bytes(value: bytes) -> bytes:
    """Redact exact key bytes in an HTTP response without decoding its payload."""
    for secret in _known_provider_secrets():
        value = value.replace(secret.encode(), REDACTED.encode())
    return value
