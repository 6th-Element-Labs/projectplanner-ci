"""FastAPI application factory for the Deliverables side-by-side process cut.

Only the ADR-0014 read surface is mounted. All writers, MCP, SPA, Tally, Tasks,
Coord, and other sibling bounded contexts stay on the monolith.
"""
from __future__ import annotations

from fastapi import FastAPI

from switchboard.api import deps as api_deps
from switchboard.api.deliverables_port_adapters import (
    configure_deliverables_ports,
    probe_deliverables_readiness,
)
from switchboard.api.middleware import register_auth_gate
from switchboard.services.deliverables import health as health_router
from switchboard.services.deliverables.router import create_router
from switchboard.services.deliverables.settings import DeliverablesServiceSettings


def create_app(
    settings: DeliverablesServiceSettings | None = None,
) -> FastAPI:
    """Build the read-only Deliverables application for port 8124."""
    cfg = settings or DeliverablesServiceSettings.from_env()
    configure_deliverables_ports()

    application = FastAPI(
        title=f"Switchboard — {cfg.service_name}",
        version="0.1.0",
        description=(
            "Deliverables/mission process-cut service (ARCH-MS-110). "
            "Read-only day-one surface; live Caddy cutover is a successor task."
        ),
    )
    application.state.deliverables_service_settings = cfg
    # Preserve the monolith's transport parity before the route-level read port
    # runs: bearer agents and cookie-backed browser sessions both become one
    # project-scoped request.state principal. Without this bridge, a future Caddy
    # cut would reject a valid taikun_session as if only Bearer auth existed.
    register_auth_gate(
        application,
        global_user_scopes=api_deps.global_user_scopes,
        global_principal=api_deps.global_principal,
        admin_scopes=api_deps.ADMIN_SCOPES,
    )
    application.include_router(
        health_router.create_router(
            service_name=cfg.service_name,
            readiness_probe=probe_deliverables_readiness,
        )
    )
    application.include_router(
        create_router(
            resolve_project=api_deps.resolve_project,
            etag_json=api_deps.etag_json,
        )
    )
    return application
