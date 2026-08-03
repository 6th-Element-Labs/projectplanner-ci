"""FastAPI application factory for the Compand Responses pilot gateway."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import Callable

import httpx
from fastapi import FastAPI

from switchboard.api.routers.compand import CompandGatewayRuntime, create_router
from switchboard.contracts.compand import (
    EgressObservation,
    GatewayCoverageReceiptInput,
    GatewayTelemetry,
)
from switchboard.domain.compand import (
    ClientCredentialRegistry,
    GatewaySecurityError,
    validate_upstream_origin,
)
from switchboard.services.compand.settings import CompandGatewaySettings


def create_app(
    settings: CompandGatewaySettings | None = None,
    *,
    credentials: ClientCredentialRegistry | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    telemetry_sink: Callable[[GatewayTelemetry], None] | None = None,
    coverage_sink: Callable[[GatewayCoverageReceiptInput], None] | None = None,
    egress_observer: Callable[[EgressObservation], None] | None = None,
) -> FastAPI:
    cfg = settings or CompandGatewaySettings.from_env()
    normalized_origin = validate_upstream_origin(
        cfg.upstream_origin, allow_http_loopback=cfg.allow_http_loopback
    )
    if normalized_origin != cfg.upstream_origin:
        raise ValueError("upstream origin must be normalized")
    if not isinstance(cfg.upstream_api_key, str):
        raise GatewaySecurityError("upstream OpenAI credential must be a string")
    if not isinstance(cfg.frozen_tuple_config_attested, bool):
        raise GatewaySecurityError("frozen tuple configuration attestation must be boolean")
    registry = (
        credentials
        if credentials is not None
        else ClientCredentialRegistry(
            dict(cfg.client_credentials), set(cfg.revoked_credential_ids)
        )
    )
    if registry.contains_token(cfg.upstream_api_key):
        raise GatewaySecurityError(
            "client and upstream credentials must be separate identities"
        )
    client = httpx.AsyncClient(
        transport=transport,
        timeout=None,
        follow_redirects=False,
        trust_env=False,
    )
    runtime = CompandGatewayRuntime(
        settings=cfg,
        credentials=registry,
        http_client=client,
        telemetry_sink=telemetry_sink or (lambda _event: None),
        coverage_sink=coverage_sink or (lambda _event: None),
        egress_observer=egress_observer or (lambda _event: None),
    )

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        try:
            yield
        finally:
            await client.aclose()

    application = FastAPI(
        title="Compand — OpenAI Responses pilot gateway",
        version="0.1.0",
        description=(
            "No-transform passthrough and shadow Scan adapter. Caddy remains the edge; "
            "this service owns neither model routing nor Switchboard lifecycle."
        ),
        lifespan=lifespan,
    )
    application.state.compand_gateway_runtime = runtime
    application.include_router(create_router(runtime))

    return application


# Missing credentials leave every request fail-closed; deployment supplies them via env.
app = create_app()
