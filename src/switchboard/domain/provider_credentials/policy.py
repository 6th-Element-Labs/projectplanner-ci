"""Provider identity and concurrency policy invariants."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


PROVIDER_ALIASES = {
    "openai": "openai-codex",
    "codex": "openai-codex",
    "chatgpt": "openai-codex",
    "openai-codex": "openai-codex",
    "anthropic": "anthropic-claude",
    "claude": "anthropic-claude",
    "claude-code": "anthropic-claude",
    "anthropic-claude": "anthropic-claude",
    "cursor": "cursor",
}
ALLOWED_PROVIDERS = frozenset(PROVIDER_ALIASES.values())
ALLOWED_CONCURRENCY_MODES = frozenset({"exclusive", "bounded"})
PROVIDER_CREDENTIAL_PRINCIPAL_KINDS = frozenset({"user", "agent", "host", "system"})


class CredentialPolicyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CredentialPrincipal:
    """Authenticated actor carried intact across every lease boundary."""

    principal_id: str
    principal_kind: str
    scopes: tuple[str, ...] = ()
    admin: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "CredentialPrincipal":
        raw = dict(value or {})
        principal_id = str(raw.get("principal_id") or raw.get("id") or "").strip()
        kind = str(raw.get("principal_kind") or raw.get("kind") or "").strip().lower()
        if kind == "human":
            kind = "user"
        raw_scopes = raw.get("effective_scopes") or raw.get("scopes") or ()
        if isinstance(raw_scopes, str):
            raw_scopes = raw_scopes.split(",")
        scopes = tuple(sorted({str(item or "").strip() for item in raw_scopes if str(item or "").strip()}))
        admin = bool(raw.get("admin")) or "admin" in scopes
        if not principal_id:
            raise CredentialPolicyError(
                "credential_principal_invalid", "credential principal id is required")
        if kind not in PROVIDER_CREDENTIAL_PRINCIPAL_KINDS:
            raise CredentialPolicyError(
                "credential_principal_invalid", "credential principal kind is invalid")
        return cls(principal_id=principal_id, principal_kind=kind,
                   scopes=scopes, admin=admin)

    def can_use_credentials(self) -> bool:
        return self.admin or "use:credentials" in self.scopes

    def as_dict(self) -> dict[str, Any]:
        return {
            "principal_id": self.principal_id,
            "principal_kind": self.principal_kind,
            "scopes": list(self.scopes),
            "admin": self.admin,
        }


def normalize_provider(value: str) -> str:
    provider = PROVIDER_ALIASES.get(str(value or "").strip().lower(), "")
    if provider not in ALLOWED_PROVIDERS:
        raise CredentialPolicyError("provider_not_supported", "provider is not supported")
    return provider


def validate_auth_type(value: str) -> str:
    auth_type = str(value or "").strip().lower()
    if not auth_type:
        raise CredentialPolicyError("auth_type_required", "auth_type is required")
    if "github" in auth_type or auth_type in {"repository", "github_app"}:
        raise CredentialPolicyError(
            "github_authorization_separate",
            "GitHub repository authorization is separate from provider authentication",
        )
    return auth_type


def normalize_concurrency_policy(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(value or {})
    mode = str(raw.get("mode") or "exclusive").strip().lower()
    if mode not in ALLOWED_CONCURRENCY_MODES:
        raise CredentialPolicyError(
            "concurrency_policy_invalid", "concurrency policy mode is invalid")
    try:
        maximum = int(raw.get("max_parallel", 1))
    except (TypeError, ValueError) as exc:
        raise CredentialPolicyError(
            "concurrency_policy_invalid", "max_parallel must be an integer") from exc
    if maximum < 1 or maximum > 64:
        raise CredentialPolicyError(
            "concurrency_policy_invalid", "max_parallel must be between 1 and 64")
    if mode == "exclusive" and maximum != 1:
        raise CredentialPolicyError(
            "concurrency_policy_invalid", "exclusive credentials require max_parallel=1")
    return {"mode": mode, "max_parallel": maximum}
