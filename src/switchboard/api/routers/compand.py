"""Thin FastAPI adapter for the Compand OpenAI Responses passthrough."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Callable

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from switchboard.application.commands.compand_gateway import (
    GatewayPlan,
    GatewayPolicy,
    GatewayRejection,
    GatewayRequest,
    downstream_response_headers,
    plan_gateway_request,
    upstream_request_headers,
)
from switchboard.contracts.compand import (
    EgressObservation,
    GatewayCoverageReceiptInput,
    GatewayErrorDetail,
    GatewayErrorEnvelope,
    GatewayTelemetry,
)
from switchboard.domain.compand import ClientCredentialRegistry
from switchboard.services.compand.settings import CompandGatewaySettings


TelemetrySink = Callable[[GatewayTelemetry], None]
CoverageSink = Callable[[GatewayCoverageReceiptInput], None]
EgressObserver = Callable[[EgressObservation], None]


def _ignore_event(_event: object) -> None:
    return None


@dataclass
class CompandGatewayRuntime:
    settings: CompandGatewaySettings
    credentials: ClientCredentialRegistry
    http_client: httpx.AsyncClient
    telemetry_sink: TelemetrySink = _ignore_event
    coverage_sink: CoverageSink = _ignore_event
    egress_observer: EgressObserver = _ignore_event


def create_router(runtime: CompandGatewayRuntime) -> APIRouter:
    router = APIRouter()

    @router.api_route(
        "/v1/{provider_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy_provider_request(provider_path: str, request: Request):
        del (
            provider_path
        )  # Admission uses the exact ASGI path, not a decoded route parameter.
        correlation_id = f"cmp_{uuid.uuid4().hex}"
        started = time.perf_counter()
        try:
            request_path = _raw_ascii_path(request)
        except GatewayRejection as rejection:
            return _rejection_response(
                runtime,
                GatewayRequest(
                    method=request.method,
                    path="<invalid-raw-path>",
                    query=request.scope.get("query_string", b""),
                    headers=tuple(request.scope.get("headers", ())),
                    body=b"",
                ),
                correlation_id=correlation_id,
                started=started,
                rejection=rejection,
            )
        raw_body = await _read_limited_body(
            request, runtime.settings.max_request_bytes + 1
        )
        envelope = GatewayRequest(
            method=request.method,
            path=request_path,
            query=request.scope.get("query_string", b""),
            headers=tuple(request.scope.get("headers", ())),
            body=raw_body,
        )
        try:
            plan = plan_gateway_request(
                envelope,
                policy=GatewayPolicy(
                    mode=runtime.settings.mode,
                    max_request_bytes=runtime.settings.max_request_bytes,
                    frozen_tuple_config_attested=(
                        runtime.settings.frozen_tuple_config_attested
                    ),
                ),
                credentials=runtime.credentials,
            )
        except GatewayRejection as rejection:
            return _rejection_response(
                runtime,
                envelope,
                correlation_id=correlation_id,
                started=started,
                rejection=rejection,
            )

        if not runtime.settings.upstream_api_key:
            return _rejection_response(
                runtime,
                envelope,
                correlation_id=correlation_id,
                started=started,
                rejection=GatewayRejection(
                    503,
                    "upstream_credential_unavailable",
                    "The configured upstream OpenAI credential is unavailable.",
                ),
                plan=plan,
            )

        egress_classification = "excluded" if plan.feature == "models" else "captured"
        reason_code = (
            "control_endpoint"
            if plan.feature == "models"
            else "certified_gateway_path"
            if plan.tuple_status == "certified"
            else "unknown_client_tuple"
        )
        try:
            runtime.egress_observer(
                EgressObservation(
                    correlation_id=correlation_id,
                    method=envelope.method,
                    endpoint=envelope.path,
                    classification=egress_classification,
                    reason_code=reason_code,
                )
            )
            runtime.coverage_sink(
                GatewayCoverageReceiptInput(
                    correlation_id=correlation_id,
                    client_version=plan.client_version,
                    mode=plan.mode.value,
                    certified_feature=plan.feature,
                    tuple_status=plan.tuple_status,
                    observed_endpoint=envelope.path,
                    egress_classification=egress_classification,
                    source_version=runtime.settings.source_version,
                )
            )
        except (
            Exception
        ) as exc:  # Evidence hooks are part of this observable pilot boundary.
            return _rejection_response(
                runtime,
                envelope,
                correlation_id=correlation_id,
                started=started,
                rejection=GatewayRejection(
                    503,
                    "observation_policy_failed",
                    f"The required observation hook failed: {_safe_cause(exc, runtime.settings)}",
                ),
                plan=plan,
            )

        target = runtime.settings.upstream_origin + envelope.path
        if envelope.query:
            target += "?" + envelope.query.decode("ascii")
        try:
            upstream_request = runtime.http_client.build_request(
                envelope.method,
                target,
                headers=upstream_request_headers(
                    envelope.headers, runtime.settings.upstream_api_key
                ),
                content=envelope.body,
            )
            upstream_response = await runtime.http_client.send(
                upstream_request, stream=True
            )
        except httpx.HTTPError as exc:
            cause = _safe_cause(exc, runtime.settings)
            rejection = GatewayRejection(
                502,
                "upstream_transport_failed",
                f"The direct OpenAI transport failed: {cause}",
            )
            return _rejection_response(
                runtime,
                envelope,
                correlation_id=correlation_id,
                started=started,
                rejection=rejection,
                plan=plan,
                outcome="transport_failed",
                cause=cause,
            )

        response_bytes = 0

        async def passthrough_body():
            nonlocal response_bytes
            outcome = "completed"
            classification = "upstream_response"
            try:
                async for chunk in upstream_response.aiter_raw():
                    response_bytes += len(chunk)
                    yield chunk
            except asyncio.CancelledError:
                outcome = "cancelled"
                classification = "client_cancelled"
                raise
            except httpx.HTTPError:
                outcome = "transport_failed"
                classification = "upstream_stream_read_failed"
                raise
            finally:
                await upstream_response.aclose()
                runtime.telemetry_sink(
                    GatewayTelemetry(
                        correlation_id=correlation_id,
                        method=envelope.method,
                        endpoint=envelope.path,
                        mode=plan.mode.value,
                        outcome=outcome,
                        classification=classification,
                        status_code=upstream_response.status_code,
                        request_bytes=len(envelope.body),
                        response_bytes=response_bytes,
                        elapsed_ms=_elapsed_ms(started),
                        credential_id=plan.credential_id,
                        scan=plan.scan,
                    )
                )

        response = StreamingResponse(
            passthrough_body(), status_code=upstream_response.status_code
        )
        response.raw_headers = downstream_response_headers(
            upstream_response.headers.raw
        )
        return response

    return router


async def _read_limited_body(request: Request, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        remaining = limit - size
        chunks.append(chunk[:remaining])
        size += min(len(chunk), remaining)
        if size >= limit:
            break
    return b"".join(chunks)


def _raw_ascii_path(request: Request) -> str:
    raw_path = request.scope.get("raw_path")
    if not isinstance(raw_path, bytes) or not raw_path:
        raise GatewayRejection(
            400,
            "malformed_path",
            "The exact ASCII request path is required.",
        )
    try:
        path = raw_path.decode("ascii")
    except UnicodeDecodeError as exc:
        raise GatewayRejection(
            400,
            "malformed_path",
            "The exact request path must be ASCII encoded.",
        ) from exc
    if "%" in path:
        raise GatewayRejection(
            400,
            "noncanonical_path",
            "Percent-encoded path aliases are not accepted.",
        )
    return path


def _rejection_response(
    runtime: CompandGatewayRuntime,
    request: GatewayRequest,
    *,
    correlation_id: str,
    started: float,
    rejection: GatewayRejection,
    plan: GatewayPlan | None = None,
    outcome: str = "rejected",
    cause: str | None = None,
) -> JSONResponse:
    runtime.telemetry_sink(
        GatewayTelemetry(
            correlation_id=correlation_id,
            method=request.method,
            endpoint=request.path,
            mode=(plan.mode if plan else runtime.settings.mode).value,
            outcome=outcome,
            classification=rejection.classification,
            status_code=rejection.status_code,
            request_bytes=len(request.body),
            response_bytes=0,
            elapsed_ms=_elapsed_ms(started),
            credential_id=plan.credential_id if plan else None,
            scan=plan.scan if plan else None,
        )
    )
    payload = GatewayErrorEnvelope(
        error=GatewayErrorDetail(
            message=rejection.message,
            code=rejection.classification,
            classification=rejection.classification,
            correlation_id=correlation_id,
            cause=cause,
        )
    )
    return JSONResponse(
        status_code=rejection.status_code,
        content=payload.model_dump(by_alias=True, exclude_none=True),
    )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _safe_cause(exc: Exception, settings: CompandGatewaySettings) -> str:
    value = f"{exc.__class__.__name__}: {exc}"
    secrets_to_remove = [
        settings.upstream_api_key,
        *settings.client_credentials.values(),
    ]
    for secret in secrets_to_remove:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value[:500]
