"""Server-authoritative provider authentication capability policy (CO-15).

The matrix in this module is the one source consumed by enrollment, leases,
scheduling, runtime launch, REST/MCP, Settings, and the CO-14 proof console.
Unknown combinations and stale evidence fail closed.  A UI label or an enrolled
legacy row can never upgrade a denied capability.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import time
from typing import Any, Mapping

from .policy import CredentialPolicyError, normalize_provider


PROVIDER_AUTH_CAPABILITY_SCHEMA = "switchboard.provider_auth_capability.v1"
PROVIDER_AUTH_MATRIX_SCHEMA = "switchboard.provider_auth_capability_matrix.v1"
PROVIDER_AUTH_DECISION_SCHEMA = "switchboard.provider_auth_policy_decision.v1"
PROVIDER_AUTH_POLICY_VERSION = "2026-07-16"

CAPABILITY_STATES = frozenset({
    "supported",
    "supported_host_bound",
    "vendor_confirmation_required",
    "unavailable",
})
ALLOWED_CAPABILITY_STATES = frozenset({"supported", "supported_host_bound"})

# API / paygo modes may run on broad managed fleets. Personal modes declare a
# restrictive host_class and must match server-derived host classification.
_BROAD_HOST_CLASSES = frozenset({"managed_or_user_owned_worker"})
_EXECUTION_OPERATIONS = frozenset({
    "lease", "lease_admission", "materialize", "activation", "launch", "schedule",
    "use",
})


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


_REVIEWED_AT = "2026-07-16T00:00:00Z"
_REVALIDATE_AFTER = "2027-07-16T00:00:00Z"


def _evidence(*urls: str) -> dict[str, Any]:
    return {
        "reviewed_at": _REVIEWED_AT,
        "revalidate_after": _REVALIDATE_AFTER,
        "sources": list(urls),
        "source_class": "official_vendor_documentation",
    }


# These records describe product capability, not stored customer credentials.
# auth_type_aliases are accepted only to translate existing CO-6 records into a
# canonical auth_mode; they are omitted from public responses.
_CAPABILITIES: tuple[dict[str, Any], ...] = (
    {
        "capability_id": "codex-chatgpt-capsule-trusted-private",
        "provider": "openai-codex",
        "auth_mode": "chatgpt_subscription",
        "auth_type_aliases": (
            "chatgpt_auth_capsule", "chatgpt_personal", "oauth_capsule",
            "personal_subscription", "subscription",
        ),
        "host_class": "trusted_private_worker",
        "portability": "encrypted_opaque_capsule",
        "bootstrap_method": "codex_auth_json_capsule",
        "concurrency": {"mode": "exclusive", "max_parallel": 1},
        "state": "supported",
        "approval_state": "official_first_party_auth_documented",
        "disable_reason": "",
        "execution_path": "native_codex_cli",
        "litellm": {"eligible": False, "reason": "personal_subscription_not_api_auth"},
        "evidence": _evidence(
            "https://learn.chatgpt.com/docs/auth",
            "https://learn.chatgpt.com/docs/auth/ci-cd-auth",
        ),
    },
    {
        "capability_id": "codex-api-key-managed-worker",
        "provider": "openai-codex",
        "auth_mode": "api_key",
        "auth_type_aliases": (
            "api_key", "customer_api_key", "customer_metered_api", "metered_api",
        ),
        "host_class": "managed_or_user_owned_worker",
        "portability": "portable_secret",
        "bootstrap_method": "codex_api_key_login",
        "concurrency": {"mode": "bounded", "max_parallel": "provider_and_budget_policy"},
        "state": "supported",
        "approval_state": "official_automation_auth_documented",
        "disable_reason": "",
        "execution_path": "native_cli_or_api_gateway",
        "litellm": {"eligible": True, "reason": "api_paygo_only"},
        "evidence": _evidence("https://learn.chatgpt.com/docs/auth"),
    },
    {
        "capability_id": "claude-subscription-switchboard",
        "provider": "anthropic-claude",
        "auth_mode": "claude_subscription_oauth",
        "auth_type_aliases": (
            "oauth_capsule", "personal_subscription", "setup_token",
            "setup_token_oauth", "subscription",
        ),
        "host_class": "switchboard_managed_or_user_owned_worker",
        "portability": "token_documented_but_third_party_use_unapproved",
        "bootstrap_method": "claude_setup_token",
        "concurrency": {"mode": "exclusive", "max_parallel": 1},
        "state": "vendor_confirmation_required",
        "approval_state": "written_vendor_confirmation_required",
        "disable_reason": "anthropic_third_party_subscription_routing_not_permitted",
        "execution_path": "disabled",
        "litellm": {"eligible": False, "reason": "personal_subscription_not_api_auth"},
        "evidence": _evidence(
            "https://code.claude.com/docs/en/cli-usage",
            "https://code.claude.com/docs/en/legal-and-compliance",
        ),
    },
    {
        # CO-22: host-bound personal-subscription posture. Distinct from the
        # portable `claude-subscription-switchboard` entry above, which stays
        # disabled. Here the operator runs `claude /login` ON the host; the
        # credential lives only in that host's OS keychain and is never minted
        # (no `claude setup-token`), exported, or brokered by Switchboard. This
        # mirrors the already-approved `cursor-browser-login-user-host` posture
        # and removes exactly what the portable entry's disable_reason names —
        # third-party subscription ROUTING. Approved by operator decision
        # (decision-979, 2026-07-24) accepting the residual Consumer-Terms risk;
        # no separate written Anthropic confirmation was obtained. Aliases are
        # kept disjoint from the portable entry so `oauth_personal` (what the
        # runtime preflight yields) resolves here, while `setup_token*` /
        # `subscription` still resolve to the disabled portable entry.
        "capability_id": "claude-host-bound-native-cli",
        "provider": "anthropic-claude",
        "auth_mode": "claude_subscription_oauth",
        "auth_type_aliases": (
            "browser_login", "claude_ai_oauth", "claude_login_on_host",
            "host_login", "local_login_session", "oauth_personal",
        ),
        "host_class": "user_owned_persistent",
        "portability": "host_bound",
        "bootstrap_method": "claude_login_on_host",
        "concurrency": {"mode": "exclusive", "max_parallel": 1},
        "state": "supported_host_bound",
        "approval_state": "operator_accepted_host_bound_local_cli",
        "disable_reason": "",
        "execution_path": "registered_agent_host_native_cli",
        "litellm": {"eligible": False, "reason": "personal_subscription_not_api_auth"},
        "evidence": _evidence(
            "https://code.claude.com/docs/en/cli-usage",
            "https://code.claude.com/docs/en/legal-and-compliance",
        ),
    },
    {
        "capability_id": "claude-api-key-managed-worker",
        "provider": "anthropic-claude",
        "auth_mode": "api_key",
        "auth_type_aliases": (
            "api_key", "customer_api_key", "customer_metered_api", "metered_api",
        ),
        "host_class": "managed_or_user_owned_worker",
        "portability": "portable_secret",
        "bootstrap_method": "anthropic_api_key",
        "concurrency": {"mode": "bounded", "max_parallel": "provider_and_budget_policy"},
        "state": "supported",
        "approval_state": "official_automation_auth_documented",
        "disable_reason": "",
        "execution_path": "native_cli_or_api_gateway",
        "litellm": {"eligible": True, "reason": "api_paygo_only"},
        "evidence": _evidence("https://code.claude.com/docs/en/env-vars"),
    },
    {
        "capability_id": "cursor-browser-login-user-host",
        "provider": "cursor",
        "auth_mode": "cursor_personal_browser",
        "auth_type_aliases": ("browser_login", "local_browser_session"),
        "host_class": "user_owned_persistent",
        "portability": "host_bound",
        "bootstrap_method": "cursor_browser_login_on_host",
        "concurrency": {"mode": "exclusive", "max_parallel": 1},
        "state": "supported_host_bound",
        "approval_state": "official_local_cli_auth_documented",
        "disable_reason": "",
        "execution_path": "registered_agent_host_native_cli",
        "litellm": {"eligible": False, "reason": "personal_subscription_not_api_auth"},
        "evidence": _evidence("https://docs.cursor.com/en/cli/reference/authentication"),
    },
    {
        "capability_id": "cursor-personal-portable-worker",
        "provider": "cursor",
        "auth_mode": "cursor_personal_browser",
        "auth_type_aliases": (
            "personal_subscription", "session_capsule", "subscription",
        ),
        "host_class": "managed_or_ephemeral_worker",
        "portability": "unsupported",
        "bootstrap_method": "none_documented",
        "concurrency": {"mode": "exclusive", "max_parallel": 1},
        "state": "unavailable",
        "approval_state": "supported_portable_bootstrap_not_documented",
        "disable_reason": "cursor_personal_auth_portability_unavailable",
        "execution_path": "disabled",
        "litellm": {"eligible": False, "reason": "personal_subscription_not_api_auth"},
        "evidence": _evidence(
            "https://docs.cursor.com/en/cli/reference/authentication",
            "https://docs.cursor.com/en/cli/headless",
        ),
    },
    {
        "capability_id": "cursor-api-key-worker",
        "provider": "cursor",
        "auth_mode": "api_key",
        "auth_type_aliases": (
            "api_key", "customer_api_key", "customer_metered_api", "metered_api",
            "personal_api_key",
        ),
        "host_class": "managed_or_user_owned_worker",
        "portability": "portable_secret",
        "bootstrap_method": "cursor_api_key",
        "concurrency": {"mode": "bounded", "max_parallel": "provider_and_budget_policy"},
        "state": "supported",
        "approval_state": "official_automation_auth_documented",
        "disable_reason": "",
        "execution_path": "native_cursor_cli",
        "litellm": {"eligible": False, "reason": "cursor_agent_key_not_llm_gateway_auth"},
        "evidence": _evidence(
            "https://docs.cursor.com/en/cli/reference/authentication",
            "https://docs.cursor.com/en/cli/headless",
        ),
    },
)


def _public_record(record: Mapping[str, Any], *, now: float) -> dict[str, Any]:
    item = deepcopy(dict(record))
    item.pop("auth_type_aliases", None)
    evidence = dict(item.get("evidence") or {})
    fresh = now < _epoch(str(evidence.get("revalidate_after") or _REVIEWED_AT))
    evidence["freshness"] = "current" if fresh else "stale"
    evidence["fresh"] = fresh
    item["evidence"] = evidence
    item["schema"] = PROVIDER_AUTH_CAPABILITY_SCHEMA
    item["policy_version"] = PROVIDER_AUTH_POLICY_VERSION
    item["effective_state"] = item["state"] if fresh else "unavailable"
    if not fresh:
        item["effective_disable_reason"] = "provider_auth_policy_evidence_stale"
    else:
        item["effective_disable_reason"] = item.get("disable_reason") or ""
    item["allowed"] = item["effective_state"] in ALLOWED_CAPABILITY_STATES
    return item


def list_provider_auth_capabilities(*, now: float | None = None) -> dict[str, Any]:
    """Return the complete non-secret matrix for REST, MCP, Settings, and proof UI."""
    timestamp = time.time() if now is None else float(now)
    return {
        "schema": PROVIDER_AUTH_MATRIX_SCHEMA,
        "policy_version": PROVIDER_AUTH_POLICY_VERSION,
        "generated_at": timestamp,
        "fail_closed": True,
        "personal_subscription_broker": {
            "litellm": False,
            "reason": "litellm_is_api_gateway_not_personal_subscription_auth_broker",
        },
        "capabilities": [_public_record(record, now=timestamp) for record in _CAPABILITIES],
    }


def _normalized_auth_type(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _host_placement(host: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(host or {})
    direct = raw.get("placement")
    capacity = raw.get("capacity") if isinstance(raw.get("capacity"), Mapping) else {}
    nested = capacity.get("placement") if isinstance(capacity, Mapping) else None
    return dict(direct or nested or {})


def auth_host_classes_for_host(host: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Derive capability-taxonomy host classes from a registered Agent Host.

    Placement's scheduler taxonomy (``persistent`` / ``ephemeral``) is an *input*
    only. Callers cannot elevate a host by sending a string ``host_class``.
    """
    placement = _host_placement(host)
    advertised: list[str] = []
    for key in ("auth_host_classes", "capability_host_classes"):
        raw = placement.get(key) or (host or {}).get(key)
        if isinstance(raw, str):
            advertised.extend(part.strip() for part in raw.split(","))
        elif isinstance(raw, (list, tuple, set)):
            advertised.extend(str(item or "").strip() for item in raw)
    single = placement.get("auth_host_class") or (host or {}).get("auth_host_class")
    if single:
        advertised.append(str(single))
    classes = tuple(dict.fromkeys(
        _normalized_auth_type(item) for item in advertised if _normalized_auth_type(item)
    ))
    if classes:
        return classes

    host_id = str((host or {}).get("host_id") or "")
    scheduler_class = str(placement.get("host_class") or "").strip().lower()
    bound_wake = str(placement.get("bound_wake_id") or "").strip()
    leases = bool(placement.get("supports_credential_leases"))
    ephemeral = (
        bool(bound_wake)
        or scheduler_class == "ephemeral"
        or host_id.startswith("host/i-")
    )
    if ephemeral:
        return ("managed_or_ephemeral_worker",)
    if not host:
        return ()
    # Always-on (non-ephemeral) Agent Hosts are the trusted-private /
    # user-owned persistent class for personal auth. Credential-lease support
    # is expected on those hosts but is not the sole trust signal — wake-bound
    # / host/i-* fleet VMs are already excluded above.
    if scheduler_class in {"", "persistent"} or leases:
        return ("trusted_private_worker", "user_owned_persistent")
    return ("managed_or_user_owned_worker",)


