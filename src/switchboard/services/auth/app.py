"""FastAPI application factory for the Auth process cut (ARCH-MS-75).

Composition root only: binds Auth ports, initializes Auth DDL, mounts health +
the shared Auth router. Does not import root ``store`` / ``auth`` / ``notify``
into the service package body — adapters live in ``auth_port_adapters``.

Use ``uvicorn --factory switchboard.services.auth.app:create_app`` (or
``python -m switchboard.services.auth``) so Auth DDL runs at process start,
not at import time.
"""
from __future__ import annotations

from fastapi import FastAPI

from switchboard.api.auth_port_adapters import configure_auth_ports
from switchboard.api.routers.auth import store as auth_store
from switchboard.api.routers.auth.routes import router as auth_router

from switchboard.services.auth import health as health_router
from switchboard.services.auth.settings import AuthServiceSettings


def create_app(settings: AuthServiceSettings | None = None) -> FastAPI:
    """Build a standalone Auth FastAPI app (side-by-side with the monolith).

    Intentionally omits ``/api/auth/me`` (monolith Access/UI helpers), SPA,
    MCP, and task routers. Caddy must not route live ``/api/auth*`` here until
    ARCH-MS-76.
    """
    cfg = settings or AuthServiceSettings.from_env()
    configure_auth_ports()
    auth_store.init()

    application = FastAPI(
        title=f"Switchboard — {cfg.service_name}",
        version="0.1.0",
        description=(
            "Auth process-cut service (ARCH-MS-75). Side-by-side with the "
            "monolith; production Caddy cutover is ARCH-MS-76."
        ),
    )
    application.state.auth_service_settings = cfg
    application.include_router(health_router.create_router(service_name=cfg.service_name))
    application.include_router(auth_router)
    return application
