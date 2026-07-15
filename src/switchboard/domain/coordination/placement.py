"""Pure hybrid Agent Host placement policy (CO-9).

The coordinator owns *where* a wake may run. Agent Hosts still own process launch,
the provider-capacity repository still owns subscription/account admission, and the
CO fleet still owns EC2 creation and scale-in. Keeping those signals separate avoids
mistaking an idle physical slot for provider entitlement (or vice versa).
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict, deque
from typing import Any, Iterable, Mapping


PLACEMENT_SCHEMA = "switchboard.hybrid_placement_decision.v1"
HOST_PLACEMENT_SCHEMA = "switchboard.agent_host_placement.v1"

_COST_RANK = {
    "already_paid": 0,
    "included": 0,
    "spot": 1,
    "ephemeral_variable": 2,
    "on_demand": 3,
    "unknown": 9,
}


def _strings(value: Any) -> set[str]:
    if isinstance(value, str):
        value = [value]
    return {str(item).strip() for item in (value or []) if str(item).strip()}


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _host_placement(host: Mapping[str, Any]) -> dict[str, Any]:
    """Read the additive placement inventory without requiring a host-table rewrite."""
    direct = host.get("placement")
    capacity = host.get("capacity") or {}
    nested = capacity.get("placement") if isinstance(capacity, Mapping) else None
    return dict(direct or nested or {})


def host_class(host: Mapping[str, Any]) -> str:
    placement = _host_placement(host)
    value = str(placement.get("host_class") or "").strip().lower()
    if value in {"persistent", "ephemeral"}:
        return value
    host_id = str(host.get("host_id") or "")
    return "ephemeral" if host_id.startswith("host/i-") else "persistent"


def _runtime_match(host: Mapping[str, Any], selector: Mapping[str, Any]) -> dict[str, Any] | None:
    want_runtime = str(selector.get("runtime") or "").strip()
    want_lane = str(selector.get("lane") or "").strip()
    want_caps = _strings(selector.get("capabilities"))
    for raw in host.get("runtimes") or []:
        runtime = {"runtime": raw} if isinstance(raw, str) else dict(raw or {})
        have_runtime = str(runtime.get("runtime") or runtime.get("name") or "").strip()
        if want_runtime and have_runtime != want_runtime:
            continue
        lanes = _strings(runtime.get("lanes"))
        if want_lane and lanes and want_lane not in lanes:
            continue
        if want_caps and not want_caps.issubset(_strings(runtime.get("capabilities"))):
            continue
        return runtime
    return None


def _request_error(policy: Mapping[str, Any], project: str) -> str:
    scheduler = dict(policy.get("scheduler") or {})
    if str(scheduler.get("mode") or "") != "hybrid":
        return ""
    binding = dict(policy.get("account_binding") or {})
    if not binding:
        return ""
    required = (
        "tenant_id", "user_id", "provider", "provider_account_id",
        "account_affinity_id", "credential_reference", "task_id",
    )
    if any(not str(binding.get(key) or "").strip() for key in required):
        return "account_binding_incomplete"
    if str(binding.get("project") or "") != project:
        return "account_binding_project_mismatch"
    provider_capacity = policy.get("provider_capacity")
    if (not isinstance(provider_capacity, Mapping)
            or not isinstance(provider_capacity.get("allowed"), bool)):
        return "provider_capacity_admission_missing"
    return ""


def _headroom(host: Mapping[str, Any], reserved_sessions: int) -> tuple[int | None, int, int | None]:
    placement = _host_placement(host)
    concurrency = placement.get("concurrency") or {}
    limits = host.get("limits") or {}
    capacity = host.get("capacity") or {}
    maximum = _number(limits.get("max_sessions"))
    if maximum is None:
        maximum = _number(concurrency.get("max_sessions"))
    active = int(_number(capacity.get("active_sessions")) or 0)
    if maximum is None:
        return None, active, None
    available = max(0, int(maximum) - active - max(0, int(reserved_sessions)))
    return int(maximum), active, available


def evaluate_host(
    host: Mapping[str, Any],
    selector: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    project: str,
    reserved_sessions: int = 0,
    candidate_wake_id: str = "",
) -> dict[str, Any]:
    """Return one redacted, reason-coded host eligibility result."""
    scheduler = dict(policy.get("scheduler") or {})
    strict = str(scheduler.get("mode") or "") == "hybrid"
    placement = _host_placement(host)
    kind = host_class(host)
    reasons: list[str] = []

    if host.get("stale") or str(host.get("status") or "online") != "online":
        reasons.append("host_unavailable")
    runtime = _runtime_match(host, selector)
    if runtime is None:
        reasons.append("runtime_or_capability_mismatch")

    maximum, active, available = _headroom(host, reserved_sessions)
    if available is not None and available <= 0:
        reasons.append("physical_host_capacity_exhausted")

    if strict:
        if placement.get("schema") != HOST_PLACEMENT_SCHEMA:
            reasons.append("placement_inventory_missing")
        if placement.get("wakeable") is not True:
            reasons.append("host_not_wakeable")
        if str(placement.get("drain_state") or "") != "accepting":
            reasons.append("host_draining")
        if runtime is not None and (runtime.get("policy") or {}).get("allow_work") is not True:
            reasons.append("runtime_work_policy_denied")
        if kind == "persistent" and scheduler.get("allow_persistent", True) is not True:
            reasons.append("persistent_capacity_disabled")
        if kind == "ephemeral" and scheduler.get("allow_ephemeral", True) is not True:
            reasons.append("ephemeral_capacity_disabled")
        # Provisioned fleet hosts are single-wake resources.  General placement
        # must not reuse them, while claim-time evaluation must still allow the
        # exact wake that caused the host to be provisioned.
        bound_wake_id = str(placement.get("bound_wake_id") or "").strip()
        evaluated_wake_id = str(candidate_wake_id or "").strip()
        if (kind == "ephemeral" and bound_wake_id
                and bound_wake_id != evaluated_wake_id):
            reasons.append("host_wake_bound")

        projects = _strings(placement.get("projects"))
        if project not in projects:
            reasons.append("project_not_allowed")

        binding = dict(policy.get("account_binding") or {})
        tenant_id = str(binding.get("tenant_id") or "")
        provider = str(binding.get("provider") or "")
        affinity = str(binding.get("account_affinity_id") or "")
        if tenant_id and tenant_id not in _strings(placement.get("tenant_ids")):
            reasons.append("tenant_not_allowed")
        if provider and provider not in _strings(placement.get("providers")):
            reasons.append("provider_not_allowed")
        if affinity and affinity not in _strings(placement.get("account_affinity_ids")):
            reasons.append("provider_account_affinity_mismatch")
        if binding:
            if placement.get("supports_credential_leases") is not True:
                reasons.append("credential_lease_not_supported")

        request = dict(policy.get("placement") or {})
        repository = str(request.get("canonical_repo") or selector.get("canonical_repo") or "")
        if repository and repository not in _strings(placement.get("repositories")):
            reasons.append("repository_not_available")
        session_policy = str(request.get("session_policy") or "")
        if session_policy and session_policy not in _strings(placement.get("session_policies")):
            reasons.append("session_policy_not_supported")
        isolation = str(request.get("isolation") or "")
        if isolation and isolation not in _strings(placement.get("isolation_modes")):
            reasons.append("isolation_policy_not_supported")
        missing_binaries = _strings(request.get("runtime_binaries")) \
            - _strings(placement.get("runtime_binaries"))
        if missing_binaries:
            reasons.append("runtime_binary_missing")

        resources = placement.get("resources") or {}
        requirements = request.get("resources") or {}
        for requested_key, available_key in (
            ("cpu", "cpu_available"),
            ("memory_mb", "memory_mb_available"),
            ("disk_gb", "disk_gb_available"),
        ):
            needed = _number(requirements.get(requested_key))
            have = _number(resources.get(available_key))
            if needed is not None and (have is None or have < needed):
                reasons.append(f"{requested_key}_headroom_insufficient")

        provider_capacity = dict(policy.get("provider_capacity") or {})
        if provider_capacity.get("allowed") is False:
            reasons.append("provider_subscription_capacity_denied")

    return {
        "host_id": host.get("host_id"),
        "host_class": kind,
        "eligible": not reasons,
        "reason_codes": list(dict.fromkeys(reasons)),
        "cost_class": str(placement.get("cost_class") or "unknown"),
        "physical_capacity": {
            "max_sessions": maximum,
            "active_sessions": active,
            "reserved_sessions": max(0, int(reserved_sessions)),
            "available_sessions": available,
        },
    }


def _fair_share_bucket(policy: Mapping[str, Any], project: str) -> str:
    scheduler = dict(policy.get("scheduler") or {})
    explicit = str(scheduler.get("fair_share_key") or "").strip()
    if explicit:
        return explicit[:160]
    binding = dict(policy.get("account_binding") or {})
    tenant = str(binding.get("tenant_id") or "")
    source = f"tenant:{tenant}" if tenant else f"project:{project}"
    return "fair-" + hashlib.sha256(source.encode()).hexdigest()[:16]


def plan_hybrid_placement(
    hosts: Iterable[Mapping[str, Any]],
    selector: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    project: str,
    reserved_by_host: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Choose paid persistent capacity first, then registered/burst ephemeral capacity."""
    scheduler = dict(policy.get("scheduler") or {})
    hybrid = str(scheduler.get("mode") or "") == "hybrid"
    reserved_by_host = dict(reserved_by_host or {})
    candidates = [
        evaluate_host(
            host, selector, policy, project=project,
            reserved_sessions=int(reserved_by_host.get(str(host.get("host_id") or ""), 0)),
        )
        for host in hosts
    ]
    eligible = [candidate for candidate in candidates if candidate["eligible"]]

    def rank(candidate: Mapping[str, Any]) -> tuple[int, int, int, str]:
        kind_rank = 0 if candidate["host_class"] == "persistent" else 1
        if scheduler.get("prefer_persistent", True) is not True:
            kind_rank = 0
        cost_rank = _COST_RANK.get(str(candidate.get("cost_class") or "unknown"), 8)
        available = (candidate.get("physical_capacity") or {}).get("available_sessions")
        return (kind_rank, cost_rank, -int(available or 0), str(candidate.get("host_id") or ""))

    eligible.sort(key=rank)
    chosen = eligible[0] if eligible else None
    request_error = _request_error(policy, project)
    provider_capacity = dict(policy.get("provider_capacity") or {})
    action = "wait"
    reason_code = "no_eligible_host"
    if request_error:
        action, reason_code = "deny", request_error
        chosen = None
    elif provider_capacity.get("allowed") is False:
        action, reason_code = "deny", "provider_subscription_capacity_denied"
        chosen = None
    elif chosen:
        action = f"assign_{chosen['host_class']}"
        reason_code = f"{chosen['host_class']}_capacity_available"
    elif (hybrid and scheduler.get("allow_ephemeral", True) is True
          and scheduler.get("burst_enabled", True) is True
          and str(policy.get("mode") or "") == "co_fleet"):
        action = "provision_ephemeral"
        persistent = [item for item in candidates if item["host_class"] == "persistent"]
        saturated = bool(persistent) and all(
            "physical_host_capacity_exhausted" in item["reason_codes"] for item in persistent
        )
        reason_code = (
            "persistent_capacity_saturated" if saturated
            else "no_eligible_persistent_capacity"
        )

    safe_request = {
        "project": project,
        "runtime": selector.get("runtime"),
        "lane": selector.get("lane"),
        "capabilities": sorted(_strings(selector.get("capabilities"))),
        "has_account_binding": bool(policy.get("account_binding")),
        "placement": policy.get("placement") or {},
    }
    return {
        "schema": PLACEMENT_SCHEMA,
        "scheduler_mode": "hybrid" if hybrid else "legacy",
        "action": action,
        "reason_code": reason_code,
        "selected_host_id": chosen.get("host_id") if chosen else None,
        "selected_host_class": chosen.get("host_class") if chosen else None,
        "cost_class": chosen.get("cost_class") if chosen else (
            "spot_first" if action == "provision_ephemeral" else None
        ),
        "fair_share_bucket": _fair_share_bucket(policy, project),
        "request_digest": _digest(safe_request),
        "provider_capacity": {
            "allowed": provider_capacity.get("allowed"),
            "state": provider_capacity.get("state") or "independent_not_observed",
            "reason_code": provider_capacity.get("reason_code"),
        },
        "candidate_count": len(candidates),
        "eligible_host_count": len(eligible),
        "candidates": candidates,
    }


