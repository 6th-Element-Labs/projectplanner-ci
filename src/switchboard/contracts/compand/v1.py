"""Content-free wire and evidence contracts for the Compand pilot gateway."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator


GatewayMode = Literal["passthrough", "scan"]
EgressClassification = Literal["captured", "bypassed", "excluded", "unknown"]
CoverageStatus = Literal["full", "partial", "control_only", "unsupported", "unknown"]
ScanDecisionKind = Literal["advance", "low_coverage_hold", "redesign", "stop"]
EvidenceState = Literal[
    "exploratory",
    "provisional",
    "verified",
    "independently_reproduced",
    "suspended",
]
ScanClaimLimit = Literal[
    "diagnostic_only",
    "named_scan_mechanism_only",
    "provisional_c2_named_corpus_only",
    "production_roi",
    "broad_product_savings",
]


_SHA256_EVIDENCE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_LINE_RLE_SOURCE_BYTES = 1_048_576
_PARITY_FIELDS = (
    "protocol",
    "usage_fields",
    "task_result",
    "streaming",
    "tools",
    "errors",
    "cancellation",
)


class _FrozenContract(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        populate_by_name=True,
        extra="forbid",
        allow_inf_nan=False,
    )


class GatewayErrorDetail(_FrozenContract):
    """OpenAI-shaped error details for failures owned by the gateway."""

    message: str
    type: Literal["compand_gateway_error"] = "compand_gateway_error"
    code: str
    classification: str
    correlation_id: str
    cause: str | None = None


class GatewayErrorEnvelope(_FrozenContract):
    """A typed gateway error that cannot be confused with an upstream body."""

    schema_id: Literal["compand.gateway_error.v1"] = Field(
        default="compand.gateway_error.v1", alias="schema"
    )
    error: GatewayErrorDetail


class ScanObservation(_FrozenContract):
    """Non-recoverable request-shape facts emitted by shadow-only Scan mode."""

    schema_id: Literal["compand.scan_observation.v1"] = Field(
        default="compand.scan_observation.v1", alias="schema"
    )
    json_kind: Literal["object", "array", "scalar", "not_json"]
    input_item_count: int | None = None
    tool_count: int | None = None
    stream_requested: bool | None = None
    continuation_kind: Literal[
        "manual_history", "previous_response_id", "conversation", "none", "unknown"
    ] = "unknown"


class GatewayTelemetry(_FrozenContract):
    """Redacted operational metadata; content, headers, and hashes are absent by design."""

    schema_id: Literal["compand.gateway_telemetry.v1"] = Field(
        default="compand.gateway_telemetry.v1", alias="schema"
    )
    correlation_id: str
    method: str
    endpoint: str
    mode: GatewayMode
    outcome: Literal["rejected", "completed", "cancelled", "transport_failed"]
    classification: str
    status_code: int
    request_bytes: int
    response_bytes: int
    elapsed_ms: float
    credential_id: str | None = None
    scan: ScanObservation | None = None


class EgressObservation(_FrozenContract):
    """One hook input for DOGFOOD-32 process-level coverage classification."""

    schema_id: Literal["compand.egress_observation.v1"] = Field(
        default="compand.egress_observation.v1", alias="schema"
    )
    correlation_id: str
    client: Literal["codex"] = "codex"
    adapter: Literal["openai-responses/v1"] = "openai-responses/v1"
    method: str
    endpoint: str
    classification: EgressClassification
    reason_code: str


class GatewayCoverageReceiptInput(_FrozenContract):
    """Per-request, content-free inputs used to assemble gateway_coverage_receipt.v1."""

    schema_id: Literal["gateway_coverage_receipt.input.v1"] = Field(
        default="gateway_coverage_receipt.input.v1", alias="schema"
    )
    correlation_id: str
    client: Literal["codex"] = "codex"
    client_version: str
    auth_lane: Literal["custom_api_provider"] = "custom_api_provider"
    adapter: Literal["openai-responses/v1"] = "openai-responses/v1"
    mode: GatewayMode
    certified_feature: Literal["models", "responses", "input_tokens"]
    tuple_status: Literal["certified", "unknown"]
    observed_endpoint: str
    egress_classification: EgressClassification
    source_version: str


class CompandSystemSnapshot(_FrozenContract):
    """The exact client/provider/gateway tuple evaluated by one Scan run."""

    schema_id: Literal["compand.system_snapshot.v1"] = Field(
        default="compand.system_snapshot.v1", alias="schema"
    )
    client: Literal["codex"] = "codex"
    client_version: str
    client_binary_sha256: str
    os_arch: str
    model: str
    provider_id: str
    provider_name: str
    provider_base_url: str
    credential_environment_variable: str
    auth_lane: Literal["custom_api_provider"] = "custom_api_provider"
    adapter: Literal["openai-responses/v1"] = "openai-responses/v1"
    wire_api: Literal["responses"] = "responses"
    reasoning_effort: str
    request_max_retries: StrictInt = Field(ge=0)
    stream_max_retries: StrictInt = Field(ge=0)
    stream_idle_timeout_ms: StrictInt = Field(gt=0)
    gateway_version: str
    task_snapshot_sha256: str
    configuration_sha256: str


class EgressObservationWindow(_FrozenContract):
    """Content-free process-level network observation provenance."""

    schema_id: Literal["compand.egress_observation_window.v1"] = Field(
        default="compand.egress_observation_window.v1", alias="schema"
    )
    method: Literal[
        "process_network_capture", "process_socket_audit", "fixture_loopback"
    ]
    window_started_at: datetime
    window_ended_at: datetime
    observer_version: str
    ancillary_destination_classes: tuple[str, ...] = ()


class DirectGatewayParity(_FrozenContract):
    """Direct-versus-gateway result matrix for the frozen run tuple."""

    schema_id: Literal["compand.direct_gateway_parity.v1"] = Field(
        default="compand.direct_gateway_parity.v1", alias="schema"
    )
    protocol: StrictBool
    usage_fields: StrictBool
    task_result: StrictBool
    streaming: StrictBool
    tools: StrictBool
    errors: StrictBool
    cancellation: StrictBool


class CoverageCounts(_FrozenContract):
    captured: StrictInt = Field(ge=0)
    bypassed: StrictInt = Field(ge=0)
    excluded: StrictInt = Field(ge=0)
    unknown: StrictInt = Field(ge=0)
    total: StrictInt = Field(ge=0)

    def validate_primitives(self) -> None:
        """Reject forged aggregate counts, including validation-bypassed objects."""

        values: dict[str, int] = {}
        for field in ("captured", "bypassed", "excluded", "unknown", "total"):
            value = getattr(self, field, None)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"coverage_counts.{field} is not a non-negative integer")
            values[field] = value
        expected_total = sum(
            values[field] for field in ("captured", "bypassed", "excluded", "unknown")
        )
        if values["total"] != expected_total:
            raise ValueError(
                "coverage_counts.total must equal captured+bypassed+excluded+unknown"
            )

    @model_validator(mode="after")
    def validate_total(self) -> "CoverageCounts":
        self.validate_primitives()
        return self


class GatewayCoverageReceipt(_FrozenContract):
    """Immutable, content-free insertion and coverage evidence."""

    schema_id: Literal["gateway_coverage_receipt.v1"] = Field(
        default="gateway_coverage_receipt.v1", alias="schema"
    )
    system: CompandSystemSnapshot
    modes_exercised: tuple[GatewayMode, ...]
    certified_features: tuple[str, ...]
    observed_endpoints: tuple[str, ...]
    egress_observation: EgressObservationWindow
    coverage_counts: CoverageCounts
    coverage: CoverageStatus
    direct_inference_egress_observed: StrictBool
    parity: DirectGatewayParity
    mutation_blocked: StrictBool
    blocking_reasons: tuple[str, ...]
    evidence_hash: str

    @staticmethod
    def compute_evidence_hash(payload: dict[str, object]) -> str:
        """Hash the canonical receipt payload without trusting a supplied hash."""

        canonical = dict(payload)
        canonical.pop("evidence_hash", None)
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return f"sha256:{digest}"

    def recomputed_evidence_hash(self) -> str:
        return self.compute_evidence_hash(
            self.model_dump(mode="json", by_alias=True, exclude={"evidence_hash"})
        )

    def expected_coverage(self) -> CoverageStatus:
        """Derive aggregate coverage solely from primitive request counts."""

        self.coverage_counts.validate_primitives()
        if self.coverage_counts.unknown:
            return "unknown"
        if self.coverage_counts.bypassed:
            return "partial"
        if self.coverage_counts.captured:
            return "full"
        if self.coverage_counts.excluded:
            return "control_only"
        return "unsupported"

    def required_blocking_reasons(self) -> set[str]:
        """Regenerate minimum fail-closed reasons from receipt primitives."""

        self.coverage_counts.validate_primitives()
        reasons: set[str] = set()
        if self.coverage_counts.total == 0:
            reasons.add("no_observed_in_scope_requests")
        if self.coverage_counts.bypassed:
            reasons.add("unexplained_bypass")
        if self.coverage_counts.unknown:
            reasons.add("unreconciled_egress_observation")
        if self.egress_observation.method == "fixture_loopback":
            reasons.add("process_level_egress_observation_missing")
        for field in _PARITY_FIELDS:
            value = getattr(self.parity, field, None)
            if not isinstance(value, bool):
                raise ValueError(f"parity.{field} is not a boolean")
            if value is False:
                reasons.add(f"direct_gateway_parity_failed:{field}")
        if self.coverage_counts.captured <= 0:
            reasons.add("no_captured_inference_requests")
        if (
            "responses" not in self.certified_features
            or "/v1/responses" not in self.observed_endpoints
        ):
            reasons.add("captured_responses_route_missing")
        return reasons

    def validate_derived_truth(self) -> None:
        """Cross-check every caller-supplied aggregate and its canonical hash."""

        expected_coverage = self.expected_coverage()
        if self.coverage != expected_coverage:
            raise ValueError(
                f"coverage mismatch: expected {expected_coverage}, got {self.coverage}"
            )
        expected_direct_egress = bool(self.coverage_counts.bypassed)
        if self.direct_inference_egress_observed is not expected_direct_egress:
            raise ValueError(
                "direct_inference_egress_observed does not match bypassed count"
            )
        if not isinstance(self.blocking_reasons, tuple):
            raise ValueError("blocking_reasons must be a canonical tuple")
        canonical_reasons = tuple(sorted(set(self.blocking_reasons)))
        if self.blocking_reasons != canonical_reasons:
            raise ValueError("blocking_reasons must be sorted and unique")
        missing_reasons = self.required_blocking_reasons() - set(
            self.blocking_reasons
        )
        if missing_reasons:
            raise ValueError(
                "blocking_reasons omit derived reasons: "
                + ", ".join(sorted(missing_reasons))
            )
        if self.mutation_blocked is not bool(self.blocking_reasons):
            raise ValueError("mutation_blocked does not match blocking_reasons")
        if not isinstance(self.evidence_hash, str) or not _SHA256_EVIDENCE.fullmatch(
            self.evidence_hash
        ):
            raise ValueError("evidence_hash is not canonical sha256 evidence")
        expected_hash = self.recomputed_evidence_hash()
        if self.evidence_hash != expected_hash:
            raise ValueError("evidence_hash does not match canonical receipt evidence")

    @model_validator(mode="after")
    def validate_receipt_truth(self) -> "GatewayCoverageReceipt":
        self.validate_derived_truth()
        return self


class ProviderPriceTable(_FrozenContract):
    """Dated provider input prices used only for projected Scan economics."""

    schema_id: Literal["compand.provider_price_table.v1"] = Field(
        default="compand.provider_price_table.v1", alias="schema"
    )
    provider: str
    model: str
    effective_date: date
    currency: Literal["USD"] = "USD"
    input_usd_per_million_tokens: float = Field(ge=0)
    cached_input_usd_per_million_tokens: float = Field(ge=0)
    source: str


class ProviderTokenCount(_FrozenContract):
    """Provider-authoritative count for one original or candidate payload."""

    schema_id: Literal["compand.provider_token_count.v1"] = Field(
        default="compand.provider_token_count.v1", alias="schema"
    )
    input_tokens: StrictInt = Field(ge=0)
    cached_input_tokens: StrictInt | None = Field(default=None, ge=0)
    count_call_latency_ms: float = Field(ge=0)
    retry_count: StrictInt = Field(default=0, ge=0)
    source: Literal["provider_input_tokens"] = "provider_input_tokens"


class LineRleShadowMeasurement(_FrozenContract):
    """Content-free economics from one shadow-only line-rle-v1 candidate."""

    schema_id: Literal["compand.line_rle_shadow_measurement.v1"] = Field(
        default="compand.line_rle_shadow_measurement.v1", alias="schema"
    )
    technique: Literal["line-rle-v1"] = "line-rle-v1"
    run_id: str
    task_snapshot_sha256: str
    source_artifact_sha256: str
    candidate_artifact_sha256: str
    repeated_span_count: StrictInt = Field(ge=0)
    repeated_line_count: StrictInt = Field(ge=0)
    removed_line_count: StrictInt = Field(ge=0)
    original_bytes: StrictInt = Field(ge=0)
    candidate_bytes: StrictInt = Field(ge=0)
    original_count: ProviderTokenCount
    candidate_count: ProviderTokenCount
    cache_fields_exposed: StrictBool
    projected_original_input_usd: float = Field(ge=0)
    projected_candidate_input_usd: float = Field(ge=0)
    projected_input_savings_usd: float
    cache_adjusted_candidate_is_cheaper: StrictBool
    gateway_latency_ms: float = Field(ge=0)
    gateway_retry_count: StrictInt = Field(ge=0)
    task_completed: StrictBool
    shadow_original_forwarded_byte_for_byte: StrictBool
    price_table: ProviderPriceTable

    def validate_structural_primitives(self) -> None:
        """Revalidate the content-free attestation emitted by the trusted builder."""

        for field in (
            "task_snapshot_sha256",
            "source_artifact_sha256",
            "candidate_artifact_sha256",
        ):
            value = getattr(self, field, None)
            if not isinstance(value, str) or not _SHA256_EVIDENCE.fullmatch(value):
                raise ValueError(f"{field} is not canonical sha256 evidence")
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id is required")

        primitives = {
            "repeated_span_count": self.repeated_span_count,
            "repeated_line_count": self.repeated_line_count,
            "removed_line_count": self.removed_line_count,
            "original_bytes": self.original_bytes,
            "candidate_bytes": self.candidate_bytes,
            "gateway_retry_count": self.gateway_retry_count,
        }
        for field, value in primitives.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} is not a valid non-negative integer")
        for field in (
            "cache_fields_exposed",
            "cache_adjusted_candidate_is_cheaper",
            "task_completed",
            "shadow_original_forwarded_byte_for_byte",
        ):
            if not isinstance(getattr(self, field, None), bool):
                raise ValueError(f"{field} is not a boolean")
        if self.original_bytes == 0 or self.candidate_bytes == 0:
            raise ValueError("line-rle-v1 artifact evidence must be non-empty")
        if self.original_bytes > _MAX_LINE_RLE_SOURCE_BYTES:
            raise ValueError("line-rle-v1 source artifact exceeds the byte limit")

        if self.repeated_span_count == 0:
            if self.repeated_line_count or self.removed_line_count:
                raise ValueError(
                    "zero repeated spans require zero repeated and removed lines"
                )
            return
        if self.repeated_line_count < self.repeated_span_count * 2:
            raise ValueError(
                "each repeated span must attest at least two repeated lines"
            )
        if self.removed_line_count != (
            self.repeated_line_count - self.repeated_span_count
        ):
            raise ValueError(
                "removed_line_count must equal repeated lines minus repeated spans"
            )
        if self.source_artifact_sha256 == self.candidate_artifact_sha256:
            raise ValueError(
                "a repeated-span candidate must differ from its source artifact"
            )

    def recomputed_derived_values(self) -> dict[str, float | bool]:
        """Regenerate every derived economics field from primitive evidence."""

        self.validate_structural_primitives()
        _validate_provider_token_count("original_count", self.original_count)
        _validate_provider_token_count("candidate_count", self.candidate_count)
        original_cost = _projected_input_cost(self.original_count, self.price_table)
        candidate_cost = _projected_input_cost(self.candidate_count, self.price_table)
        for label, cost in (
            ("projected_original_input_usd", original_cost),
            ("projected_candidate_input_usd", candidate_cost),
        ):
            if not math.isfinite(cost) or cost < 0:
                raise ValueError(f"{label} is not a valid non-negative cost")
        savings = round(original_cost - candidate_cost, 12)
        if not math.isfinite(savings):
            raise ValueError("projected_input_savings_usd is not finite")
        cache_exposed = (
            self.original_count.cached_input_tokens is not None
            and self.candidate_count.cached_input_tokens is not None
        )
        cheaper = bool(
            self.repeated_span_count
            and cache_exposed
            and savings > 0
            and self.task_completed
            and self.shadow_original_forwarded_byte_for_byte
        )
        return {
            "cache_fields_exposed": cache_exposed,
            "projected_original_input_usd": original_cost,
            "projected_candidate_input_usd": candidate_cost,
            "projected_input_savings_usd": savings,
            "cache_adjusted_candidate_is_cheaper": cheaper,
        }

    @model_validator(mode="after")
    def validate_derived_economics(self) -> "LineRleShadowMeasurement":
        """Reject evidence whose published economics do not match its primitives."""

        expected = self.recomputed_derived_values()
        mismatches: list[str] = []
        for field in (
            "projected_original_input_usd",
            "projected_candidate_input_usd",
            "projected_input_savings_usd",
        ):
            if not math.isclose(
                float(getattr(self, field)),
                float(expected[field]),
                rel_tol=0.0,
                abs_tol=5e-13,
            ):
                mismatches.append(field)
        for field in (
            "cache_fields_exposed",
            "cache_adjusted_candidate_is_cheaper",
        ):
            if getattr(self, field) is not expected[field]:
                mismatches.append(field)
        if mismatches:
            raise ValueError(
                "derived economics mismatch: " + ", ".join(sorted(mismatches))
            )
        return self


class CompandScanDecision(_FrozenContract):
    """Bounded DOGFOOD-32 decision; it never enables mutation by itself."""

    schema_id: Literal["compand.scan_decision.v1"] = Field(
        default="compand.scan_decision.v1", alias="schema"
    )
    technique: Literal["line-rle-v1"] = "line-rle-v1"
    decision: ScanDecisionKind
    reasons: tuple[str, ...]
    measured_candidate_count: StrictInt = Field(ge=0)
    qualifying_candidate_count: StrictInt = Field(ge=0)
    mutation_authorized: StrictBool = False

    @model_validator(mode="after")
    def validate_mutation_authority(self) -> "CompandScanDecision":
        if self.mutation_authorized is not False:
            raise ValueError("DOGFOOD-32 scan evidence cannot authorize mutation")
        return self


class CompandScanEvidenceAuthority(_FrozenContract):
    """Typed caller assertion that must equal the compiler-derived ceiling."""

    evidence_state: EvidenceState
    claim_limit: ScanClaimLimit


class CompandScanEvidenceBundle(_FrozenContract):
    """Immutable Scan bundle whose claim authority is derived from its evidence."""

    schema_id: Literal["compand.scan_evidence_bundle.v1"] = Field(
        default="compand.scan_evidence_bundle.v1", alias="schema"
    )
    ces_phase: Literal["phase_1_scan"] = "phase_1_scan"
    evidence_state: EvidenceState
    claim_limit: ScanClaimLimit
    source_input_sha256: str
    coverage_receipt: GatewayCoverageReceipt
    measurements: tuple[LineRleShadowMeasurement, ...]
    decision: CompandScanDecision
    limitations: tuple[str, ...] = ()

    @staticmethod
    def derive_authority(
        coverage: GatewayCoverageReceipt,
        measurements: tuple[LineRleShadowMeasurement, ...],
        decision: CompandScanDecision,
    ) -> CompandScanEvidenceAuthority:
        """Apply the conservative ADR-0026/CES-1 Phase 1 Scan ceiling."""

        try:
            coverage.validate_derived_truth()
        except (AttributeError, TypeError, ValueError):
            return CompandScanEvidenceAuthority(
                evidence_state="exploratory",
                claim_limit="diagnostic_only",
            )

        qualifying_measurements = 0
        for item in measurements:
            try:
                derived = item.recomputed_derived_values()
            except (TypeError, ValueError):
                continue
            provider = item.price_table.provider.strip().casefold()
            expected_providers = {
                coverage.system.provider_id.strip().casefold(),
                coverage.system.provider_name.strip().casefold(),
            }
            if (
                derived["cache_adjusted_candidate_is_cheaper"] is True
                and item.task_snapshot_sha256
                == coverage.system.task_snapshot_sha256
                and item.price_table.model == coverage.system.model
                and provider in expected_providers
            ):
                qualifying_measurements += 1

        parity_passes = all(
            getattr(coverage.parity, field, None) is True
            for field in (
                "protocol",
                "usage_fields",
                "task_result",
                "streaming",
                "tools",
                "errors",
                "cancellation",
            )
        )
        process_level_observation = coverage.egress_observation.method in {
            "process_network_capture",
            "process_socket_audit",
        }
        supported_advance = (
            decision.decision == "advance"
            and decision.mutation_authorized is False
            and decision.measured_candidate_count == len(measurements)
            and decision.qualifying_candidate_count == qualifying_measurements
            and qualifying_measurements > 0
            and process_level_observation
            and parity_passes
            and coverage.coverage == "full"
            and coverage.coverage_counts.captured > 0
            and coverage.coverage_counts.bypassed == 0
            and coverage.coverage_counts.unknown == 0
            and coverage.mutation_blocked is False
            and not coverage.blocking_reasons
        )
        if supported_advance:
            # Scan lacks the frozen paired release and clean-environment reproduction
            # required for provisional C2 or verified CES-1 evidence.
            return CompandScanEvidenceAuthority(
                evidence_state="exploratory",
                claim_limit="named_scan_mechanism_only",
            )
        return CompandScanEvidenceAuthority(
            evidence_state="exploratory",
            claim_limit="diagnostic_only",
        )

    @model_validator(mode="after")
    def validate_claim_ceiling(self) -> "CompandScanEvidenceBundle":
        if not _SHA256_EVIDENCE.fullmatch(self.source_input_sha256):
            raise ValueError("source_input_sha256 is not canonical sha256 evidence")
        derived = self.derive_authority(
            self.coverage_receipt,
            self.measurements,
            self.decision,
        )
        if (
            self.evidence_state != derived.evidence_state
            or self.claim_limit != derived.claim_limit
        ):
            raise ValueError(
                "evidence authority exceeds the derived ADR-0026/CES-1 ceiling: "
                f"expected {derived.evidence_state}/{derived.claim_limit}"
            )
        return self


def _validate_provider_token_count(label: str, count: ProviderTokenCount) -> None:
    """Validate count primitives even when callers bypass Pydantic construction."""

    input_tokens = getattr(count, "input_tokens", None)
    cached_tokens = getattr(count, "cached_input_tokens", None)
    retry_count = getattr(count, "retry_count", None)
    latency = getattr(count, "count_call_latency_ms", None)
    for field, value in (
        ("input_tokens", input_tokens),
        ("retry_count", retry_count),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{label}.{field} is not a valid non-negative integer")
    if cached_tokens is not None and (
        isinstance(cached_tokens, bool)
        or not isinstance(cached_tokens, int)
        or cached_tokens < 0
    ):
        raise ValueError(
            f"{label}.cached_input_tokens is not a valid non-negative integer"
        )
    if cached_tokens is not None and cached_tokens > input_tokens:
        raise ValueError(f"{label}.cached_input_tokens exceeds input_tokens")
    if (
        isinstance(latency, bool)
        or not isinstance(latency, (int, float))
        or not math.isfinite(float(latency))
        or latency < 0
    ):
        raise ValueError(
            f"{label}.count_call_latency_ms is not a valid non-negative number"
        )


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
