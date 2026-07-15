"""FastAPI application factory for a cut-out service (ARCH-MS-73)."""
from __future__ import annotations

from fastapi import FastAPI

from switchboard.services._skeleton import health as health_router
from switchboard.services._skeleton.contracts.openapi import build_openapi_document
from switchboard.services._skeleton.routers import example as example_router
from switchboard.services._skeleton.settings import SkeletonSettings


def create_app(settings: SkeletonSettings | None = None) -> FastAPI:
    """Build a standalone FastAPI app: health + stub domain router.

    Intentionally free of monolith composition (no ``app_impl``, store, or
    MCP). Callers that need the live Switchboard surface keep using ``app:app``.
    """
    cfg = settings or SkeletonSettings.from_env()
    application = FastAPI(
        title=f"Switchboard — {cfg.service_name}",
        version="0.1.0",
        description=(
            "Dormant service-cut skeleton (ARCH-MS-73). Not mounted behind "
            "production Caddy; use deploy/skeleton templates for cutover."
        ),
    )
    application.state.skeleton_settings = cfg
    application.include_router(health_router.create_router(service_name=cfg.service_name))
    application.include_router(example_router.create_router())

    @application.get("/openapi-skeleton.json", include_in_schema=False)
    async def openapi_skeleton():
        """Expose the contracts-built OpenAPI doc (not FastAPI's auto schema)."""
        return build_openapi_document(service_name=cfg.service_name)

    return application


# Module-level app for ``uvicorn switchboard.services._skeleton.app:app``.
app = create_app()
