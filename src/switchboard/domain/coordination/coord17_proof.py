"""Fail-closed aggregate acceptance contract for COORD-17.

Individual workers already emit provider receipts.  This module answers the harder
closure question: did one evidence bundle prove *all* three personal-account lanes,
tenant isolation, lease fencing, quota recovery, hybrid placement, cleanup, and zero
unauthorized metered spend?  It emits only normalized booleans and redacted account
attribution; raw provider output and credential material are never copied forward.
"""
from __future__ import annotations

from typing import Any, Mapping


SCHEMA = "switchboard.coord17_acceptance.v1"
REQUIRED_PROVIDERS = ("anthropic-claude", "openai-codex", "cursor")
EXPECTED_AUTH_MODES = {
    "anthropic-claude": "oauth_personal",
    "openai-codex": "chatgpt_personal",
    "cursor": "personal_api_key",
}
REQUIRED_BINDING_FIELDS = (
    "tenant_id", "user_id", "provider", "provider_account_id",
    "credential_reference", "credential_lease_id", "project", "task_id",
    "host_id", "runner_session_id", "work_session_id", "claim_id",
)
REQUIRED_ISOLATION_CHECKS = (
    "cross_tenant_denied", "cross_project_denied", "wrong_provider_denied",
    "revoked_credential_denied", "duplicate_codex_capsule_denied",
    "account_pooling_denied",
)
REQUIRED_LEASE_CHECKS = (
    "replay_at_most_one_process", "race_at_most_one_process",
    "start_failure_fenced", "terminal_binding_denied", "stale_binding_denied",
    "mismatched_binding_denied", "exact_principal_release_succeeds",
    "cross_service_release_denied", "cross_user_release_denied",
)
FORBIDDEN_SECRET_KEYS = {
    "credential", "auth_capsule", "access_token", "refresh_token", "setup_token",
    "private_key", "private_key_pem", "api_key", "oauth_token",
}


def _truthy_map(value: Any, required: tuple[str, ...], prefix: str) -> list[str]:
    payload = value if isinstance(value, Mapping) else {}
    return [f"{prefix}.{name}" for name in required if payload.get(name) is not True]


def _string_list(value: Any, prefix: str, blockers: list[str]) -> list[str]:
    """Return a normalized JSON string list or add a fail-closed shape blocker."""
    if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value):
        blockers.append(f"{prefix}.shape")
        return []
    return [item.strip() for item in value]


def _is_zero_count(value: Any) -> bool:
    return type(value) is int and value == 0


def _is_zero_amount(value: Any) -> bool:
    return not isinstance(value, bool) \
        and isinstance(value, (int, float)) \
        and value == 0


