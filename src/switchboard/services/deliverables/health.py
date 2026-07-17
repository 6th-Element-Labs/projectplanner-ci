"""Shared liveness/readiness router for the Deliverables process."""
from switchboard.services.health import create_router

__all__ = ["create_router"]
