"""Cheap liveness endpoint for the standalone Deliverables process."""
from __future__ import annotations

from fastapi import APIRouter


def create_router(*, service_name: str) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health():
        return {"status": "ok", "service": service_name}

    return router