def _find_secret_keys(value: Any, path: str = "evidence") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if key.lower() in FORBIDDEN_SECRET_KEYS:
                found.append(child_path)
            found.extend(_find_secret_keys(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_find_secret_keys(child, f"{path}[{index}]"))
    return found


def _provider_summary(provider: str, receipt: Any) -> tuple[dict[str, Any], list[str]]:
    item = receipt if isinstance(receipt, Mapping) else {}
    binding = item.get("binding") if isinstance(item.get("binding"), Mapping) else {}
    blockers: list[str] = []
    if item.get("started") is not True:
        blockers.append(f"providers.{provider}.started")
    if item.get("personal_subscription") is not True:
        blockers.append(f"providers.{provider}.personal_subscription")
    if item.get("auth_mode") != EXPECTED_AUTH_MODES[provider]:
        blockers.append(f"providers.{provider}.auth_mode")
    if item.get("metered_fallback") is not False or item.get("api_key_fallback") is not False:
        blockers.append(f"providers.{provider}.no_metered_fallback")
    if item.get("provider_output_redacted") is not True:
        blockers.append(f"providers.{provider}.provider_output_redacted")
    if item.get("credential_values_redacted") is not True:
        blockers.append(f"providers.{provider}.credential_values_redacted")
    if item.get("residue_purged") is not True:
        blockers.append(f"providers.{provider}.residue_purged")
    if binding.get("provider") != provider:
        blockers.append(f"providers.{provider}.binding.provider")
    blockers.extend(
        f"providers.{provider}.binding.{field}"
        for field in REQUIRED_BINDING_FIELDS if not binding.get(field)
    )
    attribution = str(
        item.get("provider_account_attribution")
        or binding.get("provider_account_attribution") or ""
    )
    if not attribution.startswith("acct-"):
        blockers.append(f"providers.{provider}.provider_account_attribution")
    durable = item.get("durable_evidence") \
        if isinstance(item.get("durable_evidence"), Mapping) else {}
    if not (
        durable.get("work_session_id")
        and durable.get("branch")
        and durable.get("head_sha")
        and durable.get("executed_test_run")
        and (durable.get("pr_url") or durable.get("remote_ref") or durable.get("offline_evidence"))
    ):
        blockers.append(f"providers.{provider}.durable_evidence")
    return {
        "provider": provider,
        "auth_mode": item.get("auth_mode"),
        "provider_account_attribution": attribution or None,
        "started": item.get("started") is True,
        "personal_subscription": item.get("personal_subscription") is True,
        "no_metered_fallback": (
            item.get("metered_fallback") is False and item.get("api_key_fallback") is False
        ),
        "binding_complete": not any(
            blocker.startswith(f"providers.{provider}.binding.") for blocker in blockers),
        "durable_evidence": f"providers.{provider}.durable_evidence" not in blockers,
        "residue_purged": item.get("residue_purged") is True,
    }, blockers


def build_coord17_acceptance(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a COORD-17 evidence bundle and enumerate every missing gate."""
    evidence = evidence if isinstance(evidence, Mapping) else {}
    blockers: list[str] = []
    secret_paths = _find_secret_keys(evidence)
    blockers.extend(f"forbidden_secret_key:{path}" for path in secret_paths)

    raw_providers = evidence.get("providers") \
        if isinstance(evidence.get("providers"), Mapping) else {}
    provider_summaries: dict[str, Any] = {}
    for provider in REQUIRED_PROVIDERS:
        summary, provider_blockers = _provider_summary(provider, raw_providers.get(provider))
        provider_summaries[provider] = summary
        blockers.extend(provider_blockers)

    blockers.extend(_truthy_map(
        evidence.get("isolation"), REQUIRED_ISOLATION_CHECKS, "isolation"))
    blockers.extend(_truthy_map(
        evidence.get("lease_lifecycle"), REQUIRED_LEASE_CHECKS, "lease_lifecycle"))

    placement = evidence.get("hybrid_placement") \
        if isinstance(evidence.get("hybrid_placement"), Mapping) else {}
    host_classes = set(_string_list(
        placement.get("host_classes"), "hybrid_placement.host_classes", blockers))
    if not {"persistent", "ephemeral"}.issubset(host_classes):
        blockers.append("hybrid_placement.persistent_and_ephemeral")
    if placement.get("same_deliverable") is not True:
        blockers.append("hybrid_placement.same_deliverable")
    if placement.get("explainable_decisions") is not True:
        blockers.append("hybrid_placement.explainable_decisions")

    capacity = evidence.get("capacity") \
        if isinstance(evidence.get("capacity"), Mapping) else {}
    states = _string_list(capacity.get("states"), "capacity.states", blockers)
    required_states = ("provider_capacity_exhausted", "waiting_for_plan_reset", "ready")
    cursor = 0
    for state in states:
        if cursor < len(required_states) and state == required_states[cursor]:
            cursor += 1
    if cursor != len(required_states):
        blockers.append("capacity.pause_wait_resume_sequence")
    if capacity.get("bounded_probes") is not True:
        blockers.append("capacity.bounded_probes")
    if capacity.get("retry_storm") is not False:
        blockers.append("capacity.no_retry_storm")
    if capacity.get("metered_fallback") is not False:
        blockers.append("capacity.no_metered_fallback")

    teardown = evidence.get("teardown") \
        if isinstance(evidence.get("teardown"), Mapping) else {}
    if not _is_zero_count(teardown.get("aws_active_instances")):
        blockers.append("teardown.aws_scale_to_zero")
    if teardown.get("persistent_host_registered") is not True:
        blockers.append("teardown.persistent_host_registered")
    if teardown.get("all_provider_residue_purged") is not True:
        blockers.append("teardown.all_provider_residue_purged")
    if not _is_zero_amount(teardown.get("unauthorized_metered_spend")):
        blockers.append("teardown.zero_unauthorized_metered_spend")

    unique_blockers = sorted(set(blockers))
    return {
        "schema": SCHEMA,
        "task_id": "COORD-17",
        "passed": not unique_blockers,
        "provider_count": len(REQUIRED_PROVIDERS),
        "providers": provider_summaries,
        "isolation_check_count": len(REQUIRED_ISOLATION_CHECKS),
        "lease_check_count": len(REQUIRED_LEASE_CHECKS),
        "hybrid_host_classes": sorted(host_classes & {"persistent", "ephemeral"}),
        "capacity_states": states,
        "aws_active_instances": teardown.get("aws_active_instances"),
        "unauthorized_metered_spend": teardown.get("unauthorized_metered_spend"),
        "credential_values_redacted": not secret_paths,
        "blocker_count": len(unique_blockers),
        "blockers": unique_blockers,
    }


__all__ = [
    "EXPECTED_AUTH_MODES",
    "REQUIRED_BINDING_FIELDS",
    "REQUIRED_ISOLATION_CHECKS",
    "REQUIRED_LEASE_CHECKS",
    "REQUIRED_PROVIDERS",
    "SCHEMA",
    "build_coord17_acceptance",
]
