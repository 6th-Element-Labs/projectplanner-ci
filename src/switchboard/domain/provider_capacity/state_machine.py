"""Pure provider-response normalization for subscription-backed execution lanes.

Raw provider payloads are used only for classification.  The normalized value contains
stable reason codes and timing metadata, never provider messages, headers, tokens, or
credential values.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import time
from typing import Any, Mapping

from switchboard.domain.provider_credentials import normalize_provider


PROVIDER_CAPACITY_SIGNAL_SCHEMA = "switchboard.provider_capacity.signal.v1"
CAPACITY_STATES = frozenset({
    "ready",
    "throttled_retryable",
    "provider_capacity_exhausted",
    "waiting_for_plan_reset",
    "reauthentication_required",
    "revoked",
    "policy_blocked",
})
POLLABLE_CAPACITY_STATES = frozenset({
    "throttled_retryable",
    "provider_capacity_exhausted",
    "waiting_for_plan_reset",
})

_AUTH_CODES = frozenset({
    "authentication_error", "authentication_required", "auth_expired",
    "invalid_auth", "invalid_grant", "invalid_token", "login_required",
    "reauthentication_required", "session_expired", "token_expired",
})
_REVOKED_CODES = frozenset({
    "account_deactivated", "account_disabled", "credential_revoked",
    "oauth_revoked", "revoked", "subscription_revoked", "token_revoked",
})
_PLAN_CODES = frozenset({
    "plan_capacity_exhausted", "plan_limit_reached", "subscription_limit_reached",
    "subscription_quota_exceeded", "usage_limit_reached", "weekly_limit_reached",
})
_METERED_CODES = frozenset({
    "billing_required", "buy_credits", "credit_purchase_required",
    "extra_usage_required", "insufficient_credits", "metered_fallback",
    "pay_as_you_go_required", "payment_required",
})
_CAPACITY_CODES = frozenset({
    "capacity_exhausted", "engine_overloaded", "overloaded_error",
    "provider_capacity_exhausted", "service_overloaded", "temporarily_unavailable",
})
_EXPLICIT_STATE_REASONS = {
    "ready": "provider_ready",
    "throttled_retryable": "provider_throttled",
    "provider_capacity_exhausted": "provider_temporarily_at_capacity",
    "waiting_for_plan_reset": "personal_plan_capacity_exhausted",
    "reauthentication_required": "provider_reauthentication_required",
    "revoked": "provider_credential_revoked",
    "policy_blocked": "provider_policy_blocked",
}


def account_fingerprint(provider: str, account_id: str) -> str:
    provider_id = normalize_provider(provider)
    digest = hashlib.sha256(
        f"{provider_id}\x1f{str(account_id or '').strip()}".encode("utf-8")
    ).hexdigest()
    return f"acct-{digest[:16]}"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _first_number(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed) and parsed >= 0:
            return parsed
    return None


def _status_code(payload: Mapping[str, Any], error: Mapping[str, Any]) -> int:
    for value in (
        payload.get("status_code"), payload.get("http_status"),
        error.get("status_code"), error.get("http_status"), payload.get("status"),
    ):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if 100 <= parsed <= 599:
            return parsed
    return 0


def _timing(payload: Mapping[str, Any], error: Mapping[str, Any], *, now: float,
            state: str) -> tuple[int | None, float | None]:
    headers = _mapping(payload.get("headers"))
    retry = _first_number(
        payload.get("retry_after_seconds"), payload.get("retry_after"),
        error.get("retry_after_seconds"), error.get("retry_after"),
        headers.get("retry-after"), headers.get("Retry-After"),
    )
    reset = _first_number(
        payload.get("reset_at"), payload.get("plan_reset_at"),
        error.get("reset_at"), error.get("plan_reset_at"),
        headers.get("x-ratelimit-reset"), headers.get("anthropic-ratelimit-unified-reset"),
    )
    if reset is not None and reset <= now:
        # Small reset values are relative seconds; larger past values are stale epochs.
        reset = now + reset if reset <= 366 * 24 * 3600 else now
    if retry is None and reset is not None:
        retry = max(0, reset - now)
    if retry is None:
        retry = {
            "throttled_retryable": 60,
            "provider_capacity_exhausted": 120,
            "waiting_for_plan_reset": 900,
        }.get(state)
    if state not in POLLABLE_CAPACITY_STATES:
        return None, None
    bounded_retry = int(min(max(float(retry or 0), 1), 7 * 24 * 3600))
    if reset is None and state == "waiting_for_plan_reset":
        reset = now + bounded_retry
    return bounded_retry, reset


@dataclass(frozen=True)
class ProviderCapacitySignal:
    provider: str
    state: str
    reason_code: str
    retry_after_seconds: int | None
    reset_at: float | None
    observed_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PROVIDER_CAPACITY_SIGNAL_SCHEMA,
            "provider": self.provider,
            "state": self.state,
            "reason_code": self.reason_code,
            "retry_after_seconds": self.retry_after_seconds,
            "reset_at": self.reset_at,
            "observed_at": self.observed_at,
        }


def normalize_provider_response(
    provider: str,
    response: Mapping[str, Any] | None,
    *,
    now: float | None = None,
) -> ProviderCapacitySignal:
    """Classify one provider response without retaining its raw content."""
    provider_id = normalize_provider(provider)
    observed_at = time.time() if now is None else float(now)
    payload = _mapping(response)
    error = _mapping(payload.get("error"))
    explicit_state = _text(payload.get("capacity_state") or payload.get("state"))
    code = _text(
        payload.get("error_code") or payload.get("code")
        or error.get("error_code") or error.get("code") or error.get("type")
    )
    kind = _text(
        payload.get("rate_limit_type") or payload.get("limit_type")
        or error.get("rate_limit_type") or error.get("limit_type")
    )
    status = _status_code(payload, error)
    message = " ".join((
        str(payload.get("message") or ""), str(error.get("message") or ""),
    )).lower()

    # Denial and cooldown signals take precedence over an explicit ready value.  Provider
    # adapters can be stale or contradictory; they cannot use a convenience state field to
    # bypass credential, billing, or capacity policy.
    if explicit_state == "revoked" or code in _REVOKED_CODES or any(word in message for word in (
            "credential revoked", "account deactivated", "account disabled")):
        state, reason = "revoked", "provider_credential_revoked"
    elif explicit_state == "reauthentication_required" or status == 401 \
            or code in _AUTH_CODES or payload.get("authenticated") is False:
        state, reason = "reauthentication_required", "provider_reauthentication_required"
    elif explicit_state == "policy_blocked" or code in _METERED_CODES \
            or any(word in message for word in (
            "buy credits", "extra usage", "pay as you go", "payment required")):
        state, reason = "policy_blocked", "metered_fallback_not_authorized"
    elif explicit_state == "waiting_for_plan_reset" or code in _PLAN_CODES \
            or kind in {"plan", "subscription", "usage", "weekly"} \
            or any(word in message for word in (
                "plan limit", "subscription limit", "usage limit", "weekly limit",
                "resets at", "resets in",
            )):
        state, reason = "waiting_for_plan_reset", "personal_plan_capacity_exhausted"
    elif explicit_state == "throttled_retryable" or status == 429 \
            or code in {"rate_limit_error", "rate_limited", "too_many_requests"}:
        state, reason = "throttled_retryable", "provider_throttled"
    elif explicit_state == "provider_capacity_exhausted" \
            or status in {502, 503, 529} or code in _CAPACITY_CODES:
        state, reason = "provider_capacity_exhausted", "provider_temporarily_at_capacity"
    elif explicit_state == "ready" or bool(payload.get("ok")) or bool(payload.get("ready")) \
            or status in range(200, 300) or explicit_state in {"ok", "success"}:
        state, reason = "ready", "provider_ready"
    else:
        # Capacity classification is fail-closed: an unrecognized provider result is not
        # permission to dispatch or to try a paid fallback.
        state, reason = "policy_blocked", "unrecognized_provider_signal"

    retry_after, reset_at = _timing(payload, error, now=observed_at, state=state)
    return ProviderCapacitySignal(
        provider=provider_id,
        state=state,
        reason_code=reason,
        retry_after_seconds=retry_after,
        reset_at=reset_at,
        observed_at=observed_at,
    )