def claim_decision(
    host: Mapping[str, Any],
    wake: Mapping[str, Any],
    *,
    project: str,
    credential_rebound: bool = False,
) -> dict[str, Any]:
    """Validate a claimant against the persisted placement and current inventory."""
    policy = dict(wake.get("policy") or {})
    placement = dict(wake.get("placement") or {})
    wake_id = str(wake.get("wake_id") or "").strip()
    if placement.get("scheduler_mode") != "hybrid":
        candidate = evaluate_host(
            host, wake.get("selector") or {}, {}, project=project,
            candidate_wake_id=wake_id,
        )
        return {"allowed": candidate["eligible"], "candidate": candidate}

    candidate = evaluate_host(
        host, wake.get("selector") or {}, policy, project=project, reserved_sessions=0,
        candidate_wake_id=wake_id,
    )
    expected = str(placement.get("selected_host_id") or "")
    action = str(placement.get("action") or "")
    reasons = list(candidate["reason_codes"])
    if expected and str(host.get("host_id") or "") != expected:
        reasons.append("different_host_selected")
    if action == "provision_ephemeral" and candidate["host_class"] != "ephemeral":
        reasons.append("ephemeral_claim_required")
    if placement.get("credential_rebind_required") and not credential_rebound:
        reasons.append("credential_rebind_required_after_host_loss")
    candidate["reason_codes"] = list(dict.fromkeys(reasons))
    candidate["eligible"] = not candidate["reason_codes"]
    return {"allowed": candidate["eligible"], "candidate": candidate}


def order_wakes_fairly(wakes: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Round-robin hybrid wakes by tenant/project bucket while preserving bucket FIFO."""
    rows = [dict(wake) for wake in wakes]
    hybrid = [wake for wake in rows
              if ((wake.get("placement") or {}).get("scheduler_mode") == "hybrid")]
    legacy = [wake for wake in rows
              if ((wake.get("placement") or {}).get("scheduler_mode") != "hybrid")]
    if len(hybrid) < 2:
        return rows

    buckets: OrderedDict[str, deque[dict[str, Any]]] = OrderedDict()
    for wake in hybrid:
        key = str((wake.get("placement") or {}).get("fair_share_bucket") or "unscoped")
        buckets.setdefault(key, deque()).append(wake)
    ordered: list[dict[str, Any]] = []
    while buckets:
        for key in list(buckets):
            queue = buckets[key]
            ordered.append(queue.popleft())
            if not queue:
                del buckets[key]
    # Already-queued pre-CO-9 work remains FIFO and drains before the new fair-share queue.
    return legacy + ordered
