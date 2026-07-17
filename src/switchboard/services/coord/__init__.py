"""Coord/board process-cut service (ARCH-MS-104/105).

Standalone FastAPI + uvicorn unit for the read-only ADR-0013 day-one surface
on ``127.0.0.1:8123``. Production storage and Auth implementations are
injected through ports from ``switchboard.api.coord_port_adapters``. Live
Caddy cutover remains a later task.
"""

from .app import create_app
from .settings import CoordServiceSettings

__all__ = ("CoordServiceSettings", "create_app")
