from __future__ import annotations

from fastapi import FastAPI

from switchboard.api import deps as api_deps
from switchboard.api.ingest_port_adapters import configure_ingest_ports, probe_ingest_readiness
from switchboard.api.middleware import register_auth_gate
from switchboard.services.ingest import health
from switchboard.services.ingest.router import create_router
from switchboard.services.ingest.settings import IngestServiceSettings


def create_app(settings: IngestServiceSettings | None = None) -> FastAPI:
    cfg = settings or IngestServiceSettings.from_env()
    configure_ingest_ports()
    app = FastAPI(title=f"Switchboard — {cfg.service_name}", version="0.1.0")
    app.state.ingest_service_settings = cfg
    register_auth_gate(app, global_user_scopes=api_deps.global_user_scopes,
                       global_principal=api_deps.global_principal,
                       admin_scopes=api_deps.ADMIN_SCOPES)
    app.include_router(health.create_router(service_name=cfg.service_name,
                                            readiness_probe=probe_ingest_readiness))
    app.include_router(create_router(resolve_project=api_deps.resolve_project))
    return app
