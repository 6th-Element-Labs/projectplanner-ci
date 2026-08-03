"""Compile content-free Compand coverage and shadow-economics evidence."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable

from switchboard.contracts.compand import (
    CompandScanDecision,
    CompandSystemSnapshot,
    CoverageCounts,
    DirectGatewayParity,
    EgressObservation,
    EgressObservationWindow,
    GatewayCoverageReceipt,
    GatewayCoverageReceiptInput,
    LineRleShadowMeasurement,
    ProviderPriceTable,
    ProviderTokenCount,
)
from switchboard.domain.compand import LineRleCandidate


_PARITY_FIELDS = (
    "protocol",
    "usage_fields",
    "task_result",
    "streaming",
    "tools",
    "errors",
    "cancellation",
)
_FEATURE_PARITY_GATE = {
    "responses": "protocol",
    "models": "protocol",
    "input_tokens": "protocol",
    "sse": "streaming",
    "tools": "tools",
    "usage": "usage_fields",
    "task_result": "task_result",
    "errors": "errors",
    "cancellation": "cancellation",
}


def compile_gateway_coverage_receipt(
    *,
    system: CompandSystemSnapshot,
    observation_window: EgressObservationWindow,
    parity: DirectGatewayParity,
    coverage_inputs: Iterable[GatewayCoverageReceiptInput],
    egress_observations: Iterable[EgressObservation],
    exercised_features: Iterable[str] = (),
) -> GatewayCoverageReceipt:
    """Reconcile gateway and process observations without optimistic inference."""

    if observation_window.window_ended_at < observation_window.window_started_at:
        raise ValueError("egress observation window must end after it starts")
    inputs = _unique_by_correlation(coverage_inputs, "gateway coverage input")
    egress = _unique_by_correlation(egress_observations, "egress observation")
    counts: Counter[str] = Counter()
    reasons: set[str] = set()

    for correlation_id in sorted(set(inputs) | set(egress)):
        gateway_event = inputs.get(correlation_id)
        process_event = egress.get(correlation_id)
        classification = _reconciled_classification(gateway_event, process_event)
        counts[classification] += 1
        if classification == "bypassed":
            reasons.add("unexplained_bypass")
        if classification == "unknown":
            reasons.add("unreconciled_egress_observation")
        if gateway_event is not None:
            if gateway_event.tuple_status != "certified":
                reasons.add("uncertified_client_tuple")
            if (
                gateway_event.client_version != system.client_version
                or gateway_event.source_version != system.gateway_version
            ):
                reasons.add("system_snapshot_mismatch")

    total = sum(counts.values())
    if total == 0:
        reasons.add("no_observed_in_scope_requests")
    if observation_window.method == "fixture_loopback":
        reasons.add("process_level_egress_observation_missing")
    parity_failures = [field for field in _PARITY_FIELDS if not getattr(parity, field)]
    reasons.update(f"direct_gateway_parity_failed:{field}" for field in parity_failures)

    if counts["unknown"] or "uncertified_client_tuple" in reasons:
        coverage = "unknown"
    elif counts["bypassed"]:
        coverage = "partial"
    elif counts["captured"]:
        coverage = "full"
    elif counts["excluded"]:
        coverage = "control_only"
    else:
        coverage = "unsupported"

    observed_route_features = {
        event.certified_feature
        for event in inputs.values()
        if event.tuple_status == "certified"
        and event.egress_classification in {"captured", "excluded"}
    }
    supplied_features = {
        str(item).strip() for item in exercised_features if str(item).strip()
    }
    unknown_features = supplied_features - set(_FEATURE_PARITY_GATE)
    reasons.update(f"unrecognized_feature_evidence:{item}" for item in unknown_features)
    certified_features = {
        feature
        for feature in observed_route_features | supplied_features
        if feature in _FEATURE_PARITY_GATE
        and getattr(parity, _FEATURE_PARITY_GATE[feature])
    }
    endpoints = {
        event.observed_endpoint for event in inputs.values() if event.observed_endpoint
    }
    modes = tuple(sorted({event.mode for event in inputs.values()}))
    blocking_reasons = tuple(sorted(reasons))
    receipt = GatewayCoverageReceipt(
        system=system,
        modes_exercised=modes,
        certified_features=tuple(sorted(certified_features)),
        observed_endpoints=tuple(sorted(endpoints)),
        egress_observation=observation_window,
        coverage_counts=CoverageCounts(
            captured=counts["captured"],
            bypassed=counts["bypassed"],
            excluded=counts["excluded"],
            unknown=counts["unknown"],
            total=total,
        ),
        coverage=coverage,
        direct_inference_egress_observed=bool(counts["bypassed"]),
        parity=parity,
        mutation_blocked=bool(blocking_reasons),
        blocking_reasons=blocking_reasons,
        evidence_hash="",
    )
    canonical = receipt.model_dump(mode="json", by_alias=True)
    canonical.pop("evidence_hash", None)
    evidence_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return receipt.model_copy(update={"evidence_hash": f"sha256:{evidence_hash}"})


def measure_line_rle_candidate(
    candidate: LineRleCandidate,
    *,
    run_id: str,
    task_snapshot_sha256: str,
    original_count: ProviderTokenCount,
    candidate_count: ProviderTokenCount,
    price_table: ProviderPriceTable,
    gateway_latency_ms: float,
    gateway_retry_count: int,
    task_completed: bool,
    shadow_original_forwarded_byte_for_byte: bool,
) -> LineRleShadowMeasurement:
    """Calculate cache-aware projected provider input cost for one candidate."""

    _validate_cached_count(original_count)
    _validate_cached_count(candidate_count)
    cache_exposed = (
        original_count.cached_input_tokens is not None
        and candidate_count.cached_input_tokens is not None
    )
    original_cost = _projected_input_cost(original_count, price_table)
    candidate_cost = _projected_input_cost(candidate_count, price_table)
    savings = original_cost - candidate_cost
    cheaper = bool(
        candidate.repeated_span_count
        and cache_exposed
        and savings > 0
        and task_completed
        and shadow_original_forwarded_byte_for_byte
    )
    return LineRleShadowMeasurement(
        run_id=run_id,
        task_snapshot_sha256=task_snapshot_sha256,
        source_artifact_sha256=candidate.source_artifact_sha256,
        candidate_artifact_sha256=candidate.candidate_artifact_sha256,
        repeated_span_count=candidate.repeated_span_count,
        repeated_line_count=candidate.repeated_line_count,
        removed_line_count=candidate.removed_line_count,
        original_bytes=candidate.original_bytes,
        candidate_bytes=candidate.candidate_bytes,
        original_count=original_count,
        candidate_count=candidate_count,
        cache_fields_exposed=cache_exposed,
        projected_original_input_usd=original_cost,
        projected_candidate_input_usd=candidate_cost,
        projected_input_savings_usd=savings,
        cache_adjusted_candidate_is_cheaper=cheaper,
        gateway_latency_ms=gateway_latency_ms,
        gateway_retry_count=gateway_retry_count,
        task_completed=task_completed,
        shadow_original_forwarded_byte_for_byte=(
            shadow_original_forwarded_byte_for_byte
        ),
        price_table=price_table,
    )


def decide_compand_scan(
    coverage: GatewayCoverageReceipt,
    measurements: Iterable[LineRleShadowMeasurement],
) -> CompandScanDecision:
    """Return the bounded line-rle-v1 decision without enabling mutation."""

    measured = tuple(measurements)
    qualifying = tuple(
        item for item in measured if item.cache_adjusted_candidate_is_cheaper
    )
    parity_failures = [
        field for field in _PARITY_FIELDS if not getattr(coverage.parity, field)
    ]
    snapshot_mismatch = any(
        item.task_snapshot_sha256 != coverage.system.task_snapshot_sha256
        or item.price_table.model != coverage.system.model
        for item in measured
    )
    if parity_failures or snapshot_mismatch or any(
        not item.shadow_original_forwarded_byte_for_byte for item in measured
    ):
        reasons = tuple(
            sorted(
                {*(f"parity_failed:{field}" for field in parity_failures)}
                | (
                    {"measurement_system_snapshot_mismatch"}
                    if snapshot_mismatch
                    else set()
                )
                | (
                    {"shadow_payload_was_not_byte_preserved"}
                    if any(
                        not item.shadow_original_forwarded_byte_for_byte
                        for item in measured
                    )
                    else set()
                )
            )
        )
        decision = "stop"
    elif coverage.mutation_blocked:
        decision = "low_coverage_hold"
        reasons = coverage.blocking_reasons
    elif qualifying:
        decision = "advance"
        reasons = ("cache_adjusted_candidate_is_cheaper",)
    else:
        decision = "redesign"
        reasons = ("no_cache_adjusted_positive_candidate",)
    return CompandScanDecision(
        decision=decision,
        reasons=reasons,
        measured_candidate_count=len(measured),
        qualifying_candidate_count=len(qualifying),
    )


def _unique_by_correlation(events: Iterable[object], label: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for event in events:
        correlation_id = str(getattr(event, "correlation_id", "") or "")
        if not correlation_id:
            raise ValueError(f"{label} requires a correlation_id")
        if correlation_id in result:
            raise ValueError(f"duplicate {label} correlation_id: {correlation_id}")
        result[correlation_id] = event
    return result


def _reconciled_classification(
    gateway_event: GatewayCoverageReceiptInput | None,
    process_event: EgressObservation | None,
) -> str:
    if process_event is None:
        return "unknown"
    if gateway_event is None:
        return (
            process_event.classification
            if process_event.classification in {"bypassed", "excluded"}
            else "unknown"
        )
    if (
        gateway_event.observed_endpoint != process_event.endpoint
        or gateway_event.egress_classification != process_event.classification
    ):
        return "unknown"
    return process_event.classification


def _validate_cached_count(count: ProviderTokenCount) -> None:
    cached = count.cached_input_tokens
    if cached is not None and cached > count.input_tokens:
        raise ValueError("cached input tokens cannot exceed input tokens")


def _projected_input_cost(
    count: ProviderTokenCount, price_table: ProviderPriceTable
) -> float:
    cached = count.cached_input_tokens or 0
    uncached = count.input_tokens - cached
    value = (
        uncached * price_table.input_usd_per_million_tokens
        + cached * price_table.cached_input_usd_per_million_tokens
    ) / 1_000_000
    return round(value, 12)
