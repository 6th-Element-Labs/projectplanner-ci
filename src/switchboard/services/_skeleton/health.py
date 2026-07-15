"""Liveness probe for a cut-out service process (ARCH-MS-73).

Mirrors the monolith's cheap ``GET /health`` contract so Caddy / systemd
health gates stay identical after a process cut. No DB or network I/O.
"""
from __future__ import annotations

from fastapi import APIRouter


def create_router(*, service_name: str) -> APIRouter:
    """Build a liveness router that reports ``service`` for operator triage."""
    router = APIRouter()

    @router.get("/health")
    async def health():
        """Liveness probe — must stay cheap so monitors/Caddy never block."""
        return {"status": "ok", "service": service_name}

    return router
