"""Content-free wire and evidence contracts for the Compand pilot gateway."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


GatewayMode = Literal["passthrough", "scan"]
EgressClassification = Literal["captured", "bypassed", "excluded", "unknown"]


class _FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")


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
