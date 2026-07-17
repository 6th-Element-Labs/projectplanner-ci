"""Shared liveness/readiness router for the Coord cut-out process."""
from switchboard.services.health import create_router

__all__ = ["create_router"]
