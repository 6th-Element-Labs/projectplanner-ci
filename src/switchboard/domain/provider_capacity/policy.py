"""Default-deny policy for any lane that can incur provider charges."""
from __future__ import annotations

import math
from typing import Any, Mapping


PERSONAL_LANE_KINDS = frozenset({
    "personal", "personal_plan", "personal_subscription", "subscription",
})
_METERED_LANE_ALIASES = {
    "api": "api",
    "api_key": "api",
    "credit": "credit_purchase",
    "credits": "credit_purchase",
    "credit_purchase": "credit_purchase",
    "extra_usage": "extra_usage",
    "metered": "metered",
    "paid_api": "api",
    "pay_as_you_go": "pay_as_you_go",
    "payg": "pay_as_you_go",
    "provider_extra_usage": "extra_usage",
}
METERED_LANE_KINDS = frozenset(_METERED_LANE_ALIASES)
_COST_FIELDS = frozenset({
    "budget_id", "cost_center", "currency", "estimated_cost", "unit",
})


def safe_cost_attribution(value: Mapping[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in dict(value or {}).items():
        if key not in _COST_FIELDS or not isinstance(item, (str, int, float)):
            continue
        if isinstance(item, str):
            item = item.strip()[:128]
        elif not math.isfinite(float(item)):
            continue
        result[key] = item
    return result


def _lane_kind(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _deny(reason_code: str, *, lane_kind: str = "metered") -> dict[str, Any]:
    return {
        "allowed": False,
        "state": "policy_blocked",
        "reason_code": reason_code,
        "lane_kind": lane_kind,
    }


def evaluate_metered_lane_policy(
    lane_policy: Mapping[str, Any] | None,
    *,
    active_credential_reference: str,
) -> dict[str, Any]:
    """Require all paid-fallback controls; omission always means disabled."""
    policy = dict(lane_policy or {})
    lane_kind = _lane_kind(policy.get("lane_kind") or "personal_subscription")
    if lane_kind in PERSONAL_LANE_KINDS:
        return {
            "allowed": True,
            "state": "ready",
            "reason_code": "personal_subscription_lane",
            "lane_kind": "personal_subscription",
            "metered": False,
        }
    canonical_metered_kind = _METERED_LANE_ALIASES.get(lane_kind)
    if not canonical_metered_kind:
        return _deny("lane_kind_not_supported", lane_kind="unknown")
    if policy.get("enabled") is not True:
        return _deny("metered_lane_disabled_by_default")
    personal_reference = str(policy.get("personal_credential_reference") or "").strip()
    metered_reference = str(
        policy.get("metered_credential_reference") or active_credential_reference or ""
    ).strip()
    if not personal_reference or not metered_reference or personal_reference == metered_reference:
        return _deny("separate_metered_credential_required")
    if metered_reference != str(active_credential_reference or "").strip():
        return _deny("metered_credential_binding_mismatch")
    opt_in = dict(policy.get("audited_opt_in") or {})
    try:
        approved_at = float(opt_in.get("approved_at") or 0)
    except (TypeError, ValueError):
        approved_at = 0
    if not (opt_in.get("enabled") is True
            and str(opt_in.get("actor") or "").strip()
            and str(opt_in.get("audit_id") or "").strip()
            and math.isfinite(approved_at) and approved_at > 0):
        return _deny("audited_metered_opt_in_required")
    try:
        budget = float(policy.get("budget_ceiling"))
    except (TypeError, ValueError):
        return _deny("metered_budget_ceiling_required")
    if not math.isfinite(budget) or budget <= 0:
        return _deny("metered_budget_ceiling_required")
    attribution = safe_cost_attribution(policy.get("cost_attribution"))
    if not attribution.get("budget_id") or not attribution.get("cost_center") \
            or not attribution.get("currency"):
        return _deny("visible_cost_attribution_required")
    return {
        "allowed": True,
        "state": "ready",
        "reason_code": "metered_lane_explicitly_authorized",
        "lane_kind": canonical_metered_kind,
        "metered": True,
        "budget_ceiling": budget,
        "cost_attribution": attribution,
        "opt_in_audit_id": str(opt_in["audit_id"])[:128],
    }
