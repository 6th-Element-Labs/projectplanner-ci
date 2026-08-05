"""Live Scan/Enforce preparation for one admitted Responses request."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

import httpx

from switchboard.domain.compand import (
    GatewayMode,
    ScanEligibilityError,
    build_line_rle_candidate,
)
from switchboard.storage.repositories.compand import CompandStateRepository


@dataclass(frozen=True)
class PreparedCompandRequest:
    body: bytes
    outcome: str
    transformed: bool
    original_tokens: int | None = None
    candidate_tokens: int | None = None
    original_bytes: int | None = None
    candidate_bytes: int | None = None
    artifact_sha256: str | None = None
    capability: str | None = None


class CompandRuntimeUnavailable(RuntimeError):
    """Durable state is unavailable, so Enforce must fail closed."""


async def prepare_compand_request(
    *,
    mode: GatewayMode,
    body: bytes,
    tenant_id: str,
    session_id: str,
    receipt: Mapping[str, object] | None,
    correlation_id: str,
    upstream_origin: str,
    upstream_api_key: str,
    http_client: httpx.AsyncClient,
    repository: CompandStateRepository,
    retention_seconds: int,
) -> PreparedCompandRequest:
    """Return a frozen provider body; every uncertainty fails open to original bytes."""

    request_sha256 = repository.sha256(body)
    if not session_id:
        return PreparedCompandRequest(body, "session_id_required", False)
    try:
        frozen = repository.get_exchange(tenant_id, session_id, request_sha256)
    except Exception as exc:
        raise CompandRuntimeUnavailable("frozen exchange state is unavailable") from exc
    if frozen is not None:
        return PreparedCompandRequest(
            body=frozen.provider_body,
            outcome="frozen_retry",
            transformed=frozen.transformed,
            artifact_sha256=frozen.artifact_sha256,
            capability=frozen.capability,
        )
    if mode is GatewayMode.PASSTHROUGH:
        return PreparedCompandRequest(body, "passthrough", False)

    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return PreparedCompandRequest(body, "unsupported_body", False)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("input"), list):
        return PreparedCompandRequest(body, "unsupported_input", False)
    client_items = parsed["input"]
    if not client_items or not isinstance(client_items[-1], dict) or receipt is None:
        return PreparedCompandRequest(body, "no_eligible_suffix", False)

    provider_prefix: list[object] = []
    try:
        ledger = repository.get_ledger(tenant_id, session_id)
    except Exception as exc:
        raise CompandRuntimeUnavailable("continuation state is unavailable") from exc
    if ledger is not None:
        old_client, old_provider = ledger
        if len(client_items) < len(old_client) or client_items[: len(old_client)] != old_client:
            return PreparedCompandRequest(body, "client_history_drift", False)
        provider_prefix = old_provider
        if len(client_items[len(old_client) :]) != 1:
            return PreparedCompandRequest(body, "ambiguous_new_suffix", False)
    else:
        provider_prefix = list(client_items[:-1])

    output_item = client_items[-1]
    call_id = output_item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return PreparedCompandRequest(body, "missing_call_id", False)
    try:
        candidate = build_line_rle_candidate(
            receipt, expected_call_id=call_id, output_item=output_item
        )
    except ScanEligibilityError:
        return PreparedCompandRequest(body, "ineligible_receipt", False)
    if candidate.repeated_span_count == 0:
        return PreparedCompandRequest(body, "no_repeated_span", False)

    candidate_item = dict(output_item)
    candidate_item["output"] = candidate.candidate_text
    original_payload = dict(parsed)
    original_payload["input"] = [*provider_prefix, output_item]
    candidate_payload = dict(parsed)
    candidate_payload["input"] = [*provider_prefix, candidate_item]
    candidate_body = json.dumps(
        candidate_payload, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    try:
        original_tokens = await _provider_count(
            http_client, upstream_origin, upstream_api_key, original_payload
        )
        candidate_tokens = await _provider_count(
            http_client, upstream_origin, upstream_api_key, candidate_payload
        )
    except (httpx.HTTPError, ValueError, TypeError):
        return PreparedCompandRequest(body, "provider_count_failed", False)

    transformed = mode is GatewayMode.ENFORCE and candidate_tokens < original_tokens
    original_provider_body = (
        body
        if ledger is None
        else json.dumps(
            original_payload, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    )
    provider_body = candidate_body if transformed else original_provider_body
    outcome = (
        "enforced_cheaper" if transformed else
        "scan_cheaper" if candidate_tokens < original_tokens else
        "candidate_not_cheaper"
    )
    artifact_sha256 = capability = None
    if transformed:
        try:
            stored = repository.store_artifact(
                tenant_id,
                session_id,
                str(output_item["output"]).encode("utf-8"),
                retention_seconds=retention_seconds,
            )
        except Exception as exc:
            raise CompandRuntimeUnavailable("recovery state is unavailable") from exc
        if retention_seconds > 0 and stored is None:
            return PreparedCompandRequest(body, "recovery_store_failed", False)
        if stored is not None:
            artifact_sha256, capability = stored

    try:
        repository.record_receipt(
            correlation_id=correlation_id,
            tenant_id=tenant_id,
            session_id=session_id,
            request_sha256=request_sha256,
            technique="line-rle-v1",
            outcome=outcome,
            original_tokens=original_tokens,
            candidate_tokens=candidate_tokens,
            original_bytes=candidate.original_bytes,
            candidate_bytes=candidate.candidate_bytes,
            artifact_sha256=artifact_sha256,
        )
        if mode is GatewayMode.ENFORCE:
            repository.freeze_exchange(
                tenant_id,
                session_id,
                request_sha256,
                provider_body,
                transformed=transformed,
                artifact_sha256=artifact_sha256,
                capability=capability,
            )
            repository.save_ledger(
                tenant_id,
                session_id,
                list(client_items),
                [*provider_prefix, candidate_item] if transformed else list(client_items),
            )
    except Exception as exc:
        raise CompandRuntimeUnavailable("Compand durable state commit failed") from exc
    return PreparedCompandRequest(
        provider_body,
        outcome,
        transformed,
        original_tokens,
        candidate_tokens,
        candidate.original_bytes,
        candidate.candidate_bytes,
        artifact_sha256,
        capability,
    )


async def _provider_count(
    client: httpx.AsyncClient,
    origin: str,
    api_key: str,
    payload: dict[str, object],
) -> int:
    response = await client.post(
        origin + "/v1/responses/input_tokens",
        headers={
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
            "accept": "application/json",
            "x-compand-purpose": "provider-authoritative-count",
        },
        content=json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(),
    )
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError("provider count endpoint failed")
    data = response.json()
    value = data.get("input_tokens") if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("provider count response is invalid")
    return value
