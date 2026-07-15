"""Example domain router stub (ARCH-MS-73).

Replace with real bounded-context routes when cloning the skeleton. Stays free
of monolith / store imports so the process cut cannot accidentally re-couple.
"""
from __future__ import annotations

from fastapi import APIRouter

from switchboard.services._skeleton.contracts.v1 import ExamplePingResponse


def create_router() -> APIRouter:
    """Build the stub domain router (no business dependencies)."""
    router = APIRouter(prefix="/api/example", tags=["example"])

    @router.get("/ping", response_model=ExamplePingResponse)
    async def ping() -> ExamplePingResponse:
        return ExamplePingResponse(ok=True, message="skeleton")

    return router
