"""FastAPI application factory for the Coord side-by-side process cut.

The service mounts only the five Go-authorized read routes from ADR-0013 plus
cheap liveness. MCP, writers, dispatch, roster, and other board-adjacent routes
remain on the monolith until separately chartered.
"""
from __future__ import annotations

from fastapi import FastAPI

from switchboard.api import deps as api_deps
from switchboard.api.coord_port_adapters import configure_coord_ports, probe_coord_readiness
from switchboard.api.middleware import register_auth_gate
from switchboard.services.coord import health as health_router
from switchboard.services.coord.router import create_router
from switchboard.services.coord.settings import CoordServiceSettings


def create_app(settings: CoordServiceSettings | None = None) -> FastAPI:
    """Build the standalone, read-only Coord application for port 8123."""
    cfg = settings or CoordServiceSettings.from_env()
    configure_coord_ports()

    application = FastAPI(
        title=f"Switchboard — {cfg.service_name}",
        version="0.1.0",
        description=(
            "Coord process-cut service (ARCH-MS-105/106). Read-only day-one "
            "surface owned through the production Caddy edge."
        ),
    )
    application.state.coord_service_settings = cfg
    application.include_router(
        health_router.create_router(
            service_name=cfg.service_name, readiness_probe=probe_coord_readiness)
    )
    application.include_router(create_router(
        resolve_project=api_deps.resolve_project,
        etag_json=api_deps.etag_json,
    ))
    # BUG-81: Caddy sends browser board reads directly to this process.  The
    # router's Auth port still performs the final project/scope check, but it
    # needs the shared gate to turn a valid taikun_session cookie into the same
    # request.state principal the monolith supplies.  Without this, Bearer reads
    # worked while every signed-in browser received 401 from /api/board.
    register_auth_gate(
        application,
        global_user_scopes=api_deps.global_user_scopes,
        global_principal=api_deps.global_principal,
        admin_scopes=api_deps.ADMIN_SCOPES,
    )
    return application
