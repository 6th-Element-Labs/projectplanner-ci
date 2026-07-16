"""ENFORCE-8 ownership and execution-connection policy.

This module is deliberately pure.  Transport, scheduler, vault, and runner paths all
consume the same normalized decision so no surface can invent a weaker interpretation
of "this human's connection" or silently enable a paid fallback.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

from .policy import CredentialPolicyError, normalize_provider


EXECUTION_CONNECTION_POLICY_SCHEMA = "switchboard.execution_connection_policy.v1"
PROVIDER_OWNERSHIP_PROOF_SCHEMA = "switchboard.provider_ownership_proof.v1"

CONNECTION_KINDS = frozenset({
    "personal_subscription", "direct_api", "api_gateway",
})

# These names are rejected recursively at every browser/REST/MCP enrollment boundary.
# Repository and trusted runner bridges are intentionally not public transports.
FORBIDDEN_PUBLIC_SECRET_FIELDS = frozenset({
    "access_token", "api_key", "auth_capsule", "authorization", "bearer_token",
    "browser_profile", "browser_profile_database", "client_secret", "cookie",
    "cookie_jar", "cookies", "credential", "google_credentials", "oauth_token",
    "password", "raw_token", "refresh_token", "secret", "session_cookie",
    "session_token", "token",
})


def _text(value: Any) -> str:
    return str(value or "").strip()


def forbidden_public_secret_paths(value: Any, prefix: str = "") -> list[str]:
    """Return secret-shaped input paths without ever copying their values."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key or "").strip().lower()
            path = f"{prefix}.{key}" if prefix else key
            if key in FORBIDDEN_PUBLIC_SECRET_FIELDS:
                found.append(path)
            found.extend(forbidden_public_secret_paths(item, path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(forbidden_public_secret_paths(item, f"{prefix}[{index}]"))
    return sorted(set(found))


def require_secret_free_public_payload(value: Mapping[str, Any] | None) -> None:
    if forbidden_public_secret_paths(value or {}):
        raise CredentialPolicyError(
            "provider_native_enrollment_required",
            "public provider-connection APIs accept redacted provider-native proof only",
        )


def normalize_execution_connection_policy(
    value: Mapping[str, Any] | None,
    *,
    connection_kind: str = "personal_subscription",
    billing_account_id: str = "",
) -> dict[str, Any]:
    """Normalize billing and fallback policy; omission always means disabled."""
    raw = dict(value or {})
    kind = _text(connection_kind or raw.get("connection_kind")).lower()
    if kind not in CONNECTION_KINDS:
        raise CredentialPolicyError(
            "connection_kind_invalid", "execution connection kind is invalid")
    billing = _text(billing_account_id or raw.get("billing_account_id"))
    budget = dict(raw.get("budget") or raw.get("budget_policy") or {})
    fallback = dict(raw.get("fallback") or raw.get("fallback_policy") or {})

    if kind == "personal_subscription":
        if billing or budget:
            raise CredentialPolicyError(
                "personal_subscription_billing_forbidden",
                "personal subscription connections cannot carry API billing policy",
            )
    else:
        if not billing:
            raise CredentialPolicyError(
                "billing_account_required", "API execution connections require billing attribution")
        try:
            ceiling = float(budget.get("ceiling"))
        except (TypeError, ValueError):
            ceiling = 0
        if (not _text(budget.get("budget_id")) or not _text(budget.get("currency"))
                or not math.isfinite(ceiling) or ceiling <= 0):
            raise CredentialPolicyError(
                "budget_policy_required",
                "API execution connections require budget id, currency, and a positive ceiling",
            )
        budget = {
            "budget_id": _text(budget["budget_id"])[:128],
            "currency": _text(budget["currency"]).upper()[:16],
            "ceiling": ceiling,
        }

    enabled = fallback.get("enabled") is True
    normalized_fallback: dict[str, Any] = {"enabled": False}
    if enabled:
        target = _text(fallback.get("target_execution_connection_id"))
        audit = dict(fallback.get("audited_opt_in") or {})
        try:
            approved_at = float(audit.get("approved_at") or 0)
        except (TypeError, ValueError):
            approved_at = 0
        if (not target or not _text(audit.get("actor")) or not _text(audit.get("audit_id"))
                or not math.isfinite(approved_at) or approved_at <= 0):
            raise CredentialPolicyError(
                "audited_fallback_policy_required",
                "fallback requires a separate selected connection and audited opt-in",
            )
        normalized_fallback = {
            "enabled": True,
            "target_execution_connection_id": target,
            "audited_opt_in": {
                "actor": _text(audit["actor"])[:160],
                "audit_id": _text(audit["audit_id"])[:160],
                "approved_at": approved_at,
            },
        }

    return {
        "schema": EXECUTION_CONNECTION_POLICY_SCHEMA,
        "connection_kind": kind,
        "billing_account_id": billing,
        "budget": budget if kind != "personal_subscription" else {},
        "fallback": normalized_fallback,
    }


def ownership_proof(*, tenant_id: str, user_id: str, provider: str,
                    provider_account_id: str, execution_connection_id: str,
                    connection_kind: str) -> dict[str, Any]:
    """Build a stable redacted proof.  Raw account/user ids never appear in it."""
    provider_id = normalize_provider(provider)
    parts = (
        _text(tenant_id), _text(user_id), provider_id, _text(provider_account_id),
        _text(execution_connection_id), _text(connection_kind),
    )
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
    account_digest = hashlib.sha256(
        f"{provider_id}\x1f{_text(provider_account_id)}".encode()).hexdigest()
    return {
        "schema": PROVIDER_OWNERSHIP_PROOF_SCHEMA,
        "provider": provider_id,
        "account_fingerprint": f"acct-{account_digest[:16]}",
        "owner_binding_digest": f"sha256:{digest}",
        "connection_kind": _text(connection_kind),
    }


def policy_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
