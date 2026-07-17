"""Coord/board process-boundary package.

The package owns only the read-only ADR-0013 day-one surface.  Production
storage and Auth implementations are injected through ports from
``switchboard.api.coord_port_adapters``; importing this package never imports
the monolith ``store``, ``auth``, ``dispatch``, or ``signals`` facades.
"""

from .router import create_router

__all__ = ["create_router"]
