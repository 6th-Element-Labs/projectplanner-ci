"""Content-free wire and evidence contracts for the Compand pilot gateway."""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


GatewayMode = Literal["passthrough", "scan"]
EgressClassification = Literal["captured", "bypassed", "excluded", "unknown"]
CoverageStatus = Literal["full", "partial", "control_only", "unsupported", "unknown"]
ScanDecisionKind = Literal["advance", "low_coverage_hold", "redesign", "stop"]


_SHA256_EVIDENCE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_LINE_RLE_SOURCE_BYTES = 1_048_576


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
    protocol: bool
    usage_fields: bool
    task_result: bool
    streaming: bool
    tools: bool
    errors: bool
    cancellation: bool


class CoverageCounts(_FrozenContract):
    captured: StrictInt = Field(ge=0)
    bypassed: StrictInt = Field(ge=0)
    excluded: StrictInt = Field(ge=0)
    unknown: StrictInt = Field(ge=0)
    total: StrictInt = Field(ge=0)


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
    direct_inference_egress_observed: bool
    parity: DirectGatewayParity
    mutation_blocked: bool
    blocking_reasons: tuple[str, ...]
    evidence_hash: str


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
    cache_fields_exposed: bool
    projected_original_input_usd: float = Field(ge=0)
    projected_candidate_input_usd: float = Field(ge=0)
    projected_input_savings_usd: float
    cache_adjusted_candidate_is_cheaper: bool
    gateway_latency_ms: float = Field(ge=0)
    gateway_retry_count: StrictInt = Field(ge=0)
    task_completed: bool
    shadow_original_forwarded_byte_for_byte: bool
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
    mutation_authorized: Literal[False] = False


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
