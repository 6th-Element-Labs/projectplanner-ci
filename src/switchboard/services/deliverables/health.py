"""Liveness and dependency readiness for the Deliverables process."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse


ReadinessProbe = Callable[[], dict]


def create_router(*, service_name: str,
                  readiness_probe: ReadinessProbe | None = None) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health():
        return {"status": "ok", "service": service_name}

    @router.get("/ready")
    async def ready():
        if readiness_probe is None:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "service": service_name,
                         "checks": {"readiness_probe": "not_configured"}},
            )
        try:
            result = readiness_probe()
        except Exception as exc:
            result = {"ok": False, "checks": {"probe": type(exc).__name__}}
        ok = result.get("ok") is True
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ready" if ok else "not_ready",
                     "service": service_name, "checks": result.get("checks") or {}},
        )

    return router
