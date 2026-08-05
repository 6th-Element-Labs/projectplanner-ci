"""Provider-independent security primitives for the Compand pilot gateway."""

from __future__ import annotations

import hashlib
import secrets
import threading
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit


class GatewayMode(StrEnum):
    PASSTHROUGH = "passthrough"
    SCAN = "scan"
    ENFORCE = "enforce"


class GatewaySecurityError(ValueError):
    """Configuration violates the gateway's fixed direct-provider boundary."""


@dataclass(frozen=True)
class CredentialAuthentication:
    accepted: bool
    credential_id: str | None
    classification: str


@dataclass(frozen=True)
class _CredentialRecord:
    credential_id: str
    token_digest: bytes
    revoked: bool = False


class ClientCredentialRegistry:
    """In-memory credential verifier that never retains a plaintext client token."""

    def __init__(
        self,
        credentials: dict[str, str],
        revoked_ids: set[str] | frozenset[str] = frozenset(),
    ):
        records: dict[str, _CredentialRecord] = {}
        token_digests: set[bytes] = set()
        for credential_id, token in credentials.items():
            if (
                not isinstance(credential_id, str)
                or not credential_id.strip()
                or not isinstance(token, str)
                or not token
            ):
                raise GatewaySecurityError(
                    "client credentials require non-empty string ids and tokens"
                )
            clean_id = credential_id.strip()
            clean_token = token
            if clean_id in records:
                raise GatewaySecurityError("client credential ids must be unique")
            token_digest = _token_digest(clean_token)
            if token_digest in token_digests:
                raise GatewaySecurityError("client credential tokens must be unique")
            token_digests.add(token_digest)
            records[clean_id] = _CredentialRecord(
                credential_id=clean_id,
                token_digest=token_digest,
                revoked=clean_id in revoked_ids,
            )
        self._records = records
        self._lock = threading.RLock()

    def authenticate(self, token: str) -> CredentialAuthentication:
        supplied = _token_digest(token) if token else b""
        with self._lock:
            matched: _CredentialRecord | None = None
            for record in self._records.values():
                if supplied and secrets.compare_digest(record.token_digest, supplied):
                    matched = record
            if matched is None:
                return CredentialAuthentication(False, None, "client_auth_failed")
            if matched.revoked:
                return CredentialAuthentication(
                    False, matched.credential_id, "client_credential_revoked"
                )
            return CredentialAuthentication(
                True, matched.credential_id, "client_authenticated"
            )

    def contains_token(self, token: str) -> bool:
        """Return whether a plaintext candidate matches a retained client digest.

        This is deliberately a membership check rather than a token export: startup can
        enforce the client/upstream identity boundary without retaining another plaintext
        copy or exposing the registry's digests.
        """

        if not isinstance(token, str):
            raise GatewaySecurityError("credential comparison requires a string token")
        supplied = _token_digest(token) if token else b""
        with self._lock:
            records = tuple(self._records.values())
        matched = False
        for record in records:
            digest_matches = bool(
                supplied and secrets.compare_digest(record.token_digest, supplied)
            )
            matched = digest_matches or matched
        return matched

    def revoke(self, credential_id: str) -> bool:
        with self._lock:
            record = self._records.get(credential_id)
            if record is None:
                return False
            self._records[credential_id] = _CredentialRecord(
                credential_id=record.credential_id,
                token_digest=record.token_digest,
                revoked=True,
            )
        return True


def _token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def validate_upstream_origin(origin: str, *, allow_http_loopback: bool = False) -> str:
    """Return one normalized provider origin or fail closed.

    The pilot deliberately accepts an origin, not a routable model/provider map. HTTP is
    allowed only for explicit loopback fixture tests.
    """

    value = str(origin or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.username or parsed.password:
        raise GatewaySecurityError("upstream origin must not embed credentials")
    if parsed.query or parsed.fragment:
        raise GatewaySecurityError(
            "upstream origin must not contain query or fragment data"
        )
    if parsed.path not in {"", "/"}:
        raise GatewaySecurityError("upstream origin must not contain a path")
    if parsed.scheme == "https" and parsed.hostname == "api.openai.com":
        return value
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if allow_http_loopback and parsed.scheme == "http" and loopback:
        return value
    raise GatewaySecurityError(
        "the pilot upstream must be https://api.openai.com (HTTP is fixture-loopback only)"
    )