def host_class_satisfies(actual_classes: set[str], required: str) -> bool:
    """Whether server-derived host classes satisfy a capability's host_class."""
    required_id = _normalized_auth_type(required)
    if not required_id:
        return True
    if required_id in _BROAD_HOST_CLASSES:
        if not actual_classes:
            return True
        return bool(actual_classes & {
            "managed_or_user_owned_worker",
            "user_owned_persistent",
            "trusted_private_worker",
            "switchboard_managed_or_user_owned_worker",
            "managed_or_ephemeral_worker",
        })
    return required_id in actual_classes


def concurrency_policy_for_capability(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the forced exclusive policy for personal modes; None keeps caller policy."""
    concurrency = dict(record.get("concurrency") or {})
    mode = str(concurrency.get("mode") or "").strip().lower()
    maximum = concurrency.get("max_parallel")
    if mode == "exclusive" or maximum == 1:
        return {"mode": "exclusive", "max_parallel": 1}
    return None


def provider_auth_decision(
    provider: str,
    auth_type: str,
    *,
    host_class: str = "",
    host_classes: list[str] | tuple[str, ...] | None = None,
    operation: str = "use",
    now: float | None = None,
) -> dict[str, Any]:
    """Resolve one exact provider/auth/host tuple; any uncertainty is a denial."""
    timestamp = time.time() if now is None else float(now)
    operation_id = str(operation or "use").strip().lower() or "use"
    try:
        provider_id = normalize_provider(provider)
    except CredentialPolicyError:
        return {
            "schema": PROVIDER_AUTH_DECISION_SCHEMA,
            "allowed": False,
            "state": "unavailable",
            "reason_code": "provider_auth_provider_unknown",
            "operation": operation_id,
        }
    auth_id = _normalized_auth_type(auth_type)
    candidates = [
        record for record in _CAPABILITIES
        if record["provider"] == provider_id
        and auth_id in record["auth_type_aliases"]
    ]
    actual_classes = {
        _normalized_auth_type(item)
        for item in list(host_classes or ()) + ([host_class] if host_class else [])
        if _normalized_auth_type(item)
    }
    if actual_classes:
        exact = [
            record for record in candidates
            if host_class_satisfies(actual_classes, str(record["host_class"]))
        ]
        if exact:
            candidates = exact
    # Cursor browser auth has two explicit records. Without an exact persistent
    # host binding, choose the portable record so enrollment/lease/schedule cannot
    # accidentally treat a local browser session as exportable.
    if len(candidates) > 1:
        portable = [r for r in candidates if r["state"] == "unavailable"]
        candidates = portable or candidates
    if len(candidates) != 1:
        return {
            "schema": PROVIDER_AUTH_DECISION_SCHEMA,
            "allowed": False,
            "provider": provider_id,
            "auth_type": auth_id,
            "state": "unavailable",
            "reason_code": "provider_auth_mode_unknown",
            "operation": operation_id,
            "host_classes": sorted(actual_classes),
        }
    record = _public_record(candidates[0], now=timestamp)
    state = str(record["effective_state"])
    reason = str(record.get("effective_disable_reason") or "")
    allowed = state in ALLOWED_CAPABILITY_STATES
    required_host = _normalized_auth_type(record["host_class"])
    if allowed and required_host:
        if state == "supported_host_bound":
            allowed = host_class_satisfies(actual_classes, required_host)
            if not allowed:
                reason = "provider_auth_host_binding_required"
        elif required_host not in _BROAD_HOST_CLASSES:
            # Restrictive personal modes: enrollment may store the capsule without a
            # host, but every execution boundary must match a trusted class.
            if operation_id == "enrollment" and not actual_classes:
                pass
            elif not host_class_satisfies(actual_classes, required_host):
                allowed = False
                reason = (
                    "provider_auth_host_class_mismatch" if actual_classes
                    else "provider_auth_host_class_required"
                )
            elif operation_id in _EXECUTION_OPERATIONS and not actual_classes:
                allowed = False
                reason = "provider_auth_host_class_required"
    if not allowed:
        if record["evidence"]["freshness"] == "stale":
            reason = "provider_auth_policy_evidence_stale"
        elif state == "vendor_confirmation_required":
            reason = "provider_auth_vendor_confirmation_required"
        elif not reason:
            reason = "provider_auth_mode_unavailable"
    forced = concurrency_policy_for_capability(record)
    return {
        "schema": PROVIDER_AUTH_DECISION_SCHEMA,
        "allowed": allowed,
        "provider": provider_id,
        "auth_type": auth_id,
        "auth_mode": record["auth_mode"],
        "host_class": record["host_class"],
        "host_classes": sorted(actual_classes),
        "state": state,
        "reason_code": "provider_auth_policy_allowed" if allowed else reason,
        "capability_id": record["capability_id"],
        "policy_version": PROVIDER_AUTH_POLICY_VERSION,
        "evidence_freshness": record["evidence"]["freshness"],
        "approval_state": record["approval_state"],
        "operation": operation_id,
        "litellm_eligible": bool((record.get("litellm") or {}).get("eligible")),
        "concurrency": forced or dict(record.get("concurrency") or {}),
        "forced_concurrency_policy": forced,
    }


__all__ = [
    "ALLOWED_CAPABILITY_STATES",
    "CAPABILITY_STATES",
    "PROVIDER_AUTH_CAPABILITY_SCHEMA",
    "PROVIDER_AUTH_DECISION_SCHEMA",
    "PROVIDER_AUTH_MATRIX_SCHEMA",
    "PROVIDER_AUTH_POLICY_VERSION",
    "auth_host_classes_for_host",
    "concurrency_policy_for_capability",
    "host_class_satisfies",
    "list_provider_auth_capabilities",
    "provider_auth_decision",
]
