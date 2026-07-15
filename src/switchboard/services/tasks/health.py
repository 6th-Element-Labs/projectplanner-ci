"""Liveness probe for the Tasks cut-out process (ARCH-MS-90)."""
from __future__ import annotations

from fastapi import APIRouter


def create_router(*, service_name: str) -> APIRouter:
    """Build a cheap liveness router (no DB / network I/O)."""
    router = APIRouter()

    @router.get("/health")
    async def health():
        return {"status": "ok", "service": service_name}

    return router
