"""Compile content-free Compand coverage and shadow-economics evidence."""

from __future__ import annotations

from collections.abc import Iterable

from switchboard.contracts.compand import (
    CompandScanDecision,
    CompandSystemSnapshot,
    DirectGatewayParity,
    EgressObservation,
    EgressObservationWindow,
    GatewayCoverageReceipt,
    GatewayCoverageReceiptInput,
    LineRleShadowMeasurement,
    ProviderPriceTable,
    ProviderTokenCount,
    recompute_compand_scan_decision,
)
from switchboard.domain.compand import LineRleCandidate


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

    return GatewayCoverageReceipt.from_primitives(
        system=system,
        observation_window=observation_window,
        parity=parity,
        coverage_inputs=coverage_inputs,
        egress_observations=egress_observations,
        exercised_features=exercised_features,
    )


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
    savings = round(original_cost - candidate_cost, 12)
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

    return recompute_compand_scan_decision(coverage, tuple(measurements))


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
