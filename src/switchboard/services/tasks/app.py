"""FastAPI application factory for the Tasks process cut (ARCH-MS-90).

Composition root only: binds Tasks ports, initializes registry DDL, mounts
health + Mode A day-one routers (tasks CRUD/move/archive + TXP claims). Does
not import root ``store`` / ``auth`` / ``dispatch`` into the service package
body — adapters live in ``tasks_port_adapters``.

Use ``uvicorn --factory switchboard.services.tasks.app:create_app`` (or
``python -m switchboard.services.tasks``) so runtime init runs at process
start, not at import time.

Live Caddy ``/api/tasks*`` / claim cutover is ARCH-MS-92 — keep production
traffic on the monolith until G6 + parity (ARCH-MS-91) clear.
"""
from __future__ import annotations

from fastapi import FastAPI

from switchboard.api import deps
from switchboard.api.middleware import register_auth_gate
from switchboard.api.routers import claims as claims_router
from switchboard.api.routers import tasks as tasks_router
from switchboard.api.tasks_port_adapters import (
    configure_tasks_ports,
    ensure_tasks_runtime,
    probe_tasks_readiness,
)

from switchboard.services.tasks import health as health_router
from switchboard.services.tasks.settings import TasksServiceSettings


def create_app(settings: TasksServiceSettings | None = None) -> FastAPI:
    """Build the standalone Tasks FastAPI app (Mode A thin surface).

    Intentionally omits review / dispatch / chat (sibling BCs stay on the
    monolith), SPA, MCP, and Auth routers. Side-by-side on ``:8122`` until
    Caddy cutover.
    """
    cfg = settings or TasksServiceSettings.from_env()
    configure_tasks_ports()
    ensure_tasks_runtime()

    application = FastAPI(
        title=f"Switchboard — {cfg.service_name}",
        version="0.1.0",
        description=(
            "Tasks process-cut service (ARCH-MS-90+). Mode A thin surface only; "
            "live edge traffic remains on the monolith until ARCH-MS-92."
        ),
    )
    application.state.tasks_service_settings = cfg

    # BUG-69: the monolith blocks anonymous /api/* reads at this exact boundary
    # (register_middleware -> register_auth_gate in app_impl.py). A service cut that
    # mounts the same routers without also registering this gate silently drops that
    # protection the moment Caddy points live traffic at it — verified live on prod
    # 2026-07-15 and again 2026-07-17 (anon GET /api/tasks?project=... -> 200 with the
    # full task list). /health stays open regardless: _auth_exempt_path allows it by
    # path, independent of where the router is mounted relative to this call.
    register_auth_gate(
        application,
        global_user_scopes=deps.global_user_scopes,
        global_principal=deps.global_principal,
        admin_scopes=deps.ADMIN_SCOPES,
    )

    application.include_router(health_router.create_router(
        service_name=cfg.service_name, readiness_probe=probe_tasks_readiness))
    application.include_router(
        tasks_router.create_router(
            resolve_project=deps.resolve_project,
            resolve_principal=deps.resolve_principal,
            thin_mode_a=True,
        )
    )
    application.include_router(
        claims_router.create_router(
            resolve_project=deps.resolve_project,
            resolve_principal=deps.resolve_principal,
            resolve_body_project=deps.resolve_body_project,
        )
    )
    return application
